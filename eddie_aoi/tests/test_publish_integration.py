"""Serving a reach's AOI as GeoJSON and as a Terria catalog fragment."""
import json

import pytest

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.publish import (
    PAYLOAD_WARN_BYTES,
    VARIANT_STYLES,
    catalog_fragment,
    variants_geojson,
)
from eddie_aoi.store import save_reach
from eddie_aoi.variant_store import get_or_build_variant
from eddie_aoi.variants import default_registry

pytestmark = pytest.mark.integration

KMS = [35.0 + i for i in range(16)]
BASE = "http://localhost:5000"


@pytest.fixture
def reach(conn, straight_river, straight_markers):
    r = build_reach_from_geometries(
        "Testbach", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )
    save_reach(conn, r)
    return r


@pytest.fixture
def populated(conn, reach):
    registry = default_registry()
    for name, params in [
        ("corridor", {"half_width_m": 300.0}),
        ("buffer", {"d": 1000.0}),
        ("bbox", {"margin_m": 300.0}),
        ("watersheds", {}),
    ]:
        get_or_build_variant(conn, reach, registry, name, **params)
    return reach


# --- GeoJSON ----------------------------------------------------------------


def test_unknown_reach_returns_none(conn):
    assert variants_geojson(conn, "nosuch_000000_000001") is None


def test_collection_shape_and_crs(conn, populated):
    fc = variants_geojson(conn, populated.reach_id)

    assert fc["type"] == "FeatureCollection"
    # centerline + 4 variants
    assert len(fc["features"]) == 5
    for feature in fc["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] in (
            "LineString", "Polygon", "MultiPolygon"
        )


def test_coordinates_are_geographic(conn, populated):
    # The publish boundary: 2169 metres in, 4326 degrees out.
    fc = variants_geojson(conn, populated.reach_id, variant_key="corridor_300")
    coords = fc["features"][0]["geometry"]["coordinates"][0]

    for x, y in coords[:20]:
        assert -180 <= x <= 180, f"longitude out of range: {x}"
        assert -90 <= y <= 90, f"latitude out of range: {y}"


def test_metric_area_is_reported_not_recomputed_in_degrees(conn, populated):
    fc = variants_geojson(conn, populated.reach_id, variant_key="corridor_300")
    props = fc["features"][0]["properties"]

    # 15 km x 600 m, measured in the metric CRS before reprojecting.
    assert props["area_km2"] == pytest.approx(9.0, rel=0.02)


def test_narrowing_to_one_variant(conn, populated):
    fc = variants_geojson(conn, populated.reach_id, variant_key="buffer_1000")

    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["variant_key"] == "buffer_1000"


def test_provenance_travels_on_the_feature(conn, populated):
    fc = variants_geojson(conn, populated.reach_id, variant_key="corridor_300")
    props = fc["features"][0]["properties"]

    for key in ("variant", "variant_key", "requested_variant", "reach_id",
                "river", "km_start", "km_end", "degraded", "source_sha256"):
        assert key in props, f"missing {key}"


def test_degraded_variant_is_flagged_on_the_feature(conn, populated):
    fc = variants_geojson(conn, populated.reach_id)
    degraded = [
        f for f in fc["features"] if f["properties"].get("degraded") is True
    ]

    assert len(degraded) == 1, "watersheds should be the only substitute"
    props = degraded[0]["properties"]
    assert props["requested_variant"] == "watersheds"
    assert props["variant"] == "buffer"
    assert "has not been supplied yet" in props["degraded_reason"]


def test_centerline_carries_its_method(conn, populated):
    fc = variants_geojson(conn, populated.reach_id)
    line = next(
        f for f in fc["features"] if f["properties"]["variant"] == "centerline"
    )

    assert line["geometry"]["type"] == "LineString"
    assert line["properties"]["method"] == "routed"
    assert line["properties"]["follows_mapped_river"] is True


def test_aoi_bands_stay_well_under_the_warning_size(conn, populated):
    for key in ("corridor_300", "buffer_1000", "bbox_300"):
        size = len(json.dumps(variants_geojson(conn, populated.reach_id, key)).encode())
        assert size < PAYLOAD_WARN_BYTES, f"{key} is {size} bytes"


# --- Terria catalog fragment ------------------------------------------------


def test_unknown_reach_has_no_fragment(conn):
    assert catalog_fragment(conn, "nosuch_000000_000001", BASE) is None


def test_fragment_shape(conn, populated):
    fragment = catalog_fragment(conn, populated.reach_id, BASE)
    group = fragment["catalog"][0]

    assert group["type"] == "group"
    assert "Testbach" in group["name"] and "35.0-50.0" in group["name"]
    # centerline + 4 variants
    assert len(group["members"]) == 5


def test_every_member_is_a_styled_geojson_item(conn, populated):
    group = catalog_fragment(conn, populated.reach_id, BASE)["catalog"][0]

    for member in group["members"]:
        assert member["type"] == "geojson"
        assert member["url"].startswith(f"{BASE}/aoi/reach/{populated.reach_id}/")
        assert "variant_key=" in member["url"]
        assert member["style"], f"{member['id']} has no style"


def test_corridor_is_shown_by_default(conn, populated):
    group = catalog_fragment(conn, populated.reach_id, BASE)["catalog"][0]
    shown = [m["id"] for m in group["members"] if m.get("show")]

    assert any("corridor" in i for i in shown)
    assert not any("bbox" in i for i in shown), "a bbox must not be on by default"


def test_degraded_member_is_named_and_described_as_a_substitute(conn, populated):
    group = catalog_fragment(conn, populated.reach_id, BASE)["catalog"][0]
    member = next(m for m in group["members"] if "watersheds" in m["id"])

    assert "SUBSTITUTE" in member["name"]
    assert "not watersheds" in member["name"]
    assert "This is not the watersheds area" in member["description"]
    assert member["style"] != VARIANT_STYLES["watersheds"], "should use the warning style"


def test_group_warns_when_anything_is_degraded(conn, populated):
    group = catalog_fragment(conn, populated.reach_id, BASE)["catalog"][0]
    assert "substitutes" in group.get("description", "")


def test_no_warning_when_nothing_is_degraded(conn, reach, tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    from eddie_aoi.constants import METRIC_CRS

    gpd.GeoDataFrame(
        [{"NAME": "ws", "geometry": box(0, -500, 15000, 500)}], crs=METRIC_CRS
    ).to_file(tmp_path / "ws.gpkg", driver="GPKG")
    registry = default_registry(watersheds_path=tmp_path / "ws.gpkg")
    get_or_build_variant(conn, reach, registry, "watersheds")

    group = catalog_fragment(conn, reach.reach_id, BASE)["catalog"][0]
    assert "description" not in group
    assert not any("SUBSTITUTE" in m["name"] for m in group["members"])


def test_base_url_trailing_slash_is_tolerated(conn, populated):
    group = catalog_fragment(conn, populated.reach_id, BASE + "/")["catalog"][0]
    assert "//aoi/" not in group["members"][0]["url"]


def test_fragment_lists_default_layers_on_the_workbench(conn, populated):
    # TerriaJS v8 only loads items onto the workbench from this list; "show"
    # alone leaves them in the catalogue, which is what happened in the map.
    fragment = catalog_fragment(conn, populated.reach_id, BASE)

    assert "workbench" in fragment
    ids = fragment["workbench"]
    assert any("centerline" in i for i in ids)
    assert any("corridor" in i for i in ids)
    assert not any("bbox" in i for i in ids)


def test_workbench_ids_all_exist_as_members(conn, populated):
    fragment = catalog_fragment(conn, populated.reach_id, BASE)
    member_ids = {m["id"] for m in fragment["catalog"][0]["members"]}

    for item in fragment["workbench"]:
        assert item in member_ids, f"{item} is on the workbench but not a member"


def test_workbench_matches_the_shown_members(conn, populated):
    fragment = catalog_fragment(conn, populated.reach_id, BASE)
    shown = {m["id"] for m in fragment["catalog"][0]["members"] if m.get("show")}

    assert set(fragment["workbench"]) == shown
