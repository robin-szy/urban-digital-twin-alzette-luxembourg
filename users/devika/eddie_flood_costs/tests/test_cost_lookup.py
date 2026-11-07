"""Reused lookup/API tests with tiny synthetic results; no external datasets."""
import csv
import json
import math

import pytest

from flood_costs.cost_lookup import (
    EXCLUDED, SCENARIOS, SCHEMA_VERSION, CostLookup, InvalidScenario,
    LookupValidationError, _totals, build_lookup, write_lookup,
)


def record(category, scope, confidence, hq10, hq100):
    accepted = category is not None
    def cost(amount):
        return {"damage_eur2010": amount,
                "status": ("calculated_positive" if amount > 0 else "calculated_zero") if amount is not None
                          else "unknown_depth_coverage" if accepted else "excluded_outside_corridor" if scope == EXCLUDED else "unknown_classification",
                "quality_flags": ["provisional_reconstructed_depths", "vertical_datum_unverified"]}
    return {"jrc_category": category, "classification_scope": scope,
            "accepted_provisional_classification": accepted, "classification_confidence": confidence,
            "classification_source": "synthetic_fixture", "classification_basis": "synthetic fixture",
            "classification_review_decision": "synthetic fixture", "hq10": cost(hq10), "hq100": cost(hq100)}


@pytest.fixture
def document():
    buildings = {
        "ACT_HOME": record("Residential", "classified_subset", None, 0.0, 12.345),
        "ACT_SHOP": record("Commercial", "classified_subset", "medium", 1.25, 8.75),
        "ACT_UNRESOLVED": record(None, "unresolved_in_corridor", "medium", None, None),
        "ACT_EXCLUDED": record(None, EXCLUDED, None, None, None),
        "ACT_MISSING_DEPTH": record("Residential", "classified_subset", None, None, None),
    }
    return {"schema_version": SCHEMA_VERSION, "dataset_version": "synthetic-v1",
            "metadata": {"currency": "EUR", "price_year": 2010, "provisional": True},
            "buildings": buildings, "summaries": _totals(buildings)}


@pytest.fixture
def lookup(document):
    return CostLookup(document)


def test_building_id_lookup_and_scenario_selection(lookup):
    assert lookup.damage("ACT_HOME", "hq10") == 0
    assert lookup.damage("ACT_HOME", "HQ100") == 12.345
    result = lookup.building_cost("ACT_SHOP", "hq10")
    assert result["damage_eur2010"] == 1.25
    assert result["classification_confidence"] == "medium"
    assert result["provisional"] is True and result["price_year"] == 2010


def test_zero_null_missing_and_unresolved_are_distinct(lookup):
    assert lookup.damage("ACT_HOME", "hq10") == 0.0
    for bid in ("ACT_UNRESOLVED", "ACT_EXCLUDED", "ACT_MISSING_DEPTH", "ABSENT"):
        assert lookup.damage(bid, "hq10") is None
    assert lookup.building_cost("ACT_UNRESOLVED", "hq10")["found"] is True
    assert lookup.building_cost("ABSENT", "hq10")["found"] is False
    assert lookup.building_cost("ABSENT", "hq10")["monetary_status"] == "missing_building"


def test_totals_do_not_impute_unknown_as_zero(lookup):
    s = lookup.summary("hq10")
    assert s["total_eur2010"] == 1.25
    assert s["represented_buildings"] == 5
    assert s["accepted_provisional_classifications"] == 3
    assert s["calculated_damage_buildings"] == 2
    assert s["positive_damage_buildings"] == 1 and s["zero_damage_buildings"] == 1
    assert s["unknown_damage_buildings"] == 3 and s["classified_unknown_damage_buildings"] == 1
    assert s["complete_for_classified_subset"] is False
    assert s["by_category"]["Industrial"]["total_eur2010"] == 0


def test_all_depths_missing_has_null_classified_total(document):
    r = document["buildings"]["ACT_MISSING_DEPTH"]
    document["buildings"] = {"ACT_MISSING_DEPTH": r}
    document["summaries"] = _totals(document["buildings"])
    assert CostLookup(document).summary("hq100")["total_eur2010"] is None


def test_mutation_cannot_poison_cached_lookup(document, lookup):
    document["buildings"]["ACT_HOME"]["hq10"]["damage_eur2010"] = 999
    result = lookup.building_cost("ACT_HOME", "hq10")
    result["quality_flags"].append("poison")
    lookup.summary("hq10")["by_category"]["Commercial"]["total_eur2010"] = 999
    assert lookup.damage("ACT_HOME", "hq10") == 0
    assert "poison" not in lookup.building_cost("ACT_HOME", "hq10")["quality_flags"]
    assert lookup.summary("hq10")["by_category"]["Commercial"]["total_eur2010"] == 1.25


@pytest.mark.parametrize("scenario", ["hqext", "hq50", "120", "", "manual", None, 100])
def test_invalid_scenario(lookup, scenario):
    with pytest.raises(InvalidScenario):
        lookup.damage("ACT_HOME", scenario)
    with pytest.raises(InvalidScenario):
        lookup.summary(scenario)


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), True, "0"])
def test_invalid_amounts(document, amount):
    document["buildings"]["ACT_HOME"]["hq10"]["damage_eur2010"] = amount
    with pytest.raises(LookupValidationError):
        CostLookup(document)


def test_unresolved_zero_is_rejected(document):
    document["buildings"]["ACT_UNRESOLVED"]["hq10"]["damage_eur2010"] = 0
    with pytest.raises(LookupValidationError, match="null"):
        CostLookup(document)


def test_unknown_status_cannot_hide_known_amount(document):
    document["buildings"]["ACT_HOME"]["hq10"]["status"] = "unknown_depth_coverage"
    with pytest.raises(LookupValidationError):
        CostLookup(document)


@pytest.mark.parametrize("key,value", [("schema_version", "2.0.0"), ("metadata", {"currency": "EUR", "price_year": 2026, "provisional": True})])
def test_wrong_schema_or_price_year(document, key, value):
    document[key] = value
    with pytest.raises(LookupValidationError):
        CostLookup(document)


def test_tampered_summary_is_rejected(document):
    document["summaries"]["hq10"]["total_eur2010"] = 999
    with pytest.raises(LookupValidationError, match="reconcile"):
        CostLookup(document)


def test_duplicate_json_building_id_is_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"buildings":{"ACT_A":{},"ACT_A":{}}}')
    with pytest.raises(LookupValidationError, match="Duplicate"):
        CostLookup.load(path)


def test_nonfinite_json_is_rejected(tmp_path):
    path = tmp_path / "nonfinite.json"
    path.write_text('{"amount":NaN}')
    with pytest.raises(LookupValidationError, match="Non-finite"):
        CostLookup.load(path)


def test_invalid_utf8_is_a_validation_error(tmp_path):
    path = tmp_path / "binary.json"
    path.write_bytes(b'\xff\xfe')
    with pytest.raises(LookupValidationError):
        CostLookup.load(path)


def test_json_roundtrip_preserves_zero_and_null(document, tmp_path):
    path = tmp_path / "lookup.json"
    path.write_text(json.dumps(document, allow_nan=False))
    lookup = CostLookup.load(path)
    assert lookup.damage("ACT_HOME", "hq10") == 0
    assert lookup.damage("ACT_UNRESOLVED", "hq10") is None


@pytest.fixture
def source_dir(document, tmp_path, source_results_writer):
    """Four saved-result inputs, generated from five synthetic buildings only."""
    source = tmp_path / "monetary"
    source.mkdir()
    source_results_writer(source, document["buildings"])
    return source


@pytest.fixture
def built_document(source_dir):
    return build_lookup(source_dir)


def test_source_counts_and_confidence(built_document):
    lookup = CostLookup(built_document)
    for scenario in SCENARIOS:
        summary = lookup.summary(scenario)
        assert summary["represented_buildings"] == 5
        assert summary["accepted_provisional_classifications"] == 3
        assert summary["unknown_damage_buildings"] == 3
        assert summary["unresolved_classifications"] == 1
        assert summary["outside_corridor_excluded"] == 1
        assert summary["by_category"]["Residential"]["represented_buildings"] == 2
        assert summary["by_category"]["Commercial"]["represented_buildings"] == 1
        assert summary["by_category"]["Industrial"]["represented_buildings"] == 0
    assert lookup.building_cost("ACT_SHOP", "hq100")["classification_confidence"] == "medium"
    assert lookup.building_cost("ACT_UNRESOLVED", "hq100")["damage_eur2010"] is None


@pytest.mark.parametrize("scenario,expected", [("hq10", 1.25), ("hq100", 21.095)])
def test_totals_reconcile_unrounded_saved_results(built_document, source_dir, scenario, expected):
    lookup = CostLookup(built_document)
    with (source_dir / "damage_summary.csv").open(newline="") as handle:
        saved = next(r for r in csv.DictReader(handle)
                     if r["scenario"].lower() == scenario and r["group"] == "Classified total")
    total = lookup.summary(scenario)["total_eur2010"]
    assert total == pytest.approx(expected)
    assert math.isclose(total, float(saved["known_estimates_sum_eur2010"]), rel_tol=0, abs_tol=1e-8)
    values = [record[scenario]["damage_eur2010"] for record in built_document["buildings"].values()
              if record["accepted_provisional_classification"] and record[scenario]["damage_eur2010"] is not None]
    assert total == math.fsum(values)


def test_generated_lookup_has_only_approved_fields_and_no_geometry(built_document):
    allowed = {"jrc_category", "accepted_provisional_classification", "classification_scope",
               "classification_confidence", "classification_source", "classification_basis",
               "classification_review_decision", "hq10", "hq100"}
    for record in built_document["buildings"].values():
        assert set(record) == allowed
    encoded = json.dumps(built_document)
    for disallowed in ('"geometry"', '"coordinates"', '"email"', '"phone"', '"classification_osm_evidence_json"', '/Users/'):
        assert disallowed not in encoded


def test_duplicate_csv_id_is_rejected_before_other_sources(source_dir, tmp_path):
    with (source_dir / "building_flood_damage.csv").open(newline="") as f:
        reader = csv.DictReader(f); fields = reader.fieldnames; first = next(reader)
    for name in ("building_flood_damage.geojson", "damage_summary.csv", "damage_validation.json"):
        (tmp_path / name).write_bytes((source_dir / name).read_bytes())
    with (tmp_path / "building_flood_damage.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows([first, first])
    with pytest.raises(LookupValidationError, match="Duplicate building ID"):
        build_lookup(tmp_path)


def test_writer_refuses_overwrite_and_git_checkout(source_dir, tmp_path):
    output = tmp_path / "lookup.json"
    write_lookup(source_dir, output)
    assert CostLookup.load(output).summary("hq10")["accepted_provisional_classifications"] == 3
    with pytest.raises(FileExistsError):
        write_lookup(source_dir, output)
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    with pytest.raises(LookupValidationError, match="outside Git"):
        write_lookup(source_dir, repo / "generated.json")


def test_writer_protects_original_results_folder(source_dir):
    with pytest.raises(LookupValidationError, match="separate"):
        write_lookup(source_dir, source_dir / "must_not_create.json")


@pytest.fixture
def flask_client(document, tmp_path):
    flask = pytest.importorskip("flask", reason="Flask is absent; no team-environment changes authorized.")
    from flood_costs.blueprint import blueprint
    path = tmp_path / "lookup.json"; path.write_text(json.dumps(document))
    app = flask.Flask(__name__)
    app.config.update(TESTING=True, FLOOD_COSTS_LOOKUP_PATH=str(path))
    app.register_blueprint(blueprint)
    return app.test_client()


def test_flask_routes_zero_null_missing_invalid_and_read_only(flask_client):
    c = flask_client
    assert c.get('/flood-costs/summary/hq10.json').json['total_eur2010'] == 1.25
    assert c.get('/flood-costs/buildings/ACT_HOME/hq10.json').json['damage_eur2010'] == 0
    unresolved = c.get('/flood-costs/buildings/ACT_UNRESOLVED/hq10.json')
    assert unresolved.status_code == 200 and unresolved.json['damage_eur2010'] is None
    missing = c.get('/flood-costs/buildings/ABSENT/hq100.json')
    assert missing.status_code == 404 and missing.json['damage_eur2010'] is None
    assert c.get('/flood-costs/summary/hqext.json').status_code == 400
    assert c.get('/flood-costs/buildings/ACT_HOME/120.json').status_code == 400
    assert c.post('/flood-costs/summary/hq10.json').status_code == 405


def test_flask_missing_or_invalid_lookup_returns_503(flask_client, tmp_path, monkeypatch):
    monkeypatch.delenv('FLOOD_COSTS_LOOKUP_PATH', raising=False)
    flask_client.application.config.pop('FLOOD_COSTS_LOOKUP_PATH')
    assert flask_client.get('/flood-costs/summary/hq10.json').status_code == 503
    bad = tmp_path / 'invalid.json'; bad.write_text('{"bad":"schema"}')
    flask_client.application.config['FLOOD_COSTS_LOOKUP_PATH'] = str(bad)
    response = flask_client.get('/flood-costs/summary/hq10.json')
    assert response.status_code == 503 and response.json['damage_eur2010'] is None
    assert str(tmp_path) not in response.get_data(as_text=True)

