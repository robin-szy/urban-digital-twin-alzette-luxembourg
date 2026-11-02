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

from eddie.tasks import app

from eddie_aoi.builder import METHOD_MARKER_POLYLINE, build_reach
from eddie_aoi.config import fallback_reach_spec
from eddie_aoi.db import create_schema, get_engine
from eddie_aoi.sources import RiverSources
from eddie_aoi.store import get_or_build, set_pinned

log = logging.getLogger(__name__)

#: Where the kilometre-marker source sits inside the container. Overridable so
#: the path is configuration rather than a constant.
MARKERS_PATH_ENV = "AOI_MARKERS_PATH"
DEFAULT_MARKERS_PATH = "/app/src/static/geo/kilometrage-des-cours-deau.gpkg"

#: The mapped-river source is optional: without it the centerline is built
#: from the ordered markers instead, which records that it does not follow
#: the mapped watercourse.
RIVER_LINES_PATH_ENV = "AOI_RIVER_LINES_PATH"


def configured_sources() -> RiverSources:
    """The marker and mapped-river sources, from the environment."""
    markers = Path(os.environ.get(MARKERS_PATH_ENV, DEFAULT_MARKERS_PATH))
    lines_raw = os.environ.get(RIVER_LINES_PATH_ENV)
    return RiverSources(
        markers_path=markers,
        river_lines_path=Path(lines_raw) if lines_raw else None,
    )


def default_method(sources: RiverSources) -> str:
    """Route through the mapped river when it is available, else the markers."""
    from eddie_aoi.builder import METHOD_ROUTED

    if sources.river_lines_path and Path(sources.river_lines_path).exists():
        return METHOD_ROUTED
    return METHOD_MARKER_POLYLINE


@app.task(name="eddie_aoi.tasks.build_aoi_reach")
def build_aoi_reach(
    river: str,
    km_start: float,
    km_end: float,
    pinned: bool = False,
) -> dict:
    """Build (or reuse) a reach and report what was produced.

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

    provenance = reach.provenance
    log.info("AOI reach ready: %s (%s)", reach.reach_id, provenance.get("method"))

    return {
        "reach_id": reach.reach_id,
        "river": reach.river,
        "km_start": reach.km_start,
        "km_end": reach.km_end,
        "centerline_length_m": reach.centerline.length,
        "method": provenance.get("method"),
        "follows_mapped_river": provenance.get("follows_mapped_river"),
        "length_deviation_fraction": provenance.get("length_deviation_fraction"),
        "marker_count": provenance.get("marker_count"),
    }


@app.task(name="eddie_aoi.tasks.ensure_fallback_reach")
def ensure_fallback_reach() -> dict:
    """Build and pin the configured fallback reach.

    Gives a deployment a usable AOI before anyone has selected one, replacing
    the hardcoded ``selected_polygon.geojson`` read at worker startup.
    """
    spec = fallback_reach_spec()
    return build_aoi_reach(spec.river, spec.km_start, spec.km_end, pinned=True)
