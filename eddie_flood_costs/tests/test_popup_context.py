"""Small saved-result fixtures; no GIS processing, network or EDDIE services."""
import json

import pytest

from flood_costs.cost_lookup import CostLookup, LookupValidationError, build_lookup
from flood_costs.popup_context import PopupDetails, build_popup_payload


@pytest.fixture
def popup_source(tmp_path, source_results_writer):
    def record(amount, status, accepted=True):
        return {"jrc_category": "Residential" if accepted else None,
                "accepted_provisional_classification": accepted,
                "classification_scope": "classified_subset" if accepted else "unresolved_in_corridor",
                "classification_confidence": None, "classification_source": "synthetic",
                "classification_basis": None, "classification_review_decision": None,
                "hq10": {"damage_eur2010": amount, "status": status, "quality_flags": []},
                "hq100": {"damage_eur2010": amount * 2 if amount is not None else None,
                          "status": status, "quality_flags": []}}
    buildings = {"ACT_POSITIVE": record(13573.35, "calculated_positive"),
                 "ACT_ZERO": record(0., "calculated_zero"),
                 "ACT_UNKNOWN": record(None, "unknown_classification", False)}
    source = tmp_path / "source"
    source.mkdir()
    source_results_writer(source, buildings)
    path = source / "building_flood_damage.geojson"
    document = json.loads(path.read_text())
    for feature in document["features"]:
        # This is the whole-building area, including both parts; no display-part area.
        feature["properties"].update(footprint_area_m2=321.75, footprint_part_count=2,
                                     depth_hq10_mean_m=.75, depth_hq100_mean_m=None,
                                     exposure_hq10_status="touched_or_partial",
                                     exposure_hq100_status="fully_contained")
    path.write_text(json.dumps(document))
    return source, CostLookup(build_lookup(source)), path


def test_exact_id_both_scenarios_and_whole_building_context(popup_source):
    _, lookup, path = popup_source
    result = build_popup_payload(lookup, "ACT_POSITIVE", PopupDetails(path))
    assert result["buildingid"] == "ACT_POSITIVE" and result["found"]
    assert result["footprint_area_m2"] == 321.75
    assert result["hq10"]["damage_eur2010"] == 13573.35
    assert result["hq100"]["damage_eur2010"] == 27146.7
    assert result["hq10"]["depth_mean_m"] == .75
    assert result["hq100"]["depth_mean_m"] is None
    assert result["hq10"]["exposure_status"] == "touched_or_partial"
    assert result["provisional"] and result["price_year"] == 2010
    assert "geometry" not in json.dumps(result) and "total_eur2010" not in result


@pytest.mark.parametrize("bid,amount", [("ACT_ZERO", 0.), ("ACT_UNKNOWN", None), ("ACT_ABSENT", None)])
def test_zero_unknown_and_absent_never_reuse_another_building(popup_source, bid, amount):
    _, lookup, path = popup_source
    result = build_popup_payload(lookup, bid, PopupDetails(path))
    assert result["buildingid"] == bid
    assert result["hq10"]["damage_eur2010"] == amount
    assert result["hq100"]["damage_eur2010"] == amount
    if bid == "ACT_ABSENT":
        assert not result["found"] and result["footprint_area_m2"] is None


def test_context_from_different_run_is_rejected_without_changing_cost(popup_source):
    _, lookup, path = popup_source
    document = json.loads(path.read_text())
    document["features"][0]["properties"]["footprint_area_m2"] = 999999
    path.write_text(json.dumps(document))
    result = build_popup_payload(lookup, "ACT_POSITIVE", PopupDetails(path))
    assert result["footprint_area_m2"] is None
    assert result["hq10"]["depth_mean_m"] is None
    assert result["hq10"]["damage_eur2010"] == 13573.35
    assert result["warnings"]


def test_missing_context_does_not_invent_area_or_depth(popup_source):
    _, lookup, _ = popup_source
    result = build_popup_payload(lookup, "ACT_POSITIVE")
    assert result["footprint_area_m2"] is None and result["hq10"]["depth_mean_m"] is None
    assert result["hq10"]["damage_eur2010"] == 13573.35


def test_duplicate_context_building_ids_are_rejected(popup_source):
    _, _, path = popup_source
    document = json.loads(path.read_text())
    document["features"].append(document["features"][0])
    path.write_text(json.dumps(document))
    with pytest.raises(LookupValidationError):
        PopupDetails(path)


def test_missing_or_malformed_provenance_never_supplies_unverified_context(popup_source):
    _, lookup, path = popup_source
    document = lookup.to_dict()
    document["metadata"]["source_sha256"] = []
    result = build_popup_payload(CostLookup(document), "ACT_POSITIVE", PopupDetails(path))
    assert result["footprint_area_m2"] is None and result["hq10"]["depth_mean_m"] is None
    assert result["hq10"]["damage_eur2010"] == 13573.35
    assert result["warnings"]


def test_popup_endpoint_is_read_only_and_existing_routes_remain(popup_source, tmp_path):
    flask = pytest.importorskip("flask", reason="Flask unavailable; dependencies are not installed by tests.")
    from flood_costs.blueprint import blueprint
    _, lookup, details = popup_source
    path = tmp_path / "lookup.json"
    path.write_text(json.dumps(lookup.to_dict()))
    app = flask.Flask(__name__)
    app.config.update(TESTING=True, FLOOD_COSTS_LOOKUP_PATH=str(path), FLOOD_COSTS_DETAILS_PATH=str(details))
    app.register_blueprint(blueprint)
    client = app.test_client()
    result = client.get("/flood-costs/popup/ACT_POSITIVE.json")
    assert result.status_code == 200 and result.json["footprint_area_m2"] == 321.75
    assert client.get("/flood-costs/popup/ACT_ABSENT.json").status_code == 404
    assert client.post("/flood-costs/popup/ACT_POSITIVE.json").status_code == 405
    assert client.get("/flood-costs/buildings/ACT_ZERO/hq10.json").json["damage_eur2010"] == 0
    assert client.get("/flood-costs/summary/hq100.json").status_code == 200
