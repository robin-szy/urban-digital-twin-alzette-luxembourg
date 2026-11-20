"""Which area each dataset is processed against: the one place that decides it.

Every dataset is BOUND to an AOI variant here, in ``BINDINGS``. A module never
defines its own area (no table names, no distances, no km ranges in its code);
it asks ``resolve(conn, dataset)`` and gets the geometry for the CURRENT reach
together with what produced it. Changing a dataset's area is a one-line change
in ``BINDINGS`` and no edit inside the module.

Three roles of "the AOI" are kept apart (design, "Resolver"):

* fetch envelope - a RECTANGLE covering every variant of the reach, for sources
  that only accept bounds (``envelope``). Never a clip mask, never the
  denominator of a statistic.
* clip mask - what a dataset's stored rows are cut to: its bound variant.
* analysis domain - the one area a statistic is measured over: also its bound
  variant, and reported with the result (``Resolution.variant_key``).

``FOLLOWERS`` lists the modules that recompute automatically when a new reach
is selected (see ``eddie_aoi.tasks.build_aoi_reach``). A module may join only
once it resolves its area here AND any "already computed?" check it has matches
on the reach - otherwise it would skip work for the wrong area.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from eddie_aoi.current import current_reach
from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import Reach
from eddie_aoi.store import ReachNotReady, get_or_build, read_cached
from eddie_aoi.variant_store import get_or_build_variant
from eddie_aoi.variants import VariantRegistry, dissolve, variant_key

#: Pseudo-variant: the rectangle around every other bound variant of the reach.
ENVELOPE = "envelope"


@dataclass(frozen=True)
class Binding:
    """The variant (and its parameters) a dataset is processed against."""

    variant: str
    params: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return ENVELOPE if self.variant == ENVELOPE else variant_key(self.variant, self.params)


#: dataset -> its area. THE registry: nothing else in the project defines an AOI.
BINDINGS: dict[str, Binding] = {
    # 500 m, not the 300 m planned for buildings: it is the band Devika's flood-cost
    # study already uses (BUILDINGS_CORRIDOR_M), so people and damage are counted
    # over the same area and stay comparable (team decision, 2026-11-20).
    "building_population": Binding("corridor", {"half_width_m": 500.0}),
    "flood_costs": Binding("corridor", {"half_width_m": 500.0}),
    "landcover": Binding("corridor", {"half_width_m": 300.0}),
    "buildings": Binding("corridor", {"half_width_m": 300.0}),
    "addresses": Binding("corridor", {"half_width_m": 300.0}),
    # Replaces AOI_STATION_TOLERANCE_M = 1000.0 in eddie_water_level (task 10.2).
    "water_gauges": Binding("buffer", {"d": 1000.0}),
    # Degrades to a 5 km buffer until a watershed layer exists.
    "precipitation": Binding("watersheds"),
    # Terrain analysis needs the contributing area around the reach, so its BUILD
    # extent is the envelope; only what it publishes is clipped narrower.
    "dem_build": Binding(ENVELOPE),
    "dem_publish": Binding("corridor", {"half_width_m": 300.0}),
}

#: dataset -> the Celery task that recomputes it for the current reach.
#: Sent, in this order, whenever a selection becomes the current reach.
FOLLOWERS: dict[str, str] = {
    "building_population": "eddie_building_population.tasks.compute_building_population",
    # Depths for the selected reach; the damage steps run once Devika's missing HQextreme
    # review files are on the host (until then reported as skipped, not failed).
    "flood_costs": "eddie_flood_costs.tasks.recompute_for_current_reach",
}


@dataclass(frozen=True)
class Resolution:
    """A dataset's area for a reach, and exactly what produced it.

    ``variant_key`` is the REQUESTED key (e.g. ``corridor_500``, ``watersheds``);
    ``actual`` names what was really used. They differ only when ``degraded``.
    """

    dataset: str
    reach_id: str
    geometry: BaseGeometry
    requested: str
    actual: str
    variant_key: str
    degraded: bool
    degraded_reason: str | None
    is_fallback: bool


def binding_for(dataset: str) -> Binding:
    try:
        return BINDINGS[dataset]
    except KeyError:
        raise PreparationError(
            f"No AOI binding for dataset {dataset!r}. Registered datasets: "
            f"{sorted(BINDINGS)}. Add it to eddie_aoi.resolver.BINDINGS rather "
            f"than giving the module its own area."
        ) from None


def ensure_reach(
    conn: sa.Connection, reach_id: str, *, sources=None, is_fallback=False, env=None
) -> Reach:
    """The ready reach for an identifier.

    The fallback is built on demand (a fresh database has no reach yet); any
    other reach must already have been built by its selection.
    """
    cached = read_cached(conn, reach_id)
    if cached is not None and cached.is_ready:
        return cached.reach
    if cached is not None and not is_fallback:
        raise ReachNotReady(reach_id, cached.status, cached.error)
    if not is_fallback:
        raise PreparationError(f"Reach {reach_id} has not been built.")

    from pathlib import Path

    from eddie_aoi.builder import METHOD_MARKER_POLYLINE, METHOD_ROUTED, build_reach
    from eddie_aoi.config import fallback_reach_spec
    from eddie_aoi.store import set_pinned

    if sources is None:
        from eddie_aoi.tasks import configured_sources  # container only: imports Celery

        sources = configured_sources()
    # Same choice as eddie_aoi.tasks.default_method, without importing Celery.
    lines = sources.river_lines_path
    method = METHOD_ROUTED if lines and Path(lines).exists() else METHOD_MARKER_POLYLINE
    spec = fallback_reach_spec(env)
    reach = get_or_build(
        conn, spec.river, spec.km_start, spec.km_end,
        lambda: build_reach(sources, spec.river, spec.km_start, spec.km_end, method=method),
        current_source_sha256=sources.markers_sha256(),
    )
    set_pinned(conn, reach.reach_id, True)
    return reach


def _registry(registry: VariantRegistry | None) -> VariantRegistry:
    if registry is not None:
        return registry
    from eddie_aoi.tasks import configured_registry  # container only: imports Celery

    return configured_registry()


def envelope(conn: sa.Connection, reach: Reach, registry: VariantRegistry) -> tuple:
    """The fetch envelope: a rectangle around every bound variant of the reach.

    Returns ``(geometry, degraded, reasons)``; degraded when any variant inside
    it was a substitute. A rectangle on purpose - it is for bounds-only sources,
    so it must never be used as a clip mask or an analysis domain.
    """
    parts, reasons = [], []
    for binding in {b.key: b for b in BINDINGS.values() if b.variant != ENVELOPE}.values():
        cached = get_or_build_variant(conn, reach, registry, binding.variant, **binding.params)
        parts.append(cached.geometry)
        if cached.degraded:
            reasons.append(f"{cached.requested_variant}: {cached.degraded_reason}")
    return box(*dissolve(parts).bounds), bool(reasons), reasons


def resolve(
    conn: sa.Connection,
    dataset: str,
    reach_id: str | None = None,
    *,
    registry: VariantRegistry | None = None,
    sources=None,
    env: dict | None = None,
) -> Resolution:
    """A dataset's area for a reach - the CURRENT reach unless one is named."""
    binding = binding_for(dataset)
    is_fallback = False
    if reach_id is None:
        current = current_reach(conn, env)
        reach_id, is_fallback = current.reach_id, current.is_fallback
    reach = ensure_reach(conn, reach_id, sources=sources, is_fallback=is_fallback, env=env)
    registry = _registry(registry)

    if binding.variant == ENVELOPE:
        geometry, degraded, reasons = envelope(conn, reach, registry)
        return Resolution(
            dataset=dataset, reach_id=reach.reach_id, geometry=geometry,
            requested=ENVELOPE, actual=ENVELOPE, variant_key=ENVELOPE,
            degraded=degraded, degraded_reason="; ".join(reasons) or None,
            is_fallback=is_fallback,
        )

    cached = get_or_build_variant(conn, reach, registry, binding.variant, **binding.params)
    return Resolution(
        dataset=dataset,
        reach_id=reach.reach_id,
        geometry=cached.geometry,
        requested=cached.requested_variant,
        actual=cached.variant,
        variant_key=binding.key,
        degraded=cached.degraded,
        degraded_reason=cached.degraded_reason,
        is_fallback=is_fallback,
    )
