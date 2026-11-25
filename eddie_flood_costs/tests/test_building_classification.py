"""Synthetic checks of building_classification and scope: no database, no real data."""
import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

from flood_costs import building_classification as bc

CRS = 2169


def gdf(rows, **columns):
    """GeoDataFrame in EPSG:2169 from geometries plus equal-length attribute lists."""
    return gpd.GeoDataFrame({**columns, "geometry": rows}, crs=CRS)


def empty_osm():
    return (gdf([], osm_id=[], type=[]), gdf([], osm_id=[], fclass=[]), gdf([], osm_id=[], fclass=[]))


def landuse(*parts):
    """parts: (geometry, code, label)."""
    return gdf([p[0] for p in parts], LU1_level_3_code=[p[1] for p in parts],
               LU1_level__3_name=[p[2] for p in parts])


def classify_one(footprint, lu=None, osm_buildings=None, points=None, areas=None, reviewed=None, bid="B1"):
    ob, pp, pa = empty_osm()
    result = bc.classify(gdf([footprint], buildingid=[bid]),
                         lu if lu is not None else landuse(),
                         osm_buildings if osm_buildings is not None else ob,
                         points if points is not None else pp, areas if areas is not None else pa, reviewed)
    return result.iloc[0]


SQUARE = box(0, 0, 10, 10)


# --- land use -----------------------------------------------------------------

def test_dominant_landuse_picks_largest_overlap():
    lu = landuse((box(0, 0, 3, 10), 121, "Industry"), (box(3, 0, 10, 10), 111, " Residential "))
    result = bc.dominant_landuse(gdf([SQUARE], buildingid=["B1"]), lu)
    assert result.loc["B1", "landuse_code"] == 111
    assert result.loc["B1", "landuse_label"] == "Residential"
    assert result.loc["B1", "landuse_share"] == pytest.approx(0.7)


def test_landuse_columns_as_lowercased_by_eddie_ingest():
    lu = gdf([box(-5, -5, 20, 20)], lu1_level_3_code=[111], lu1_level__3_name=[" Residential "])
    assert bc.dominant_landuse(gdf([SQUARE], buildingid=["B1"]), lu).loc["B1", "landuse_code"] == 111


def test_landuse_without_the_code_column_fails_clearly():
    with pytest.raises(KeyError, match="lacks columns"):
        bc.dominant_landuse(gdf([SQUARE], buildingid=["B1"]), gdf([box(0, 0, 1, 1)], other=[1]))


def test_landuse_111_gives_residential_proxy():
    row = classify_one(SQUARE, lu=landuse((box(-5, -5, 20, 20), 111, "Residential")))
    assert (row.jrc_category, row.classification_source, row.classification_confidence) == \
        ("Residential", "landuse", "proxy")


def test_no_landuse_and_no_osm_is_unknown_never_zero():
    row = classify_one(SQUARE)
    assert row.jrc_category == "Unknown"
    assert row.classification_status == "unknown"
    assert row.reason_code == "no_reliable_building_use_match"
    assert "no land-use polygon" in row.classification_reason


# --- OSM matching -------------------------------------------------------------

@pytest.mark.parametrize("osm_shape, strong", [
    (box(0, 0, 10, 10), True),          # identical: IoU 1
    (box(0, 0, 10, 8.5), True),         # footprint 85 % covered
    (box(-20, -20, 30, 30), True),      # OSM building covers the whole footprint (>= 80 %)
    (box(0, 0, 10, 3), False),          # only 30 % of the footprint
])
def test_building_match_thresholds(osm_shape, strong):
    evidence = bc.osm_evidence(SQUARE, gdf([osm_shape], osm_id=["w1"], type=["commercial"]), *empty_osm()[1:])
    assert bool(evidence) is strong


def test_large_containing_site_area_is_context_only():
    """A school ground containing the building is not evidence; the same object would be."""
    site = gdf([box(-50, -50, 60, 60)], osm_id=["w9"], fclass=["school"])
    assert bc.osm_evidence(SQUARE, empty_osm()[0], empty_osm()[1], site) == []
    same = gdf([box(0, 0, 10, 10)], osm_id=["w9"], fclass=["school"])
    assert bc.osm_evidence(SQUARE, empty_osm()[0], empty_osm()[1], same)[0]["relation"] == "same_object_overlap"


def test_point_must_be_inside_the_footprint():
    points = gdf([Point(5, 5), Point(30, 30)], osm_id=["n1", "n2"], fclass=["restaurant", "bank"])
    evidence = bc.osm_evidence(SQUARE, empty_osm()[0], points, empty_osm()[2])
    assert [e["osm_id"] for e in evidence] == ["n1"]


# --- rules ----------------------------------------------------------------------

@pytest.mark.parametrize("building_type, category, confidence", [
    ("commercial", "Commercial", "high"), ("office", "Commercial", "high"), ("school", "Commercial", "high"),
    ("sports_centre", "Commercial", "medium"), ("industrial", "Industrial", "high"),
    ("warehouse", "Industrial", "medium"),                      # team decision B
    ("house", "Residential", "medium"), ("apartments", "Residential", "medium"),  # team decision A
])
def test_building_type_rules(building_type, category, confidence):
    row = classify_one(SQUARE, osm_buildings=gdf([SQUARE], osm_id=["w1"], type=[building_type]))
    assert (row.jrc_category, row.classification_confidence, row.classification_source) == \
        (category, confidence, "osm")


@pytest.mark.parametrize("building_type", ["", "shed", "garage", "barn", "church", "public", "government",
                                           "hospital", "service", "hangar"])
def test_generic_building_types_stay_unknown(building_type):
    row = classify_one(SQUARE, osm_buildings=gdf([SQUARE], osm_id=["w1"], type=[building_type]))
    assert row.jrc_category == "Unknown"
    assert row.reason_code == "generic_building_tags_only"


@pytest.mark.parametrize("fclass, category", [
    ("restaurant", "Commercial"), ("bank", "Commercial"), ("hairdresser", "Commercial"),
    ("doctors", "Commercial"), ("dentist", "Commercial"), ("veterinary", "Commercial"),  # team decision C
])
def test_poi_inside_gives_medium_commercial(fclass, category):
    row = classify_one(SQUARE, points=gdf([Point(5, 5)], osm_id=["n1"], fclass=[fclass]))
    assert (row.jrc_category, row.classification_confidence) == (category, "medium")


@pytest.mark.parametrize("fclass", ["bench", "waste_basket", "playground", "atm", "fire_station", "town_hall",
                                    "wastewater_plant", "community_centre"])
def test_context_pois_never_decide(fclass):
    assert classify_one(SQUARE, points=gdf([Point(5, 5)], osm_id=["n1"], fclass=[fclass])).jrc_category == "Unknown"


def test_conflicting_evidence_stays_unknown():
    row = classify_one(SQUARE, osm_buildings=gdf([SQUARE], osm_id=["w1"], type=["industrial"]),
                       points=gdf([Point(5, 5)], osm_id=["n1"], fclass=["restaurant"]))
    assert row.jrc_category == "Unknown"
    assert row.reason_code == "conflicting_evidence"
    assert row.osm_feature_ids == "w1;n1"


def test_agreeing_evidence_keeps_the_highest_confidence():
    row = classify_one(SQUARE, osm_buildings=gdf([SQUARE], osm_id=["w1"], type=["retail"]),
                       points=gdf([Point(5, 5)], osm_id=["n1"], fclass=["clothes"]))
    assert (row.jrc_category, row.classification_confidence) == ("Commercial", "high")


# --- decision order ----------------------------------------------------------------

def test_reviewed_decision_wins_over_everything():
    reviewed = pd.DataFrame({"buildingid": ["B1"], "jrc_category": ["Industrial"],
                             "classification_confidence": ["medium"]})
    row = classify_one(SQUARE, lu=landuse((box(-5, -5, 20, 20), 111, "Residential")),
                       osm_buildings=gdf([SQUARE], osm_id=["w1"], type=["commercial"]), reviewed=reviewed)
    assert (row.jrc_category, row.classification_source, row.classification_status) == \
        ("Industrial", "reviewed", "existing_reviewed")
    assert row.osm_proposed_category == "Commercial"   # still recorded for audit


def test_landuse_111_wins_over_osm_but_osm_is_recorded():
    row = classify_one(SQUARE, lu=landuse((box(-5, -5, 20, 20), 111, "Residential")),
                       osm_buildings=gdf([SQUARE], osm_id=["w1"], type=["commercial"]))
    assert (row.jrc_category, row.osm_proposed_category) == ("Residential", "Commercial")


def test_reviewed_rows_without_a_category_are_ignored():
    reviewed = pd.DataFrame({"buildingid": ["B1"], "jrc_category": [""], "classification_confidence": [""]})
    assert classify_one(SQUARE, reviewed=reviewed).classification_source == "none"


def test_evidence_and_rule_version_are_recorded():
    row = classify_one(SQUARE, osm_buildings=gdf([SQUARE], osm_id=["w1"], type=["office"]))
    assert json.loads(row.osm_evidence_json)[0]["value"] == "office"
    assert row.classification_rule_version == bc.RULE_VERSION


# --- summary and comparison ---------------------------------------------------------

def test_summary_and_comparison_with_reviewed():
    result = pd.DataFrame({"buildingid": ["A", "B", "C"], "jrc_category": ["Commercial", "Unknown", "Residential"],
                           "classification_source": ["osm", "none", "landuse"],
                           "reason_code": ["explicit_building_type", "generic_building_tags_only",
                                           "landuse_111_residential_proxy"],
                           "classification_reason": ["", "", ""]})
    assert bc.summary(result)["categories"] == {"Commercial": 1, "Unknown": 1, "Residential": 1}
    reviewed = pd.DataFrame({"buildingid": ["A", "B", "D"], "jrc_category": ["Commercial", "Industrial", "Residential"]})
    comparison = bc.compare_with_reviewed(result, reviewed)
    assert comparison["agrees"].tolist() == [True, False, False]
    assert comparison.loc[comparison.buildingid == "D", "jrc_category_rules"].item() == "not_in_aoi"


def test_load_reviewed_missing_file_is_empty(tmp_path):
    assert bc.load_reviewed(tmp_path / "missing.csv").empty


# --- scope ------------------------------------------------------------------------------

def test_reach_id_matches_the_team_format():
    pytest.importorskip("eddie_base_data.query")   # the shared AOI module; present inside EDDIE
    from flood_costs.scope import reach_id
    assert reach_id("Alzette", 35.0, 50.0) == "alzette_035000_050000"
    assert reach_id("Alzette", 40.0, 48.5) == "alzette_040000_048500"


# --- inputs inside EDDIE and writing results ------------------------------------------------

def test_reviewed_csv_prefers_inputs_folder_then_reference(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOOD_COSTS_REVIEWED_CSV", raising=False)
    inputs, reference = tmp_path / "inputs.csv", tmp_path / "reference.csv"
    reference.write_text("buildingid,jrc_category\n")
    assert bc.reviewed_csv_path((inputs, reference)) == str(reference)
    inputs.write_text("buildingid,jrc_category\n")
    assert bc.reviewed_csv_path((inputs, reference)) == str(inputs)
    assert bc.reviewed_csv_path((tmp_path / "none.csv",)) is None
    monkeypatch.setenv("FLOOD_COSTS_REVIEWED_CSV", "/x/y.csv")
    assert bc.reviewed_csv_path((inputs, reference)) == "/x/y.csv"


class _Scope:
    reach_id, variant_key, geometry = "alzette_035000_050000", "bbox_500", box(0, 0, 1, 1)


def _result(categories):
    return pd.DataFrame({"buildingid": list(categories), "jrc_category": list(categories.values()),
                         "classification_source": "osm", "reason_code": "x", "classification_reason": ""})


def test_write_results_files_and_report(tmp_path):
    out = tmp_path / "run" / "classification"
    reviewed = pd.DataFrame({"buildingid": ["A", "Z"], "jrc_category": ["Commercial", "Residential"]})
    report = bc.write_results(out, _Scope(), _result({"A": "Commercial", "B": "Unknown"}),
                              rules_only=_result({"A": "Commercial", "B": "Unknown"}), reviewed=reviewed,
                              sources={"osm": "x.gpkg"})
    written = pd.read_csv(out / "building_classification.csv")
    assert list(written.columns[:3]) == ["buildingid", "reach_id", "variant_key"]
    assert report["comparison_with_reviewed"] == {"reviewed_buildings_in_aoi": 1, "agree": 1,
        "by_reviewed_category": {"Commercial": {"reviewed": 1, "agree": 1, "rules_said": {"Commercial": 1}}}}
    assert json.loads((out / "summary.json").read_text())["sources"] == {"osm": "x.gpkg"}


def test_write_results_never_overwrites(tmp_path):
    bc.write_results(tmp_path / "c", _Scope(), _result({"A": "Residential"}))
    with pytest.raises(FileExistsError):
        bc.write_results(tmp_path / "c", _Scope(), _result({"A": "Residential"}))
