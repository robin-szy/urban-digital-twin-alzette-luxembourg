"""Computed variants: the provider interface, corridor, bbox, buffer, registry."""
import json

import pytest

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.errors import PreparationError
from eddie_aoi.variants import (
    BboxVariant,
    BufferVariant,
    CorridorVariant,
    VariantRegistry,
    default_registry,
    variant_key,
)

KMS = [35.0 + i for i in range(16)]


@pytest.fixture
def reach(straight_river, straight_markers):
    return build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )


# --- 5.1 the provider interface ---------------------------------------------


def test_build_returns_a_single_row_frame_in_the_metric_crs(reach):
    frame = CorridorVariant().build(reach, half_width_m=300)

    assert len(frame) == 1
    assert frame.crs.to_epsg() == 2169
    assert METRIC_CRS == "EPSG:2169"
    assert frame.geometry.iloc[0].geom_type in ("Polygon", "MultiPolygon")


def test_build_result_carries_its_provenance(reach):
    frame = CorridorVariant().build(reach, half_width_m=300)
    row = frame.iloc[0]

    assert row["variant"] == "corridor"
    assert row["variant_key"] == "corridor_300"
    assert json.loads(row["params"]) == {"half_width_m": 300.0}
    assert row["reach_id"] == "alzette_035000_050000"
    assert row["source_sha256"] == "sha-one"


def test_computed_and_lookup_variants_share_the_interface(reach):
    # Every provider answers availability() and build() the same way.
    for provider in (CorridorVariant(), BboxVariant(), BufferVariant()):
        assert provider.availability().available is True
        frame = provider.build(reach)
        assert set(
            ["variant", "variant_key", "params", "reach_id", "source_sha256"]
        ).issubset(frame.columns)


# --- 5.2 corridor ------------------------------------------------------------


def test_corridor_extends_the_half_width_each_side(reach):
    geometry = CorridorVariant().build(reach, half_width_m=300).geometry.iloc[0]
    minx, miny, maxx, maxy = geometry.bounds

    # The fixture river runs along y=0 from x=0 to x=15000.
    assert miny == pytest.approx(-300.0, abs=0.5)
    assert maxy == pytest.approx(300.0, abs=0.5)


def test_corridor_terminates_at_the_reach_endpoints(reach):
    geometry = CorridorVariant().build(reach, half_width_m=300).geometry.iloc[0]
    minx, _, maxx, _ = geometry.bounds

    # Flat caps: no half-disc of 300 m beyond either end.
    assert minx == pytest.approx(0.0, abs=0.5)
    assert maxx == pytest.approx(15000.0, abs=0.5)


def test_corridor_area_is_about_length_times_width(reach):
    geometry = CorridorVariant().build(reach, half_width_m=300).geometry.iloc[0]
    assert geometry.area == pytest.approx(15000.0 * 600.0, rel=0.01)


def test_corridor_default_is_300_m(reach):
    default = CorridorVariant().build(reach).geometry.iloc[0]
    explicit = CorridorVariant().build(reach, half_width_m=300).geometry.iloc[0]
    assert default.equals(explicit)


# --- 5.3 bbox and buffer ----------------------------------------------------


def test_bbox_is_the_centerline_bounds_plus_the_margin(reach):
    geometry = BboxVariant().build(reach, margin_m=300).geometry.iloc[0]

    assert geometry.bounds == pytest.approx((-300.0, -300.0, 15300.0, 300.0))


def test_bbox_is_rectangular(reach):
    geometry = BboxVariant().build(reach, margin_m=300).geometry.iloc[0]
    minx, miny, maxx, maxy = geometry.bounds
    assert geometry.area == pytest.approx((maxx - minx) * (maxy - miny))


def test_buffer_extends_past_the_endpoints(reach):
    geometry = BufferVariant().build(reach, d=1000).geometry.iloc[0]
    minx, _, maxx, _ = geometry.bounds

    # Round caps, unlike the corridor: this is what catches a gauge sitting
    # just beyond the end of the reach.
    assert minx == pytest.approx(-1000.0, abs=1.0)
    assert maxx == pytest.approx(16000.0, abs=1.0)


def test_corridor_and_buffer_differ_at_the_ends(reach):
    corridor = CorridorVariant().build(reach, half_width_m=300).geometry.iloc[0]
    rounded = BufferVariant().build(reach, d=300).geometry.iloc[0]

    assert not corridor.equals(rounded)
    assert rounded.area > corridor.area


@pytest.mark.parametrize("bad", [0, -1, -300.0, float("nan"), float("inf")])
def test_rejects_a_non_positive_or_non_finite_half_width(reach, bad):
    with pytest.raises(PreparationError):
        CorridorVariant().build(reach, half_width_m=bad)


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf")])
def test_rejects_a_bad_bbox_margin(reach, bad):
    with pytest.raises(PreparationError):
        BboxVariant().build(reach, margin_m=bad)


@pytest.mark.parametrize("bad", ["300", None, True, [300]])
def test_rejects_a_non_numeric_distance(reach, bad):
    with pytest.raises(PreparationError, match="must be a number"):
        BufferVariant().build(reach, d=bad)


# --- 5.4 registry -----------------------------------------------------------


def test_default_registry_holds_every_variant():
    assert default_registry().names() == [
        "bbox",
        "buffer",
        "corridor",
        "hq100",
        "hq_extreme",
        "watersheds",
    ]


def test_registry_builds_by_name(reach):
    frame = default_registry().build("corridor", reach, half_width_m=250)
    assert frame.iloc[0]["variant_key"] == "corridor_250"


def test_unknown_variant_name_lists_the_registered_ones(reach):
    with pytest.raises(PreparationError) as excinfo:
        default_registry().get("floodplain")

    message = str(excinfo.value)
    assert "Unknown AOI variant 'floodplain'" in message
    assert "corridor" in message and "bbox" in message and "buffer" in message


def test_empty_registry_still_reports_usefully():
    with pytest.raises(PreparationError, match=r"Registered: \[\]"):
        VariantRegistry().get("corridor")


def test_registry_reports_availability_of_the_computed_variants():
    availability = default_registry().availability()
    # The computed ones need no external data, so they are always available.
    for name in ("bbox", "buffer", "corridor"):
        assert availability[name].available is True


# --- variant keys ------------------------------------------------------------


def test_variant_key_shapes():
    assert variant_key("corridor", {"half_width_m": 300.0}) == "corridor_300"
    assert variant_key("buffer", {"d": 1000}) == "buffer_1000"
    assert variant_key("watersheds", {}) == "watersheds"
    assert variant_key("watersheds") == "watersheds"


def test_variant_key_distinguishes_parameters():
    # Parameters are part of the key: a 500 m corridor must never be served
    # to a caller who asked for 300 m.
    assert variant_key("corridor", {"half_width_m": 300}) != variant_key(
        "corridor", {"half_width_m": 500}
    )


def test_variant_key_is_order_independent():
    assert variant_key("x", {"a": 1, "b": 2}) == variant_key("x", {"b": 2, "a": 1})
