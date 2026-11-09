"""Active backend HTTP contract, using synthetic files and an in-memory SQL store."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_popup_context import popup_source  # reuse the approved synthetic fixture


@pytest.fixture
def api(popup_source, tmp_path):
    flask = pytest.importorskip("flask")
    module = importlib.import_module("flood_costs.blueprint")
    for cache in (module._load_snapshot, module._load_db_snapshot, module._load_popup_details):
        cache.cache_clear()
    _, lookup, details = popup_source
    path = tmp_path / "lookup.json"
    path.write_text(json.dumps(lookup.to_dict()))
    app = flask.Flask(__name__)
    app.config.update(TESTING=True, FLOOD_COSTS_LOOKUP_PATH=str(path),
                      FLOOD_COSTS_DETAILS_PATH=str(details))
    app.register_blueprint(module.blueprint)
    yield app, module, lookup, path, details
    for cache in (module._load_snapshot, module._load_db_snapshot, module._load_popup_details):
        cache.cache_clear()


@pytest.fixture
def database(api, monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    app, module, lookup, _, _ = api
    engine = sqlalchemy.create_engine("sqlite://")
    document = lookup.to_dict()
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(
            "CREATE TABLE flood_cost_lookup (dataset_version TEXT, lookup TEXT)"))
        connection.execute(sqlalchemy.text(
            "INSERT INTO flood_cost_lookup VALUES (:version, :document)"),
            {"version": document["dataset_version"], "document": json.dumps(document)})
    queries = []

    @sqlalchemy.event.listens_for(engine, "before_cursor_execute")
    def capture_query(connection, cursor, statement, parameters, context, executemany):
        queries.append((statement, parameters))

    monkeypatch.setattr(module, "_engine", lambda: engine)
    app.config.pop("FLOOD_COSTS_LOOKUP_PATH")
    yield engine, queries
    engine.dispose()


def assert_popup(client, bid, amount10, amount100, status=200):
    response = client.get(f"/flood-costs/popup/{bid}.json")
    assert response.status_code == status
    result = response.get_json()
    assert result["buildingid"] == bid
    assert result["found"] is (status == 200)
    assert result["currency"] == "EUR" and result["price_year"] == 2010
    assert result["hq10"]["damage_eur2010"] == amount10
    assert result["hq100"]["damage_eur2010"] == amount100
    return result


@pytest.mark.parametrize("configuration", ["config", "environment"])
def test_file_api_preserves_frontend_contract_and_original_routes(api, monkeypatch, configuration):
    app, module, _, path, details = api
    if configuration == "environment":
        app.config.pop("FLOOD_COSTS_LOOKUP_PATH")
        app.config.pop("FLOOD_COSTS_DETAILS_PATH")
        monkeypatch.setenv("FLOOD_COSTS_LOOKUP_PATH", str(path))
        monkeypatch.setenv("FLOOD_COSTS_DETAILS_PATH", str(details))

    def unexpected_database():
        raise AssertionError("An explicitly configured file must not use the database.")

    monkeypatch.setattr(module, "_lookup_from_database", unexpected_database)
    client = app.test_client()
    result = assert_popup(client, "ACT_POSITIVE", 13573.35, 27146.7)
    assert result["category"] == "Residential"
    assert result["footprint_area_m2"] == 321.75  # the full two-part building
    assert result["hq10"]["depth_mean_m"] == .75
    assert result["hq100"]["depth_mean_m"] is None
    assert result["hq10"]["exposure_status"] == "touched_or_partial"
    assert_popup(client, "ACT_ZERO", 0., 0.)
    assert_popup(client, "ACT_UNKNOWN", None, None)
    assert_popup(client, "ACT_ABSENT", None, None, 404)
    assert_popup(client, "building-0", None, None, 404)  # never use a CZML entity ID
    for scenario, amount in (("hq10", 13573.35), ("hq100", 27146.7)):
        assert client.get(f"/flood-costs/buildings/ACT_POSITIVE/{scenario}.json").json["damage_eur2010"] == amount
        assert client.get(f"/flood-costs/summary/{scenario}.json").json["total_eur2010"] == amount
    assert client.post("/flood-costs/popup/ACT_POSITIVE.json").status_code == 405
    popup_rules = [rule for rule in app.url_map.iter_rules() if "/popup/" in rule.rule]
    assert len(popup_rules) == 1


def test_database_lookup_is_shared_by_popup_building_and_summary(api, database):
    app, _, _, _, _ = api
    _, queries = database
    client = app.test_client()
    result = assert_popup(client, "ACT_POSITIVE", 13573.35, 27146.7)
    assert result["footprint_area_m2"] == 321.75
    assert result["hq10"]["depth_mean_m"] == .75
    assert_popup(client, "ACT_ZERO", 0., 0.)
    assert_popup(client, "ACT_UNKNOWN", None, None)
    assert_popup(client, "ACT_ABSENT", None, None, 404)
    for scenario, amount in (("hq10", 13573.35), ("hq100", 27146.7)):
        assert client.get(f"/flood-costs/buildings/ACT_POSITIVE/{scenario}.json").json["damage_eur2010"] == amount
        assert client.get(f"/flood-costs/summary/{scenario}.json").json["total_eur2010"] == amount
    lookup_queries = [(sql, parameters) for sql, parameters in queries if "WHERE dataset_version" in sql]
    assert len(lookup_queries) == 1  # retain the existing version-keyed lookup cache
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql, _ in queries)


@pytest.mark.parametrize("context", ["unset", "missing", "mismatched", "malformed"])
def test_optional_context_never_changes_database_costs(api, database, context, tmp_path):
    app, _, _, _, details = api
    if context == "unset":
        app.config.pop("FLOOD_COSTS_DETAILS_PATH")
    elif context == "missing":
        app.config["FLOOD_COSTS_DETAILS_PATH"] = str(tmp_path / "absent.geojson")
    elif context == "mismatched":
        document = json.loads(details.read_text())
        document["features"][0]["properties"]["footprint_area_m2"] = 999999
        details.write_text(json.dumps(document))
    else:
        details.write_text("invalid JSON")
    result = assert_popup(app.test_client(), "ACT_POSITIVE", 13573.35, 27146.7)
    assert not result["context_available"]
    assert result["footprint_area_m2"] is None
    assert result["hq10"]["depth_mean_m"] is None
    assert result["hq100"]["exposure_status"] is None
    if context != "unset":
        assert result["warnings"]


def test_missing_database_returns_unavailable_without_fabricated_costs(api, monkeypatch):
    app, module, _, _, _ = api
    app.config.pop("FLOOD_COSTS_LOOKUP_PATH")

    def unavailable():
        raise RuntimeError("Synthetic database is offline")

    monkeypatch.setattr(module, "_engine", unavailable)
    response = app.test_client().get("/flood-costs/popup/ACT_POSITIVE.json")
    assert response.status_code == 503
    assert response.json["damage_eur2010"] is None


def test_invalid_explicit_lookup_does_not_silently_use_database(api, monkeypatch, tmp_path):
    app, module, _, _, _ = api
    app.config["FLOOD_COSTS_LOOKUP_PATH"] = str(tmp_path / "absent.json")

    def unexpected_database():
        raise AssertionError("Invalid explicit paths must not select another dataset.")

    monkeypatch.setattr(module, "_lookup_from_database", unexpected_database)
    response = app.test_client().get("/flood-costs/popup/ACT_POSITIVE.json")
    assert response.status_code == 503 and response.json["damage_eur2010"] is None


def test_database_response_is_rendered_by_existing_frontend_adapter(api, database, tmp_path):
    app, _, _, _, _ = api
    adapter = Path(__file__).resolve().parents[2] / "EDDIE/eddie_antarctica/terriajs/flood-cost-popup.js"
    node = shutil.which("node")
    jsc = os.environ.get("EDDIE_POPUP_JS_ENGINE") or shutil.which("jsc")
    if not adapter.is_file() or not (node or (jsc and Path(jsc).is_file())):
        pytest.skip("Frontend adapter and an existing Node or JSC executable are required.")
    client = app.test_client()
    payloads = {bid: client.get(f"/flood-costs/popup/{bid}.json").json
                for bid in ("ACT_POSITIVE", "ACT_ZERO", "ACT_UNKNOWN", "ACT_ABSENT")}
    checks = tmp_path / "api_frontend_contract.js"
    checks.write_text(
        "var payloads = " + json.dumps(payloads) + ";\n" + """
var popup = globalThis.EddieFloodCostPopup;
function check(value) { if (!value) throw new Error('API/frontend contract mismatch'); }
var flags = {hq10:true,hq100:true};
var positive = popup.display('ACT_POSITIVE', payloads.ACT_POSITIVE, flags);
check(positive.fc_popup_hq10_damage === '€13,573.35');
check(positive.fc_popup_hq100_damage === '€27,146.70');
check(positive.fc_popup_area === '321.75 m²');
check(positive.fc_popup_hq10_depth === '0.75 m');
check(positive.fc_popup_hq100_depth === 'N/A');
check(positive.fc_popup_category === 'Residential');
check(positive.fc_popup_hq10_show && positive.fc_popup_hq100_show);
check(popup.display('ACT_ZERO', payloads.ACT_ZERO, flags).fc_popup_hq10_damage === '€0.00');
check(popup.display('ACT_UNKNOWN', payloads.ACT_UNKNOWN, flags).fc_popup_hq100_damage === 'N/A');
check(popup.display('ACT_ABSENT', payloads.ACT_ABSENT, flags).fc_popup_hq10_damage === 'N/A');
check(popup.display('ACT_ZERO', payloads.ACT_POSITIVE, flags).fc_popup_hq10_damage === 'N/A');
""")
    if node:
        checks.write_text("require('vm').runInThisContext(require('fs').readFileSync(" +
                          json.dumps(str(adapter)) + ", 'utf8'));\n" + checks.read_text())
        command = [node, str(checks)]
    else:
        command = [jsc, str(adapter), str(checks)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
