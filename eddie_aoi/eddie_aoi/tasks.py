"""Celery tasks for building a reach and its AOI variants.

This module is REQUIRED, not optional. EDDIE discovers plugins by scanning
site-packages for ``eddie_*`` packages and then unconditionally importing
``<name>.tasks`` (``eddie/tasks.py``, the loop after ``discover_plugins()``).
A package named ``eddie_aoi`` with no ``tasks`` module makes the Celery
worker crash-loop on startup with::

    ModuleNotFoundError: No module named 'eddie_aoi.tasks'

Building a reach is slow enough to want off the request thread - reading the
marker source, routing, then validating every marker - so the frontend
submits it as a task and polls for status.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from celery import signals
from eddie.tasks import app

from eddie_aoi.builder import METHOD_MARKER_POLYLINE, build_reach
from eddie_aoi.config import fallback_reach_spec
from eddie_aoi.db import create_schema, get_engine
from eddie_aoi.sources import RiverSources
from eddie_aoi.store import get_or_build, set_pinned
from eddie_aoi.variant_store import get_or_build_variant

log = logging.getLogger(__name__)

#: Where the kilometre-marker source sits inside the container. Overridable so
#: the path is configuration rather than a constant.
MARKERS_PATH_ENV = "AOI_MARKERS_PATH"
DEFAULT_MARKERS_PATH = "/app/src/static/geo/kilometrage-des-cours-deau.gpkg"

#: The mapped-river source is optional: without it the centerline is built
#: from the ordered markers instead, which records that it does not follow
#: the mapped watercourse.
RIVER_LINES_PATH_ENV = "AOI_RIVER_LINES_PATH"

#: Externally supplied lookup-variant sources. Configuration, so relocating a
#: layer needs no code change; absent ones leave their variant unavailable,
#: which it reports rather than failing late.
HQ100_PATH_ENV = "AOI_HQ100_PATH"
DEFAULT_HQ100_PATH = "/app/src/static/geo/HWGK2021_HQ100/HWGK2021_HQ100.shp"
WATERSHEDS_PATH_ENV = "AOI_WATERSHEDS_PATH"
HQ_EXTREME_PATH_ENV = "AOI_HQ_EXTREME_PATH"

#: Variants built and persisted whenever a reach is built, so the map has
#: something to show without a second request.
#:
#: hq100 is included even though it is large - 81 parts and about 2 MB of
#: GeoJSON clipped to a 15 km reach - because the catalog fragment leaves it
#: off the workbench by default, so a viewer only downloads it on ticking the
#: layer. It is the one dataset here that shows REAL data cut to the AOI
#: rather than the AOI outline itself. Building it is cheap (0.5-1.0 s); only
#: transferring it is not. If it ever needs to be on by default, move it to a
#: WMS layer so the rendering happens server-side.
DISPLAY_VARIANTS = (
    # The band the modules compute over (building_population, flood_costs - see
    # eddie_aoi.resolver.BINDINGS), so the map shows the area the numbers describe.
    ("corridor", {"half_width_m": 500.0}),
    ("corridor", {"half_width_m": 300.0}),
    ("buffer", {"d": 1000.0}),
    ("bbox", {"margin_m": 300.0}),
    ("watersheds", {}),
    ("hq100", {}),
)


def configured_sources() -> RiverSources:
    """The marker and mapped-river sources, from the environment."""
    markers = Path(os.environ.get(MARKERS_PATH_ENV, DEFAULT_MARKERS_PATH))
    lines_raw = os.environ.get(RIVER_LINES_PATH_ENV)
    return RiverSources(
        markers_path=markers,
        river_lines_path=Path(lines_raw) if lines_raw else None,
    )


def _optional_path(env_name: str, default: str | None = None) -> Path | None:
    """A configured source path, or None when it is absent on disk."""
    raw = os.environ.get(env_name, default)
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def configured_registry():
    """The variant registry with its lookup sources taken from the environment."""
    from eddie_aoi.variants import default_registry

    return default_registry(
        hq100_path=_optional_path(HQ100_PATH_ENV, DEFAULT_HQ100_PATH),
        watersheds_path=_optional_path(WATERSHEDS_PATH_ENV),
        hq_extreme_path=_optional_path(HQ_EXTREME_PATH_ENV),
    )


def default_method(sources: RiverSources) -> str:
    """Route through the mapped river when it is available, else the markers."""
    from eddie_aoi.builder import METHOD_ROUTED

    if sources.river_lines_path and Path(sources.river_lines_path).exists():
        return METHOD_ROUTED
    return METHOD_MARKER_POLYLINE


def select_reach(reach_id: str) -> list[dict]:
    """Make a built reach the current one and recompute the modules that follow it.

    Only ever called after a successful build, so a refused or failed selection
    never changes the current reach and never triggers a recompute. Returns what
    was sent, for the build result.
    """
    from eddie_aoi.current import set_current
    from eddie_aoi.resolver import FOLLOWERS

    with get_engine().begin() as conn:
        set_current(conn, reach_id)
    log.info("Current reach is now %s; recomputing %s", reach_id, sorted(FOLLOWERS))

    sent = []
    for dataset, task_name in FOLLOWERS.items():
        task = app.send_task(task_name)
        sent.append({"dataset": dataset, "task": task_name, "task_id": task.id})
    return sent


@app.task(name="eddie_aoi.tasks.build_aoi_reach")
def build_aoi_reach(
    river: str,
    km_start: float,
    km_end: float,
    pinned: bool = False,
    select: bool = False,
) -> dict:
    """Build (or reuse) a reach and report what was produced.

    ``select`` marks a user's selection (the REST endpoint and the WPS form pass
    it): once the reach and its variants are built, it becomes the current reach
    and the follower modules are recomputed for it. Internal builds - the
    fallback at startup, a catalog request - leave the current reach alone.

    Returns a plain dict so the existing task-status route can hand it to the
    frontend unchanged.
    """
    sources = configured_sources()
    method = default_method(sources)
    engine = get_engine()

    with engine.begin() as conn:
        create_schema(conn)
        reach = get_or_build(
            conn,
            river,
            km_start,
            km_end,
            lambda: build_reach(sources, river, km_start, km_end, method=method),
            current_source_sha256=sources.markers_sha256(),
        )
        if pinned:
            set_pinned(conn, reach.reach_id, True)

    # Build the display variants in the same task, so a reach is ready to
    # show the moment the task reports success.
    variants = {}
    registry = configured_registry()
    with engine.begin() as conn:
        for name, params in DISPLAY_VARIANTS:
            try:
                cached = get_or_build_variant(conn, reach, registry, name, **params)
            except Exception as exc:
                # One unavailable variant with no fallback must not sink the
                # reach; it is reported instead.
                log.warning("AOI variant %r unavailable for %s: %s",
                            name, reach.reach_id, exc)
                variants[name] = {"error": str(exc)}
                continue
            variants[cached.variant_key] = {
                "variant": cached.variant,
                "requested": cached.requested_variant,
                "degraded": cached.degraded,
                "degraded_reason": cached.degraded_reason,
            }

    provenance = reach.provenance
    log.info("AOI reach ready: %s (%s)", reach.reach_id, provenance.get("method"))

    recomputations = select_reach(reach.reach_id) if select else []

    return {
        "current": bool(select),
        "recomputations": recomputations,
        "reach_id": reach.reach_id,
        "river": reach.river,
        "km_start": reach.km_start,
        "km_end": reach.km_end,
        "centerline_length_m": reach.centerline.length,
        "method": provenance.get("method"),
        "follows_mapped_river": provenance.get("follows_mapped_river"),
        "length_deviation_fraction": provenance.get("length_deviation_fraction"),
        "marker_count": provenance.get("marker_count"),
        "variants": variants,
    }


@app.task(name="eddie_aoi.tasks.ensure_fallback_reach")
def ensure_fallback_reach() -> dict:
    """Build and pin the configured fallback reach.

    Gives a deployment a usable AOI before anyone has selected one, replacing
    the hardcoded ``selected_polygon.geojson`` read at worker startup.
    """
    spec = fallback_reach_spec()
    return build_aoi_reach(spec.river, spec.km_start, spec.km_end, pinned=True)


@signals.worker_ready.connect
def _ensure_fallback_at_startup(sender=None, **_kwargs) -> None:
    """Build and pin the fallback reach whenever the worker starts (task 10.3a).

    Hooked here, in the plugin, rather than in the project's own startup hook
    (src/eddie_antartica/tasks.py): that file is baked into the 30 GB image, so
    changing it needs a rebuild, whereas this plugin is bind-mounted. Celery runs
    every connected worker_ready handler, so the project's own hook (EDDIE's
    static ingest, still reading selected_polygon.geojson - task 10.3b) runs too.
    """
    ensure_fallback_reach.delay()
