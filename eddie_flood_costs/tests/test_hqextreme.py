"""Three-scenario contracts, with small synthetic records and no live services."""
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pandas as pd
import pytest
import shapely

from flood_costs import WorkflowPaths
from flood_costs.cost_lookup import (
    CostLookup, InvalidScenario, LookupValidationError, SCENARIOS, _totals, build_lookup,
)
from flood_costs.popup_context import PopupDetails, build_popup_payload
from test_cost_lookup import document as legacy_document, record


@pytest.fixture
def v2_source(tmp_path, source_results_writer):
    buildings = {
        "ACT_OLD": record("Residential", "classified_subset", None, 0., 10.),
        "ACT_NEW": record("Residential", "classified_subset", "medium", None, None),
        "ACT_ZERO": record("Commercial", "classified_subset", "high", 0., 0.),
        "ACT_PENDING": record(None, "pending_classification_review", "medium", None, None),
    }
    for bid, row in buildings.items():
        amount = {"ACT_OLD": 25., "ACT_NEW": 5., "ACT_ZERO": 0., "ACT_PENDING": None}[bid]
        row["hqextreme"] = {
            "damage_eur2010": amount,
            "status": "unknown_classification" if amount is None else
                      "calculated_positive" if amount else "calculated_zero",
            "quality_flags": [],
        }
    buildings["ACT_NEW"]["classification_review_decision"] = "approved_user_provisional"
    for s in ("hq10", "hq100"):
        buildings["ACT_NEW"][s].update(
            status="not_assessed", quality_flags=["legacy_scenario_unassessed_amount_preserved_null"])
    buildings["ACT_ZERO"]["hqextreme"]["quality_flags"] = ["positive_depth_only_in_excluded_boundary_samples"]
    source_results_writer(tmp_path, buildings)
    geo = tmp_path / "building_flood_damage.geojson"
    doc = json.loads(geo.read_text())
    for feature in doc["features"]:
        feature["properties"].update(footprint_area_m2=100., depth_hqextreme_mean_m=.5,
                                     exposure_hqextreme_status="touched_or_partial")
    geo.write_text(json.dumps(doc))
    return tmp_path, CostLookup(build_lookup(tmp_path)), geo


def test_v2_preserves_zero_null_and_explicit_approval_scope(v2_source):
    _, lookup, geo = v2_source
    assert lookup.scenarios == SCENARIOS
    assert lookup.summary("HQEXTREME")["total_eur2010"] == 30.
    assert lookup.summary("hq100")["total_eur2010"] == 10.
    assert lookup.summary("hq10")["pending_classification_review"] == 1
    assert lookup.building_cost("ACT_NEW", "hq10")["monetary_status"] == "not_assessed"
    assert lookup.damage("ACT_NEW", "hq10") is None
    assert lookup.damage("ACT_ZERO", "hqextreme") == 0.
    assert lookup.damage("ACT_PENDING", "hqextreme") is None
    payload = build_popup_payload(lookup, "ACT_NEW", PopupDetails(geo))
    assert payload["available_scenarios"] == list(SCENARIOS)
    assert payload["hqextreme"]["damage_eur2010"] == 5.
    assert payload["hqextreme"]["depth_mean_m"] == .5
    assert payload["footprint_area_m2"] == 100.


@pytest.mark.parametrize("change", ["pending_zero", "unscoped_null", "extreme_unassessed", "v1_tag", "missing_scenario"])
def test_v2_rejects_misleading_or_incomplete_records(v2_source, change):
    _, lookup, _ = v2_source
    doc = lookup.to_dict()
    if change == "pending_zero":
        doc["buildings"]["ACT_PENDING"]["hqextreme"]["damage_eur2010"] = 0.
    elif change == "unscoped_null":
        doc["buildings"]["ACT_NEW"]["classification_review_decision"] = None
    elif change == "extreme_unassessed":
        doc["buildings"]["ACT_NEW"]["hqextreme"] = copy.deepcopy(doc["buildings"]["ACT_NEW"]["hq10"])
    elif change == "v1_tag":
        doc["schema_version"] = "1.0.0"
    else:
        doc["buildings"]["ACT_NEW"].pop("hqextreme")
    with pytest.raises(LookupValidationError):
        CostLookup(doc)


def test_v1_contract_is_still_two_scenarios(legacy_document):
    lookup = CostLookup(legacy_document)
    assert lookup.to_dict() == legacy_document
    assert lookup.summary("hq10")["schema_version"] == "1.0.0"
    assert "pending_classification_review" not in lookup.summary("hq10")
    assert build_popup_payload(lookup, "ACT_HOME")["available_scenarios"] == ["hq10", "hq100"]
    with pytest.raises(InvalidScenario, match="unavailable"):
        lookup.summary("hqextreme")


@pytest.mark.parametrize("storage", ["file", "database"])
def test_three_scenario_api_and_context_hash_failure(v2_source, monkeypatch, tmp_path, storage):
    flask = pytest.importorskip("flask")
    _, lookup, geo = v2_source
    module = importlib.import_module("flood_costs.blueprint")
    module._load_snapshot.cache_clear()
    module._load_db_snapshot.cache_clear()
    module._load_popup_details.cache_clear()
    app = flask.Flask(__name__)
    app.register_blueprint(module.blueprint)
    app.config.update(TESTING=True, FLOOD_COSTS_DETAILS_PATH=str(geo))
    statements = []
    if storage == "file":
        path = tmp_path / "lookup.json"
        path.write_text(json.dumps(lookup.to_dict()))
        app.config["FLOOD_COSTS_LOOKUP_PATH"] = str(path)
    else:
        sa = pytest.importorskip("sqlalchemy")
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE flood_cost_lookup (dataset_version TEXT, lookup TEXT)"))
            conn.execute(sa.text("INSERT INTO flood_cost_lookup VALUES (:v, :doc)"),
                         {"v": "v2-test", "doc": json.dumps(lookup.to_dict())})
        # Match the version-keyed database cache contract.
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE flood_cost_lookup SET dataset_version = :v"),
                         {"v": lookup.to_dict()["dataset_version"]})
        sa.event.listen(engine, "before_cursor_execute", lambda c, cu, sql, p, ctx, many: statements.append(sql))
        monkeypatch.setattr(module, "_engine", lambda: engine)
        # No AOI module in these tests: the run covers the current reach (Robin's _reach_subset -> None).
        monkeypatch.setattr(module, "_reach_subset", lambda: None)
    client = app.test_client()
    for s in SCENARIOS:
        assert client.get(f"/flood-costs/summary/{s}.json").status_code == 200
        assert client.get(f"/flood-costs/buildings/ACT_NEW/{s}.json").json["found"]
    assert client.get("/flood-costs/popup/ACT_NEW.json").json["context_available"]
    assert client.get("/flood-costs/buildings/ACT_ABSENT/hqextreme.json").status_code == 404
    assert client.get("/flood-costs/summary/manual.json").status_code == 400
    geo.write_text(geo.read_text() + "\n")  # same records, different release bytes
    payload = client.get("/flood-costs/popup/ACT_NEW.json").json
    assert not payload["context_available"] and payload["warnings"]
    assert payload["hqextreme"]["damage_eur2010"] == 5.
    assert payload["footprint_area_m2"] is None
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)


def test_active_classification_path_is_independent_of_staging_root(tmp_path, monkeypatch):
    config = tmp_path / "paths.json"
    config.write_text(json.dumps({"project_root": str(tmp_path), "stages": {"osm": "osm_classification"}}))
    monkeypatch.setenv("FLOOD_COSTS_PATH_CONFIG", str(config))
    monkeypatch.setenv("FLOOD_COSTS_OUTPUT_ROOT", str(tmp_path / "candidate"))
    paths = WorkflowPaths()
    assert paths.classification_root() == tmp_path / "outputs/osm_classification"
    assert paths.stage_path("monetary") == tmp_path / "candidate/monetary_damage"


def test_database_schema_preflight_never_mutates_an_old_table():
    sa = pytest.importorskip("sqlalchemy")
    from flood_costs.tasks import _check_publish_schema
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE flood_depth_buildings (reach_id TEXT, hq10 FLOAT)"))
        conn.execute(sa.text("INSERT INTO flood_depth_buildings VALUES ('existing', 12.5)"))
    with pytest.raises(RuntimeError, match="migration required"):
        _check_publish_schema(engine, "flood_depth_buildings", pd.DataFrame({"hq10": [1.], "hqextreme": [2.]}))
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT * FROM flood_depth_buildings")).all() == [("existing", 12.5)]


def test_older_geos_voronoi_cell_order_is_assigned_to_correct_sample(monkeypatch):
    from flood_costs import damage_calculation as damage
    original = shapely.voronoi_polygons

    def unordered(*args, **kwargs):
        if "ordered" in kwargs:
            raise TypeError("ordered requires newer GEOS")
        result = original(*args, **kwargs)
        return shapely.GeometryCollection(list(result.geoms)[::-1])

    monkeypatch.setattr(shapely, "voronoi_polygons", unordered)
    polygon = shapely.box(0, 0, 4, 1)
    points = pd.DataFrame({"x_epsg2169": [.5, 2.5], "y_epsg2169": [.5, .5], "part_index": [0, 0]})
    weights, _, unsupported, area, _ = damage.build_weights(polygon, polygon, points, 4.)
    assert weights.tolist() == [1.5, 2.5]
    assert area == 4. and unsupported == 0.


def test_popup_hqextreme_defaults_comparison_and_legacy_payload(v2_source, tmp_path):
    _, lookup, geo = v2_source
    adapter = Path(__file__).resolve().parents[2] / "EDDIE/eddie_antarctica/terriajs/flood-cost-popup.js"
    node = shutil.which("node")
    jsc = os.environ.get("EDDIE_POPUP_JS_ENGINE") or shutil.which("jsc")
    if not adapter.is_file() or not (node or (jsc and Path(jsc).is_file())):
        pytest.skip("An existing Node or JavaScriptCore executable is required.")
    payload = build_popup_payload(lookup, "ACT_NEW", PopupDetails(geo))
    checks = tmp_path / "hqextreme.js"
    checks.write_text("var payload = " + json.dumps(payload) + ";\n" + """
var popup = globalThis.EddieFloodCostPopup;
function check(value) { if (!value) throw new Error('HQextreme popup regression'); }
var shown = popup.display('ACT_NEW', payload, {hqextreme:true});
check(shown.fc_popup_hqextreme_damage === '€5.00');
check(!shown.fc_popup_no_scenario && !shown.fc_popup_comparing);
check(popup.display('ACT_NEW', payload, {hq10:true,hqextreme:true}).fc_popup_comparing);
check(popup.display('ACT_NEW', payload, {hq10:true}).fc_popup_warning.indexOf('not assessed') >= 0);
var legacy = JSON.parse(JSON.stringify(payload)); delete legacy.hqextreme;
check(popup.display('ACT_NEW', legacy, {hqextreme:true}).fc_popup_hqextreme_damage === 'N/A');
check(popup.display('ACT_NEW', legacy, {hq10:true}).fc_popup_category === 'Residential');
var feature = {properties:{buildingid:'ACT_NEW'}}, map = 'hqextreme';
var terria = {selectedFeature:feature, workbench:{items:[]}};
var c = popup.createController(terria, {readMapScenario:function(){return map;}});
c.tick(); check(c.state().hqextreme && !c.state().hq10 && !c.state().hq100);
c.setScenario('hq100', true); check(c.state().hq100 && c.state().hqextreme);
map = 'hq10'; c.tick(); check(c.state().hq10 && !c.state().hqextreme && !c.state().hq100);
map = 'hqextreme'; c.tick(); check(c.state().hqextreme);
globalThis.AlzetteTwin = {getState:function(){return {scenario:'hqextreme'};}};
check(popup.mapScenario(null, terria) === 'hqextreme');
""")
    if node:
        checks.write_text("require('vm').runInThisContext(require('fs').readFileSync(" +
                          json.dumps(str(adapter)) + ", 'utf8'));\n" + checks.read_text())
        command = [node, str(checks)]
    else:
        command = [jsc, str(adapter), str(checks)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
