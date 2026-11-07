"""Lookup variants, declared availability, and degradation."""
import geopandas as gpd
import pytest
from shapely.geometry import Polygon, box

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.errors import PreparationError
from eddie_aoi.variants import (
    MODE_CLIP,
    MODE_FEATURES,
    HQ100_SELECT_BUFFER_M,
    WATERSHED_FALLBACK_BUFFER_M,
    LookupVariant,
    clear_source_cache,
    default_registry,
    read_source_layer,
    source_cache_info,
)

KMS = [35.0 + i for i in range(16)]


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_source_cache()
    yield
    clear_source_cache()


@pytest.fixture
def reach(straight_river, straight_markers):
    return build_reach_from_geometries(
        "Alzette", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )


def write_layer(path, polygons, crs=METRIC_CRS, names=None):
    rows = [
        {"NAME": (names[i] if names else f"f{i}"), "geometry": geom}
        for i, geom in enumerate(polygons)
    ]
    gpd.GeoDataFrame(rows, crs=crs).to_file(path, driver="GPKG")
    return path


# --- 6.1 selection and dissolve ---------------------------------------------


def test_selects_only_features_related_to_the_reach(tmp_path, reach):
    path = write_layer(
        tmp_path / "ws.gpkg",
        [
            box(0, -500, 7000, 500),        # touches the river
            box(7000, -500, 15000, 500),    # touches the river
            box(90000, 90000, 95000, 95000),  # far away
        ],
    )
    variant = LookupVariant(name="watersheds", source_path=path)

    frame = variant.build(reach)
    geometry = frame.geometry.iloc[0]

    # The two touching boxes, dissolved; the distant one excluded.
    assert geometry.area == pytest.approx(15000.0 * 1000.0, rel=1e-6)
    assert len(frame) == 1


def test_several_related_features_dissolve_to_one_geometry(tmp_path, reach):
    path = write_layer(
        tmp_path / "ws.gpkg",
        [box(0, -500, 8000, 500), box(6000, -500, 15000, 500)],  # overlapping
    )
    frame = LookupVariant(name="watersheds", source_path=path).build(reach)

    geometry = frame.geometry.iloc[0]
    assert geometry.geom_type == "Polygon", "overlapping features must dissolve to one"
    assert geometry.area == pytest.approx(15000.0 * 1000.0, rel=1e-6)


def test_an_empty_selection_is_reported_not_returned(tmp_path, reach):
    path = write_layer(tmp_path / "ws.gpkg", [box(90000, 90000, 95000, 95000)])
    variant = LookupVariant(name="watersheds", source_path=path)

    with pytest.raises(PreparationError, match="nothing related to reach"):
        variant.build(reach)


def test_select_buffer_widens_the_selection(tmp_path, reach):
    # A footprint 800 m off the river: missed by the line, caught by a buffer.
    path = write_layer(tmp_path / "hq.gpkg", [box(1000, 800, 2000, 1200)])

    with pytest.raises(PreparationError, match="nothing related"):
        LookupVariant(name="hq100", source_path=path).build(reach)

    frame = LookupVariant(
        name="hq100", source_path=path, select_buffer_m=2000.0
    ).build(reach)
    assert frame.geometry.iloc[0].area > 0


def test_lookup_result_carries_the_source_hash(tmp_path, reach):
    path = write_layer(tmp_path / "ws.gpkg", [box(0, -500, 15000, 500)])
    variant = LookupVariant(name="watersheds", source_path=path)

    frame = variant.build(reach)
    assert frame.iloc[0]["source_sha256"] == variant.source_sha256()


def test_rejects_a_source_layer_in_the_wrong_crs(tmp_path, reach):
    path = write_layer(
        tmp_path / "ws.gpkg", [Polygon([(6, 49), (6.2, 49), (6.2, 49.2)])],
        crs="EPSG:4326",
    )
    with pytest.raises(PreparationError, match="must declare EPSG:2169"):
        LookupVariant(name="watersheds", source_path=path).build(reach)


# --- 6.2 configurable source path -------------------------------------------


def test_relocating_the_configured_path_changes_where_it_reads(tmp_path, reach):
    first = write_layer(tmp_path / "a.gpkg", [box(0, -500, 15000, 500)])
    second = write_layer(tmp_path / "b.gpkg", [box(0, -100, 15000, 100)])

    area_a = LookupVariant(name="hq100", source_path=first).build(reach).geometry.iloc[0].area
    area_b = LookupVariant(name="hq100", source_path=second).build(reach).geometry.iloc[0].area

    assert area_a != area_b
    assert area_b == pytest.approx(15000.0 * 200.0, rel=1e-6)


def test_hq100_is_available_once_its_source_exists(tmp_path):
    path = write_layer(tmp_path / "hq.gpkg", [box(0, -500, 15000, 500)])
    registry = default_registry(hq100_path=path)

    assert registry.availability()["hq100"].available is True


def test_hq100_default_select_buffer_is_configured():
    assert HQ100_SELECT_BUFFER_M == 2000.0


# --- 6.3 declared availability ----------------------------------------------


def test_unconfigured_variants_declare_themselves_unavailable_with_a_reason():
    availability = default_registry().availability()

    assert availability["corridor"].available is True
    assert availability["bbox"].available is True
    assert availability["buffer"].available is True

    for name in ("watersheds", "hq100", "hq_extreme"):
        state = availability[name]
        assert state.available is False
        assert state.reason, f"{name} must say why it is unavailable"

    assert "has not been supplied yet" in availability["watersheds"].reason
    assert "not been obtained yet" in availability["hq_extreme"].reason


def test_a_configured_but_missing_file_is_unavailable(tmp_path):
    registry = default_registry(watersheds_path=tmp_path / "absent.gpkg")
    state = registry.availability()["watersheds"]

    assert state.available is False
    assert "is not present" in state.reason


def test_availability_is_declared_before_any_build(tmp_path):
    # The point of declaring it: a caller learns the variant is unusable
    # without first attempting the work.
    variant = LookupVariant(name="watersheds", source_path=None)
    assert variant.availability().available is False

    with pytest.raises(PreparationError, match="No source layer configured"):
        variant.build(object())


# --- 6.4 and 6.5 degradation ------------------------------------------------


def test_watersheds_degrades_to_the_declared_fallback(reach):
    resolution = default_registry().resolve_variant("watersheds", reach)

    assert resolution.requested == "watersheds"
    assert resolution.actual == "buffer"
    assert resolution.degraded is True
    assert "has not been supplied yet" in resolution.reason


def test_the_degraded_fallback_is_river_km_plus_margin(reach):
    resolution = default_registry().resolve_variant("watersheds", reach)
    geometry = resolution.frame.geometry.iloc[0]

    expected = reach.centerline.buffer(WATERSHED_FALLBACK_BUFFER_M, quad_segs=64)
    assert geometry.area == pytest.approx(expected.area, rel=1e-9)


def test_every_degraded_result_carries_all_four_fields(reach):
    frame = default_registry().resolve_variant("watersheds", reach).frame
    row = frame.iloc[0]

    assert row["requested_variant"] == "watersheds"
    assert row["variant"] == "buffer"
    assert bool(row["degraded"]) is True
    assert row["degraded_reason"]


def test_an_undegraded_result_says_so(reach):
    resolution = default_registry().resolve_variant("corridor", reach, half_width_m=300)
    row = resolution.frame.iloc[0]

    assert resolution.degraded is False
    assert resolution.requested == resolution.actual == "corridor"
    assert bool(row["degraded"]) is False
    assert row["degraded_reason"] is None


def test_a_variant_without_a_fallback_fails_rather_than_substituting(reach):
    # hq100 and hq_extreme have no defensible stand-in.
    for name in ("hq100", "hq_extreme"):
        with pytest.raises(PreparationError, match="no configured fallback"):
            default_registry().resolve_variant(name, reach)


def test_resolution_uses_the_real_source_once_available(tmp_path, reach):
    path = write_layer(tmp_path / "ws.gpkg", [box(0, -500, 15000, 500)])
    resolution = default_registry(watersheds_path=path).resolve_variant(
        "watersheds", reach
    )

    assert resolution.degraded is False
    assert resolution.actual == "watersheds"


def test_a_fallback_must_itself_be_registered():
    registry = default_registry()
    with pytest.raises(PreparationError, match="Unknown AOI variant"):
        registry.set_fallback("watersheds", "nonexistent")


# --- 7.4 shared source-layer read cache -------------------------------------


def test_two_lookup_variants_over_one_source_read_it_once(tmp_path, reach):
    path = write_layer(tmp_path / "shared.gpkg", [box(0, -500, 15000, 500)])
    first = LookupVariant(name="watersheds", source_path=path)
    second = LookupVariant(name="hq100", source_path=path, select_buffer_m=100.0)

    first.build(reach)
    second.build(reach)

    info = source_cache_info()
    assert info.misses == 1, "the shared layer must be read once"
    assert info.hits >= 1


def test_a_replaced_source_file_is_re_read(tmp_path, reach):
    path = tmp_path / "shared.gpkg"
    write_layer(path, [box(0, -500, 15000, 500)])
    variant = LookupVariant(name="watersheds", source_path=path)
    before = variant.build(reach).geometry.iloc[0].area

    write_layer(path, [box(0, -100, 15000, 100)])
    after = variant.build(reach).geometry.iloc[0].area

    assert after != before, "a replaced file must not be served from memory"


def test_source_layer_read_returns_an_indexed_frame(tmp_path):
    path = write_layer(tmp_path / "x.gpkg", [box(0, 0, 1, 1)])
    frame = read_source_layer(path)
    assert frame.sindex is not None


# --- selection modes --------------------------------------------------------


def test_features_mode_keeps_whole_features(tmp_path, reach):
    # A watershed extends well beyond the river; taking it entire is the point.
    path = write_layer(tmp_path / "ws.gpkg", [box(-5000, -4000, 20000, 4000)])

    frame = LookupVariant(
        name="watersheds", source_path=path, mode=MODE_FEATURES
    ).build(reach)

    assert frame.geometry.iloc[0].area == pytest.approx(25000.0 * 8000.0, rel=1e-6)


def test_clip_mode_restricts_to_the_reach_neighbourhood(tmp_path, reach):
    # The same oversized feature, clipped: only the neighbourhood survives.
    path = write_layer(tmp_path / "hq.gpkg", [box(-5000, -4000, 20000, 4000)])

    frame = LookupVariant(
        name="hq100", source_path=path, select_buffer_m=2000.0, mode=MODE_CLIP
    ).build(reach)

    geometry = frame.geometry.iloc[0]
    selector = reach.centerline.buffer(2000.0, quad_segs=64)
    assert geometry.area < selector.area * 1.0001
    assert geometry.area == pytest.approx(selector.area, rel=1e-6)


def test_the_two_modes_differ_on_an_oversized_feature(tmp_path, reach):
    path = write_layer(tmp_path / "x.gpkg", [box(-5000, -4000, 20000, 4000)])

    whole = LookupVariant(
        name="watersheds", source_path=path, mode=MODE_FEATURES
    ).build(reach).geometry.iloc[0].area
    clipped = LookupVariant(
        name="hq100", source_path=path, select_buffer_m=2000.0, mode=MODE_CLIP
    ).build(reach).geometry.iloc[0].area

    assert clipped < whole


def test_mode_is_recorded_on_the_result(tmp_path, reach):
    import json

    path = write_layer(tmp_path / "ws.gpkg", [box(0, -500, 15000, 500)])
    frame = LookupVariant(name="watersheds", source_path=path).build(reach)

    assert json.loads(frame.iloc[0]["params"])["mode"] == MODE_FEATURES


def test_registry_declares_the_modes():
    registry = default_registry()
    assert registry.get("watersheds").mode == MODE_FEATURES
    assert registry.get("hq100").mode == MODE_CLIP
    assert registry.get("hq_extreme").mode == MODE_CLIP


def test_an_unknown_mode_is_rejected_at_declaration():
    with pytest.raises(PreparationError, match="Unknown selection mode"):
        LookupVariant(name="x", mode="sometimes")


def test_clip_mode_requires_a_neighbourhood_to_clip_against():
    with pytest.raises(PreparationError, match="needs a positive select_buffer_m"):
        LookupVariant(name="x", mode=MODE_CLIP)


def test_multipart_rows_are_exploded_before_selection(tmp_path, reach):
    # One row holding two polygons, only one of which relates to the reach.
    from shapely.geometry import MultiPolygon

    multi = MultiPolygon([box(0, -500, 15000, 500), box(90000, 90000, 95000, 95000)])
    gpd.GeoDataFrame([{"NAME": "both", "geometry": multi}], crs=METRIC_CRS).to_file(
        tmp_path / "m.gpkg", driver="GPKG"
    )

    frame = LookupVariant(
        name="watersheds", source_path=tmp_path / "m.gpkg"
    ).build(reach)

    # The distant part must be excluded even though it shares a row.
    assert frame.geometry.iloc[0].area == pytest.approx(15000.0 * 1000.0, rel=1e-6)


def test_invalid_source_geometry_is_repaired(tmp_path, reach):
    # A bowtie: self-intersecting, so invalid as read.
    bowtie = Polygon([(0, -500), (15000, 500), (15000, -500), (0, 500)])
    gpd.GeoDataFrame([{"NAME": "bad", "geometry": bowtie}], crs=METRIC_CRS).to_file(
        tmp_path / "bad.gpkg", driver="GPKG"
    )
    assert not read_source_layer(tmp_path / "bad.gpkg").is_valid.all()

    frame = LookupVariant(
        name="watersheds", source_path=tmp_path / "bad.gpkg"
    ).build(reach)

    assert frame.geometry.iloc[0].is_valid, "the result must be valid geometry"
