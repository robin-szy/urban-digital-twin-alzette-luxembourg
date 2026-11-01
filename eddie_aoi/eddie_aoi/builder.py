"""Building a validated reach from the kilometre-marker and river sources.

Two methods produce a centerline:

``routed``
    Searches the mapped watercourse geometry for a path between the endpoint
    markers. Faithful to the drawn river, but the search can divert up a
    tributary and return, so its length must agree with the chainage span
    within tolerance or the reach is rejected.

``marker_polyline``
    Joins the ordered kilometre markers directly. No search happens, so it
    cannot divert anywhere - which also means the length gate cannot indicate
    a fault and is therefore not applied. Straight hops between markers cut
    the corner on bends, so the length lands slightly short; that deviation is
    recorded instead. At the source's 100 m marker spacing the lateral error
    is at most about 12 m on the tightest bends and typically 2-6 m, against a
    300 m corridor half-width.

A consumer can always tell which it got from ``provenance['method']`` and
``provenance['follows_mapped_river']``.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import LineString

from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import Reach, validate_bounds
from eddie_aoi.routing import select_route, validate_route_length
from eddie_aoi.sources import RiverSources, available_rivers, read_markers, read_river_lines

METHOD_ROUTED = "routed"
METHOD_MARKER_POLYLINE = "marker_polyline"


def _length_deviation(route_length_m: float, km_start: float, km_end: float) -> dict:
    """How far a centerline's length sits from the requested chainage span."""
    expected_m = (km_end - km_start) * 1000
    difference = route_length_m - expected_m
    return {
        "chainage_span_m": expected_m,
        "length_deviation_m": difference,
        "length_deviation_fraction": (difference / expected_m) if expected_m else None,
    }


def build_reach_from_geometries(
    river: str,
    km_start: float,
    km_end: float,
    river_lines: list,
    markers: list,
    marker_km: list[float],
    *,
    source_sha256: str | None = None,
    marker_tolerance: float = 20,
) -> Reach:
    """Resolve and validate a reach from already-loaded geometry.

    Kept separate from source reading so the validation can be exercised
    against synthetic geometry, and so a caller holding geometry from
    elsewhere does not have to go through files.
    """
    validate_bounds(river, km_start, km_end)

    route, details = select_route(
        river_lines, markers, marker_tolerance=marker_tolerance
    )
    validate_route_length(details["route_length_m"], km_start, km_end)

    positions = tuple(route.project(point) for point in markers)

    provenance = {
        "method": METHOD_ROUTED,
        "follows_mapped_river": True,
        "route_length_m": details["route_length_m"],
        "marker_max_offset_m": details["marker_max_offset_m"],
        "marker_offset_tolerance_m": details["marker_offset_tolerance_m"],
        "markers_in_order": details["markers_follow_route_in_order"],
        "numerical_join_tolerance_m": details["numerical_join_tolerance_m"],
        "numerical_joins_m": details["numerical_joins_m"],
        "marker_count": len(markers),
        "length_gate_applied": True,
        "source_sha256": source_sha256,
        **_length_deviation(details["route_length_m"], km_start, km_end),
    }

    return Reach(
        river=river,
        km_start=float(km_start),
        km_end=float(km_end),
        centerline=route,
        marker_km=tuple(float(km) for km in marker_km),
        marker_positions=positions,
        provenance=provenance,
    )


def build_reach_from_markers(
    river: str,
    km_start: float,
    km_end: float,
    markers: list,
    marker_km: list[float],
    *,
    source_sha256: str | None = None,
) -> Reach:
    """Construct a centerline by joining the ordered kilometre markers.

    Used when the mapped watercourse geometry is unavailable. No path search
    occurs, so the reach cannot divert up a tributary and the chainage length
    gate is deliberately not applied - its deviation is recorded instead. The
    provenance states that this centerline does not follow the mapped river.
    """
    validate_bounds(river, km_start, km_end)

    if len(markers) < 2:
        raise PreparationError(
            "A marker-constructed centerline needs at least two markers."
        )
    if len(markers) != len(marker_km):
        raise PreparationError("Markers and their kilometre values must be parallel.")
    if not all(a < b for a, b in zip(marker_km, marker_km[1:])):
        raise PreparationError(
            "Marker kilometres must be strictly increasing to construct a centerline."
        )

    centerline = LineString([(point.x, point.y) for point in markers])
    if not centerline.is_simple:
        # Self-crossing means the markers do not describe a single stretch.
        raise PreparationError(
            "The marker-constructed centerline crosses itself; the markers do not "
            "describe one continuous stretch of river."
        )

    positions = tuple(centerline.project(point) for point in markers)
    if not all(a < b for a, b in zip(positions, positions[1:])):
        raise PreparationError(
            "Marker positions along the constructed centerline are not increasing."
        )

    provenance = {
        "method": METHOD_MARKER_POLYLINE,
        "follows_mapped_river": False,
        "route_length_m": centerline.length,
        # Markers are vertices of this line, so their offset is zero by
        # construction - it is not evidence of anything here.
        "marker_max_offset_m": 0.0,
        "markers_in_order": True,
        "marker_count": len(markers),
        "length_gate_applied": False,
        "approximation_note": (
            "Straight segments between kilometre markers cut the corner on bends, "
            "so this centerline is slightly shorter than the mapped river."
        ),
        "source_sha256": source_sha256,
        **_length_deviation(centerline.length, km_start, km_end),
    }

    return Reach(
        river=river,
        km_start=float(km_start),
        km_end=float(km_end),
        centerline=centerline,
        marker_km=tuple(float(km) for km in marker_km),
        marker_positions=positions,
        provenance=provenance,
    )


def build_reach(
    sources: RiverSources,
    river: str,
    km_start: float,
    km_end: float,
    *,
    marker_tolerance: float = 20,
    method: str = METHOD_ROUTED,
) -> Reach:
    """Resolve and validate a reach from the configured sources.

    ``method`` selects how the centerline is produced - see the module
    docstring. ``marker_polyline`` needs no mapped-river source.
    """
    validate_bounds(river, km_start, km_end, available_rivers(sources))
    markers: gpd.GeoDataFrame = read_markers(sources, river, km_start, km_end)

    if method == METHOD_MARKER_POLYLINE:
        return build_reach_from_markers(
            river,
            km_start,
            km_end,
            list(markers.geometry),
            list(markers[sources.marker_km_field]),
            source_sha256=sources.markers_sha256(),
        )

    if method != METHOD_ROUTED:
        raise PreparationError(
            f"Unknown centerline method {method!r}. "
            f"Use {METHOD_ROUTED!r} or {METHOD_MARKER_POLYLINE!r}."
        )

    lines: gpd.GeoDataFrame = read_river_lines(sources, river)
    return build_reach_from_geometries(
        river,
        km_start,
        km_end,
        list(lines.geometry),
        list(markers.geometry),
        list(markers[sources.marker_km_field]),
        source_sha256=sources.source_sha256(),
        marker_tolerance=marker_tolerance,
    )
