"""The reach model, its construction from geometry, and chainage lookup."""
import pytest
from shapely.geometry import LineString, Point

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import Reach

from .conftest import markers_at

KMS = [35.0 + i for i in range(16)]  # km 35..50


@pytest.fixture
def reach(straight_river, straight_markers):
    return build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS
    )


# --- construction ------------------------------------------------------------


def test_builds_a_reach_from_fixture_geometry(reach):
    assert reach.river == "Alzette"
    assert reach.reach_id == "alzette_035000_050000"
    assert reach.crs == METRIC_CRS
    assert reach.centerline.geom_type == "LineString"
    assert reach.length_m == pytest.approx(15000.0)


def test_records_validation_provenance(reach):
    assert reach.provenance["route_length_m"] == pytest.approx(15000.0)
    assert reach.provenance["marker_max_offset_m"] == pytest.approx(2.0)
    assert reach.provenance["markers_in_order"] is True
    assert reach.provenance["marker_count"] == 16


def test_carries_the_source_hash_when_given(straight_river, straight_markers):
    built = build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="deadbeef",
    )
    assert built.provenance["source_sha256"] == "deadbeef"


def test_rejects_a_reach_whose_route_length_disagrees_with_chainage(
    straight_river, straight_markers
):
    # The geometry is 15 km but km 35-40 claims 5 km.
    with pytest.raises(PreparationError, match="differs from chainage length"):
        build_reach_from_geometries(
            "Alzette", 35.0, 40.0, straight_river, straight_markers, KMS
        )


def test_rejects_a_reach_with_a_non_increasing_range(straight_river, straight_markers):
    with pytest.raises(PreparationError, match="increasing kilometre range"):
        build_reach_from_geometries(
            "Alzette", 50.0, 35.0, straight_river, straight_markers, KMS
        )


def test_rejects_a_reach_whose_markers_are_out_of_order(straight_river):
    markers = markers_at([0.0, 1000.0, 3000.0, 2000.0, 15000.0])
    with pytest.raises(PreparationError, match="within tolerance and in order"):
        build_reach_from_geometries(
            "Alzette", 35.0, 50.0, straight_river, markers, [35.0, 36.0, 37.0, 38.0, 50.0]
        )


# --- model invariants --------------------------------------------------------


def test_centerline_must_be_a_linestring():
    with pytest.raises(PreparationError, match="must be a LineString"):
        Reach(
            river="Alzette",
            km_start=35.0,
            km_end=50.0,
            centerline=Point(0, 0),
            marker_km=(35.0, 50.0),
            marker_positions=(0.0, 15000.0),
        )


def test_chainage_arrays_must_be_parallel():
    with pytest.raises(PreparationError, match="parallel"):
        Reach(
            river="Alzette",
            km_start=35.0,
            km_end=50.0,
            centerline=LineString([(0, 0), (15000, 0)]),
            marker_km=(35.0, 40.0, 50.0),
            marker_positions=(0.0, 15000.0),
        )


def test_at_least_two_chainage_markers_are_required():
    with pytest.raises(PreparationError, match="at least two"):
        Reach(
            river="Alzette",
            km_start=35.0,
            km_end=50.0,
            centerline=LineString([(0, 0), (15000, 0)]),
            marker_km=(35.0,),
            marker_positions=(0.0,),
        )


def test_chainage_must_increase():
    with pytest.raises(PreparationError, match="kilometres must be increasing"):
        Reach(
            river="Alzette",
            km_start=35.0,
            km_end=50.0,
            centerline=LineString([(0, 0), (15000, 0)]),
            marker_km=(35.0, 34.0, 50.0),
            marker_positions=(0.0, 7000.0, 15000.0),
        )


# --- chainage lookup ---------------------------------------------------------


def test_position_at_a_marker(reach):
    assert reach.position_for_km(35.0) == pytest.approx(0.0)
    assert reach.position_for_km(50.0) == pytest.approx(15000.0)


def test_position_between_markers_is_interpolated(reach):
    assert reach.position_for_km(42.5) == pytest.approx(7500.0)


def test_point_for_km_lies_on_the_centerline(reach):
    point = reach.point_for_km(42.0)
    assert point.x == pytest.approx(7000.0)
    assert reach.centerline.distance(point) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("km", [34.9, 50.1, 0.0, 100.0])
def test_position_outside_the_reach_is_rejected_not_extrapolated(reach, km):
    with pytest.raises(PreparationError, match="outside reach"):
        reach.position_for_km(km)


@pytest.mark.parametrize("km", [float("nan"), float("inf")])
def test_non_finite_chainage_lookup_is_rejected(reach, km):
    with pytest.raises(PreparationError, match="finite"):
        reach.position_for_km(km)
