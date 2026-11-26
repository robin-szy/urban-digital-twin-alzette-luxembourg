"""Dynamic flood costs inside the module: maths, database inputs/results and the API.

Synthetic data only; SQLite stands in for PostGIS and a fake replaces the water model.
"""
import csv
import gzip
import importlib
import json
import math

import numpy as np
import pandas as pd
import pytest

from flood_costs import damage_calculation as damage
from flood_costs.dynamic import core, jobs, models, parameters, store
from flood_costs.dynamic.depths import BackendDepths, DepthsUnavailable, encode

sqlalchemy = pytest.importorskip("sqlalchemy")

# Synthetic curve, as in conftest.py: not official coefficients.
CURVE = {"depth_m": [0, .5, 1, 1.5, 2, 3, 4, 5, 6],
         "damage_fraction": [0, .25, .4, .5, .6, .75, .85, .95, 1],
         "maximum_damage_eur2010_per_m2": 100.0}
JRC = {c: dict(CURVE) for c in ("Residential", "Commercial", "Industrial")}


def make(depths: dict, weights: dict, category="Residential"):
    index = pd.MultiIndex.from_tuples(list(depths), names=core.SAMPLE_INDEX)
    n = len(next(iter(depths.values())))
    times = pd.date_range("2021-07-14", periods=n, freq="h")
    depth = pd.DataFrame(list(depths.values()), index=index, columns=times, dtype=float)
    samples = pd.DataFrame({"weight_m2": [weights[k] for k in depths], "jrc_category": category}, index=index)
    return samples, depth


# ----------------------------------------------------------------------- the maths (unchanged)


def test_model1_matches_the_static_weighted_estimate():
    samples, depth = make({("B1", 0): [1.0], ("B1", 1): [0.0]}, {("B1", 0): 25.0, ("B1", 1): 75.0})
    ours = models.model1(depth, samples, JRC).iloc[0, 0]
    theirs = damage.weighted_estimate(np.array([25., 75.]), np.array([True, True]), np.array([1., 0.]),
                                      0., 100., 100., CURVE)["monetary"]
    assert ours == pytest.approx(theirs)


def test_model1_holds_the_peak():
    samples, depth = make({("B1", 0): [0.2, 1.0, 0.3, 0.0]}, {("B1", 0): 50.0})
    values = models.model1(depth, samples, JRC).loc["B1"].to_numpy()
    assert np.all(np.diff(values) >= 0) and values[-1] == pytest.approx(50 * 100 * .4)


def test_model2_uses_team_beta_and_tau_and_beta_zero_is_model1(monkeypatch):
    samples, depth = make({("B1", 0): [1.0]}, {("B1", 0): 50.0})
    for hours in (24.0, 72.0):
        expected = 50 * 100 * .4 * (1 + .3 * (1 - math.exp(-hours / 24)))
        assert models.model2_for_duration(depth, samples, JRC, hours).iloc[0, 0] == pytest.approx(expected)
    monkeypatch.setattr(parameters, "DURATION_BETA", {c: 0.0 for c in JRC})
    samples, depth = make({("B1", 0): [0, .5, 1.0, 1.0, .2]}, {("B1", 0): 30.0})
    pd.testing.assert_frame_equal(models.model2(depth, samples, JRC), models.model1(depth, samples, JRC))


def test_unknown_depth_keeps_the_building_unknown_not_zero():
    samples, depth = make({("B1", 0): [0.5, np.nan, 1.0], ("B1", 1): [0.5, 0.5, 0.5]},
                          {("B1", 0): 10.0, ("B1", 1): 10.0})
    values = models.model1(depth, samples, JRC).loc["B1"]
    assert values.iloc[0] > 0 and values.iloc[1:].isna().all()


def test_disruption_counts_wet_hours_and_unknown_attributes_stay_unknown():
    samples, depth = make({("B1", 0): [0.0, 0.5, 0.5, 0.0]}, {("B1", 0): 10.0})
    attributes = pd.DataFrame({"residents": [2.0], "floor_area_m2": [100.0], "jrc_category": ["Residential"]},
                              index=pd.Index(["B1"], name="buildingid"))
    cost = models.disruption(depth, samples, attributes).loc["B1"].tolist()
    per_hour = 2 * parameters.COST_DISPLACEMENT_EUR_PER_PERSON_DAY / 24
    assert cost == pytest.approx([0, per_hour, 2 * per_hour, 2 * per_hour])
    attributes.loc["B1", "residents"] = np.nan
    assert models.disruption(depth, samples, attributes).loc["B1"].isna().all()


def test_flooded_building_rule_is_half_the_footprint_above_25_cm():
    assert (parameters.WET_RULE, parameters.WET_AREA_SHARE, parameters.DISRUPTION_DEPTH_M) == ("area_share", .5, .25)
    weights = {("B1", k): 10.0 for k in range(4)}
    cases = {"one riverside point wet": [0.6, 0, 0, 0],      # 25 % of the footprint
             "half above 25 cm": [0.3, 0.3, 0, 0],             # 50 %
             "most wet but shallow": [0.2, 0.2, 0.2, 0.2],     # nowhere above 25 cm
             "one point unknown": [0.3, 0.3, 0.3, np.nan]}
    flooded = {}
    for name, values in cases.items():
        samples, depth = make({key: [v] for key, v in zip(weights, values)}, weights)
        flooded[name] = core.building_is_wet(depth, samples).iloc[0, 0]
    assert flooded["one riverside point wet"] == 0.0
    assert flooded["half above 25 cm"] == 1.0
    assert flooded["most wet but shallow"] == 0.0
    assert np.isnan(flooded["one point unknown"])                # undecidable stays unknown


# ----------------------------------------------------------------------- database fixtures


class FakeDepths:
    """Stands in for the backend's water model: depth = (level - ground) per sample."""

    def __init__(self, ground_cm: dict, samples: pd.DataFrame, unknown=()):
        self.ground = np.array([ground_cm[b] for b, _ in samples.index], dtype=float)
        self.unknown = np.array([(b, s) in unknown for b, s in samples.index])
        self.calls = []

    def _depth(self, level_cm):
        values = np.maximum(level_cm - self.ground, 0.0) / 100.0
        values[self.unknown] = np.nan
        return values

    def at_stage(self, stage_cm):
        self.calls.append(("stage", stage_cm))
        return self._depth(float(stage_cm))

    def at_key(self, key_text):
        self.calls.append(("key", key_text))
        return self._depth(float(key_text[1:].split("_")[0]))

    def keyframes(self, start_iso, end_iso):
        times = pd.date_range(start_iso, periods=5, freq="h")
        stages = [0, 150, 250, 250, 100]            # rises, holds, falls
        return {"times": [t.isoformat() for t in times], "keys": [f"g{s}_{s}_{s}" for s in stages]}


def _monetary_dir(tmp_path):
    folder = tmp_path / "monetary_damage"
    folder.mkdir()
    rows = []
    for building, n in (("ACT_RESIDENTIAL1", 2), ("ACT_COMMERCIAL1", 1), ("ACT_UNKNOWN01", 1)):
        for k in range(n):
            for scenario in ("hq10", "hq100"):              # repeated per scenario, as written
                rows.append({"buildingid": building, "sample_index": k, "part_index": 0,
                             "x_epsg2169": 77000 + k, "y_epsg2169": 70000, "weight_m2": 50.0,
                             "scenario": scenario})
    with gzip.open(folder / "sample_damage_weights.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (folder / "jrc_parameters.json").write_text(json.dumps({"parameters": JRC}))
    return folder


@pytest.fixture
def database(tmp_path):
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        pd.DataFrame([{"run_id": "run-a", "dataset_version": "sha256:a", "lookup": "{}"}]).to_sql(
            store.LOOKUP, conn, index=False)
        pd.DataFrame([
            {"buildingid": "ACT_RESIDENTIAL1", "jrc_category": "Residential", "accepted_provisional_classification": True, "run_id": "run-a"},
            {"buildingid": "ACT_COMMERCIAL1", "jrc_category": "Commercial", "accepted_provisional_classification": True, "run_id": "run-a"},
            {"buildingid": "ACT_UNKNOWN01", "jrc_category": None, "accepted_provisional_classification": False, "run_id": "run-a"},
        ]).to_sql(store.BUILDINGS, conn, index=False)
        pd.DataFrame([{"scenario": "HQ100", "group": "Classified total", "known_estimates_sum_eur2010": 5000.0}]
                     ).to_sql("flood_cost_summary", conn, index=False)
        pd.DataFrame([
            {"gml_id": "Building2D.ACT_RESIDENTIAL1", "est_population": 4.0, "footprint_m2": 100.0, "floors": 2, "final_height_m": 6.0},
            {"gml_id": "Building2D.ACT_COMMERCIAL1", "est_population": 0.0, "footprint_m2": 50.0, "floors": None, "final_height_m": 9.0},
        ]).to_sql(store.POPULATION, conn, index=False)
        report = store.publish_inputs(conn, "run-a", "sha256:a", _monetary_dir(tmp_path))
    assert report["samples"] == 4 and report["buildings"] == 3
    return engine


def _inputs(engine):
    with engine.connect() as conn:
        run = store.published_run(conn)
        samples = store.load_samples(conn, run)
        return run, samples, store.load_jrc(conn, run), store.load_attributes(conn, samples)


GROUND = {"ACT_RESIDENTIAL1": 100.0, "ACT_COMMERCIAL1": 200.0}


def test_inputs_come_from_the_database(database):
    run, samples, jrc, attributes = _inputs(database)
    assert run == {"run_id": "run-a", "dataset_version": "sha256:a"}
    assert sorted(samples.index.get_level_values("buildingid").unique()) == ["ACT_COMMERCIAL1", "ACT_RESIDENTIAL1"]
    assert jrc["Residential"]["maximum_damage_eur2010_per_m2"] == 100.0
    assert attributes.loc["ACT_RESIDENTIAL1", "floor_area_m2"] == 200.0      # 100 m2 x 2 floors
    assert attributes.loc["ACT_COMMERCIAL1", "floor_area_m2"] == 150.0       # 50 m2 x 9 m / 3 m
    assert attributes.loc["ACT_COMMERCIAL1", "residents"] == 0.0


def test_samples_of_another_run_are_refused(database):
    with database.begin() as conn:
        conn.execute(sqlalchemy.text(f"UPDATE {store.LOOKUP} SET run_id = 'run-b', dataset_version = 'sha256:b'"))
    with database.connect() as conn, pytest.raises(store.DynamicUnavailable):
        store.load_samples(conn, store.published_run(conn))


def test_backend_depths_are_checked_against_run_and_sample_order(database):
    run, samples, _, _ = _inputs(database)
    client = BackendDepths(run, store.samples_signature(samples), len(samples), base_url="http://unused")
    good = {"status": "success", "dataset_version": "sha256:a", "signature": store.samples_signature(samples),
            "depths_m": encode(np.array([0.5, np.nan, 1.0]))}
    values = client._checked(good)
    assert np.isnan(values[1]) and values[0] == .5
    for bad in ({**good, "dataset_version": "sha256:b"}, {**good, "signature": "other"},
                {**good, "depths_m": [0.1]}, {"status": "unavailable", "message": "down"}):
        with pytest.raises(DepthsUnavailable):
            client._checked(bad)


# ----------------------------------------------------------------------- the API


@pytest.fixture
def api(database, monkeypatch):
    flask = pytest.importorskip("flask")
    module = importlib.import_module("flood_costs.blueprint")
    routes = importlib.import_module("flood_costs.dynamic.routes")
    monkeypatch.setattr(routes, "_engine", lambda: database)
    monkeypatch.setattr(routes, "_subset", lambda: None)
    app = flask.Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(module.blueprint)
    return app.test_client(), routes


def _write_curve(database, stages=(0, 100, 150, 200, 300)):
    run, samples, jrc, _ = _inputs(database)
    totals, buildings, meta = jobs.curve(samples, jrc, FakeDepths(GROUND, samples), stages, (6, 24, 72))
    with database.begin() as conn:
        store.write_curve(conn, run, totals, buildings, meta)
    return totals


def test_curve_and_stage_routes(api, database):
    client, _ = api
    assert client.get("/flood-costs/dynamic/curve.json").status_code == 404        # not computed yet
    totals = _write_curve(database)
    # 300 cm: residential 2 m water -> 2 samples x 50 m2 x 100 x 0.6; commercial 1 m -> 50 x 100 x 0.4
    assert totals.loc[300.0, "model1_eur2010"] == pytest.approx(2 * 50 * 100 * .6 + 50 * 100 * .4)
    curve = client.get("/flood-costs/dynamic/curve.json").get_json()
    assert curve["status"] == "success" and not curve["stale"] and curve["price_year"] == 2010
    assert [s["stage_cm"] for s in curve["stages"]] == [0, 100, 150, 200, 300]
    assert curve["static_totals_eur2010"] == {"HQ100": 5000.0}
    assert curve["model1_equivalent_stage_cm"]["HQ100"] == pytest.approx(225)
    stage = client.get("/flood-costs/dynamic/stage/290.json").get_json()           # nearest computed stage
    assert stage["stage_cm"] == 300 and stage["requested_stage_cm"] == 290
    one = client.get("/flood-costs/dynamic/stage/150/ACT_RESIDENTIAL1.json").get_json()
    assert one["model1_eur2010"] == pytest.approx(2 * 50 * 100 * .25)
    assert one["model2_72h_eur2010"] > one["model2_6h_eur2010"] > one["model1_eur2010"]
    assert client.get("/flood-costs/dynamic/stage/150/ACT_UNKNOWN01.json").status_code == 404
    whole = client.get("/flood-costs/dynamic/building/ACT_COMMERCIAL1/curve.json").get_json()
    assert len(whole["stages"]) == 5 and whole["stages"][0]["model1_eur2010"] == 0


def test_results_of_an_older_static_run_are_stale(api, database):
    client, _ = api
    _write_curve(database)
    with database.begin() as conn:
        conn.execute(sqlalchemy.text(f"UPDATE {store.LOOKUP} SET dataset_version = 'sha256:new'"))
    curve = client.get("/flood-costs/dynamic/curve.json").get_json()
    assert curve["stale"] and curve["message"]


def test_partial_reach_cuts_totals_and_buildings(api, database, monkeypatch):
    client, routes = api
    _write_curve(database)
    monkeypatch.setattr(routes, "_subset", lambda: {"ACT_RESIDENTIAL1"})
    curve = client.get("/flood-costs/dynamic/curve.json").get_json()
    top = curve["stages"][-1]
    assert curve["partial_reach"] and top["model1_eur2010"] == pytest.approx(2 * 50 * 100 * .6)
    assert top["damaged_buildings"] == 1 and top["known_buildings"] == 1
    assert client.get("/flood-costs/dynamic/stage/300/ACT_COMMERCIAL1.json").status_code == 404


def test_event_routes(api, database):
    client, _ = api
    run, samples, jrc, attributes = _inputs(database)
    meta, totals, buildings = jobs.event(samples, jrc, attributes, FakeDepths(GROUND, samples),
                                         "2021-07-14T00:00:00Z", "2021-07-15T00:00:00Z")
    assert meta["keyframes"] == 5 and meta["distinct_levels"] == 4
    with database.begin() as conn:
        store.write_event(conn, "july", run, meta, totals, buildings)
        store.write_event(conn, "july", run, meta, totals, buildings)            # replaced, not doubled
    listed = client.get("/flood-costs/dynamic/events.json").get_json()
    assert [e["event_id"] for e in listed["events"]] == ["july"]
    event = client.get("/flood-costs/dynamic/event/july.json").get_json()
    model1 = [t["model1_eur2010"] for t in event["timesteps"]]
    assert len(model1) == 5 and model1 == sorted(model1)                         # Model 1 never falls
    assert event["event"]["assumptions"]["duration_beta"]["Residential"] == .3
    series = client.get("/flood-costs/dynamic/event/july/ACT_RESIDENTIAL1.json").get_json()
    assert len(series["timesteps"]) == 5
    last = series["timesteps"][-1]
    assert last["model2_eur2010"] > last["model1_eur2010"]                       # wet for hours -> more
    assert last["model3_total_eur"] == pytest.approx(last["model2_eur2010"] + last["disruption_eur"])
    at = client.get("/flood-costs/dynamic/event/july/ACT_RESIDENTIAL1.json?time=2021-07-14T02:30:00Z").get_json()
    assert at["value_at"]["timestamp"].startswith("2021-07-14T02:00")


def test_event_period_is_validated(api):
    client, _ = api
    assert client.post("/flood-costs/dynamic/event/run", json={"start": "2021-07-15", "end": "2021-07-14"}).status_code == 400
    assert client.post("/flood-costs/dynamic/event/run", json={"start": "2021-07-01", "end": "2021-08-01"}).status_code == 400
    assert client.post("/flood-costs/dynamic/event/run", json={}).status_code == 400


def test_depth_route_serves_the_published_samples(api, database, monkeypatch):
    client, routes = api

    class Local:
        def __init__(self, samples):
            self.n = len(samples)

        stage_key = staticmethod(lambda cm: ("stage", cm))
        parse_key = staticmethod(lambda text: ("key", text))
        key_text = staticmethod(lambda key: f"{key[0]}:{key[1]}")

        def depths(self, key):
            return np.array([0.25] * (self.n - 1) + [np.nan])

    monkeypatch.setattr(routes, "LocalDepths", Local)
    routes._samples_for.cache_clear()
    routes._depths_for.cache_clear()
    answer = client.get("/flood-costs/dynamic/depths.json?stage=120").get_json()
    assert answer["status"] == "success" and answer["dataset_version"] == "sha256:a"
    assert answer["depths_m"] == [0.25, 0.25, None] and answer["samples"] == 3
    assert client.get("/flood-costs/dynamic/depths.json").status_code == 400
    routes._samples_for.cache_clear()
    routes._depths_for.cache_clear()


def test_static_routes_still_registered(api):
    client, _ = api
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert "/flood-costs/popup/<buildingid>.json" in rules
    assert "/flood-costs/dynamic/curve.json" in rules
    assert client.get("/flood-costs/dynamic/parameters.json").get_json()["duration_tau_hours"]["Residential"] == 24.0


def test_event_in_chunks_equals_event_at_once(database, monkeypatch):
    run, samples, jrc, attributes = _inputs(database)
    args = (samples, jrc, attributes, FakeDepths(GROUND, samples), "2021-07-14T00:00:00Z", "2021-07-15T00:00:00Z")
    _, totals_once, buildings_once = jobs.event(*args)
    monkeypatch.setattr(jobs, "CHUNK_SAMPLES", 1)              # one building per chunk
    assert list(jobs._chunks(samples, 1)) and all(
        len(set(samples.index.get_level_values("buildingid")[rows])) == 1 for rows in jobs._chunks(samples, 1))
    _, totals_chunked, buildings_chunked = jobs.event(*args)
    pd.testing.assert_frame_equal(totals_once, totals_chunked)
    pd.testing.assert_frame_equal(buildings_once.sort_values(["buildingid", "timestamp"]).reset_index(drop=True),
                                  buildings_chunked.sort_values(["buildingid", "timestamp"]).reset_index(drop=True))


def test_backend_calls_are_retried_when_the_backend_is_busy(monkeypatch):
    from flood_costs.dynamic import depths as module

    calls = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"status": "success", "times": [], "keys": []}).encode()

    def flaky(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise TimeoutError("timed out")
        return Answer()

    monkeypatch.setattr(module, "urlopen", flaky)
    client = BackendDepths({"dataset_version": "v"}, "sig", 1, base_url="http://backend", attempts=3, wait_s=0)
    assert client.keyframes("2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z")["status"] == "success"
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(module, "urlopen", lambda url, timeout: (_ for _ in ()).throw(TimeoutError("down")))
    with pytest.raises(DepthsUnavailable, match="after 3 attempts"):
        client.keyframes("2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z")


def test_reach_without_covered_buildings_is_not_computed_not_zero(api, database, monkeypatch):
    client, routes = api
    _write_curve(database)
    monkeypatch.setattr(routes, "_subset", lambda: set())          # e.g. km 30-33: no gauge coverage
    for url in ("/flood-costs/dynamic/curve.json", "/flood-costs/dynamic/stage/300.json"):
        answer = client.get(url)
        assert answer.status_code == 404 and "Not computed - not zero" in answer.get_json()["message"]
