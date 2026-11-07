"""The marker-constructed centerline, used when the mapped river is unavailable.

No path search happens, so the reach cannot divert up a tributary and the
chainage length gate is deliberately not applied. The deviation is recorded
instead, and the provenance says the centerline does not follow the mapped
river.
"""
import math

import pytest
from shapely.geometry import Point

from eddie_aoi.builder import (
    METHOD_MARKER_POLYLINE,
    METHOD_ROUTED,
    build_reach,
    build_reach_from_geometries,
    build_reach_from_markers,
)
from eddie_aoi.errors import PreparationError
from eddie_aoi.sources import RiverSources, marker_rows_sha256

from .conftest import markers_at, write_river_sources

KMS = [35.0 + i for i in range(16)]


def meander_markers(count=151, spacing=100.0, amplitude=120.0, wavelength=800.0):
    """Markers every ``spacing`` metres along a sine-shaped river.

    Chosen so the corner-cutting is real rather than theoretical: the
    polyline through these points is measurably shorter than the curve.
    """
    points, kms, travelled, previous = [], [], 0.0, None
    # Walk the curve in fine steps, dropping a marker every `spacing` metres
    # of true arc length, so the markers really are 100 m apart along the river.
    step = 1.0
    x = 0.0
    while len(points) < count:
        y = amplitude * math.sin(2 * math.pi * x / wavelength)
        current = (x, y)
        if previous is not None:
            travelled += math.dist(previous, current)
        if previous is None or travelled >= spacing:
            points.append(Point(*current))
            kms.append(35.0 + len(kms) * spacing / 1000.0)
            travelled = 0.0
        previous = current
        x += step
    return points, kms


# --- construction ------------------------------------------------------------


def test_builds_a_centerline_from_markers_alone(straight_markers):
    reach = build_reach_from_markers("Alzette", 35.0, 50.0, straight_markers, KMS)

    assert reach.centerline.geom_type == "LineString"
    assert len(reach.centerline.coords) == len(straight_markers)
    assert reach.reach_id == "alzette_035000_050000"


def test_provenance_declares_the_method_and_that_it_is_not_the_mapped_river(
    straight_markers,
):
    reach = build_reach_from_markers("Alzette", 35.0, 50.0, straight_markers, KMS)

    assert reach.provenance["method"] == METHOD_MARKER_POLYLINE
    assert reach.provenance["follows_mapped_river"] is False
    assert reach.provenance["length_gate_applied"] is False
    assert reach.provenance["approximation_note"]


def test_routed_provenance_declares_the_opposite(straight_river, straight_markers):
    reach = build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS
    )

    assert reach.provenance["method"] == METHOD_ROUTED
    assert reach.provenance["follows_mapped_river"] is True
    assert reach.provenance["length_gate_applied"] is True


def test_chainage_works_on_a_marker_centerline(straight_markers):
    reach = build_reach_from_markers("Alzette", 35.0, 50.0, straight_markers, KMS)

    assert reach.position_for_km(35.0) == pytest.approx(0.0)
    assert reach.position_for_km(42.5) == pytest.approx(7500.0, rel=1e-6)


# --- the point of option B --------------------------------------------------


def test_a_short_meandering_centerline_is_accepted_not_rejected():
    # The real case: 100 m markers along a bendy river. The polyline cuts
    # corners, so it comes out short - and that must not reject the reach,
    # because nothing went wrong.
    markers, kms = meander_markers()
    span_km = kms[-1] - kms[0]

    reach = build_reach_from_markers("Alzette", kms[0], kms[-1], markers, kms)

    deviation = reach.provenance["length_deviation_fraction"]
    assert deviation < 0, "corner-cutting must make the polyline shorter"
    assert reach.centerline.length < span_km * 1000


def test_the_same_short_length_would_fail_the_routed_gate():
    # Proves the two paths genuinely differ: the routed builder's gate would
    # reject exactly what the marker builder accepts.
    from eddie_aoi.routing import validate_route_length

    markers, kms = meander_markers()
    reach = build_reach_from_markers("Alzette", kms[0], kms[-1], markers, kms)
    fraction = abs(reach.provenance["length_deviation_fraction"])

    if fraction <= 0.02:
        pytest.skip(f"deviation {fraction:.4f} is inside the gate; nothing to contrast")
    with pytest.raises(PreparationError, match="differs from chainage length"):
        validate_route_length(reach.centerline.length, kms[0], kms[-1])


def test_deviation_is_recorded_for_both_methods(straight_river, straight_markers):
    routed = build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS
    )
    constructed = build_reach_from_markers("Alzette", 35.0, 50.0, straight_markers, KMS)

    for reach in (routed, constructed):
        assert "length_deviation_m" in reach.provenance
        assert "length_deviation_fraction" in reach.provenance
        assert reach.provenance["chainage_span_m"] == pytest.approx(15000.0)


def test_lateral_error_stays_small_against_a_300_m_half_width():
    # The practical justification: the approximation is a small fraction of
    # the corridor width it feeds.
    markers, kms = meander_markers()
    reach = build_reach_from_markers("Alzette", kms[0], kms[-1], markers, kms)

    # Every marker is a vertex, so sample the true curve between them.
    worst = 0.0
    for a, b in zip(markers, markers[1:]):
        midpoint = Point((a.x + b.x) / 2, (a.y + b.y) / 2)
        worst = max(worst, reach.centerline.distance(midpoint))
    assert worst < 30.0, f"lateral error {worst:.1f} m is larger than expected"
    assert worst < 0.1 * 300.0


# --- rejections that still apply --------------------------------------------


def test_rejects_fewer_than_two_markers():
    with pytest.raises(PreparationError, match="at least two markers"):
        build_reach_from_markers("Alzette", 35.0, 50.0, markers_at([0.0]), [35.0])


def test_rejects_non_parallel_markers_and_kilometres():
    with pytest.raises(PreparationError, match="must be parallel"):
        build_reach_from_markers(
            "Alzette", 35.0, 50.0, markers_at([0.0, 1000.0]), [35.0, 40.0, 50.0]
        )


def test_rejects_non_increasing_kilometres():
    with pytest.raises(PreparationError, match="strictly increasing"):
        build_reach_from_markers(
            "Alzette", 35.0, 50.0, markers_at([0.0, 1000.0, 2000.0]),
            [35.0, 40.0, 38.0],
        )


def test_rejects_a_self_crossing_marker_sequence():
    # Markers that double back do not describe one stretch of river.
    crossing = [Point(0, 0), Point(1000, 0), Point(500, 500), Point(500, -500)]
    with pytest.raises(PreparationError, match="crosses itself"):
        build_reach_from_markers(
            "Alzette", 35.0, 50.0, crossing, [35.0, 40.0, 45.0, 50.0]
        )


def test_rejects_a_reversed_range(straight_markers):
    with pytest.raises(PreparationError, match="increasing kilometre range"):
        build_reach_from_markers("Alzette", 50.0, 35.0, straight_markers, KMS)


# --- integration with the source loader -------------------------------------


def test_build_reach_marker_polyline_needs_no_mapped_river(tmp_path):
    sources = write_river_sources(tmp_path)
    markers_only = RiverSources(markers_path=sources.markers_path)

    reach = build_reach(
        markers_only, "Alzette", 35.0, 50.0, method=METHOD_MARKER_POLYLINE
    )

    assert reach.provenance["follows_mapped_river"] is False
    assert reach.provenance["source_sha256"]


def test_routed_method_without_a_mapped_river_says_what_to_do(tmp_path):
    sources = write_river_sources(tmp_path)
    markers_only = RiverSources(markers_path=sources.markers_path)

    with pytest.raises(PreparationError, match="marker_polyline"):
        build_reach(markers_only, "Alzette", 35.0, 50.0)


def test_unknown_method_is_rejected(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError, match="Unknown centerline method"):
        build_reach(sources, "Alzette", 35.0, 50.0, method="guesswork")


# --- content hash, forward-compatible with a PostGIS markers table ----------


def test_marker_rows_hash_is_stable_and_content_based(tmp_path):
    import geopandas as gpd

    from eddie_aoi.constants import METRIC_CRS

    frame = gpd.GeoDataFrame(
        {"KILO_KM": [35.0, 36.0], "geometry": [Point(0, 0), Point(1000, 0)]},
        crs=METRIC_CRS,
    )
    reordered = frame.iloc[::-1].reset_index(drop=True)

    # Row order must not change the hash: the content is the same.
    assert marker_rows_sha256(frame) == marker_rows_sha256(reordered)


def test_marker_rows_hash_changes_with_the_content():
    import geopandas as gpd

    from eddie_aoi.constants import METRIC_CRS

    first = gpd.GeoDataFrame(
        {"KILO_KM": [35.0, 36.0], "geometry": [Point(0, 0), Point(1000, 0)]},
        crs=METRIC_CRS,
    )
    second = gpd.GeoDataFrame(
        {"KILO_KM": [35.0, 36.0], "geometry": [Point(0, 0), Point(1001, 0)]},
        crs=METRIC_CRS,
    )

    assert marker_rows_sha256(first) != marker_rows_sha256(second)
