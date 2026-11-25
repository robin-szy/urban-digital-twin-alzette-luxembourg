"""The data API and the task module, with a stand-in for EDDIE (SQLite database, eager Celery)."""

import datetime as dt
import sys
import types

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

flask = pytest.importorskip("flask")
celery = pytest.importorskip("celery")

from eddie_weather import alzette, db, pipeline  # noqa: E402
from weather_fakes import TODAY, FakeOpenMeteo, nested_catchments  # noqa: E402

ENGINE = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})


@pytest.fixture(scope="module", autouse=True)
def fake_eddie():
    """Just enough of ``eddie`` for blueprint.py and tasks.py to import and run."""
    app = celery.Celery("test")
    app.conf.task_always_eager = True
    eddie = types.ModuleType("eddie")
    digitaltwin = types.ModuleType("eddie.digitaltwin")
    setup_environment = types.ModuleType("eddie.digitaltwin.setup_environment")
    setup_environment.get_database = lambda: ENGINE
    tasks = types.ModuleType("eddie.tasks")
    tasks.app = app
    digitaltwin.setup_environment = setup_environment
    eddie.digitaltwin, eddie.tasks = digitaltwin, tasks
    saved = {k: sys.modules.get(k) for k in ("eddie", "eddie.digitaltwin", "eddie.digitaltwin.setup_environment", "eddie.tasks")}
    sys.modules.update({"eddie": eddie, "eddie.digitaltwin": digitaltwin,
                        "eddie.digitaltwin.setup_environment": setup_environment, "eddie.tasks": tasks})
    with ENGINE.begin() as conn:
        db.ensure_tables(conn)
        places = pipeline.setup_places(conn, nested_catchments())
        pipeline.fetch_archive(conn, places, dt.date(2026, 10, 1), dt.date(2026, 11, 15), fetch=FakeOpenMeteo(), today=TODAY)
    yield app
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture
def client():
    from eddie_weather.blueprint import blueprint
    app = flask.Flask("test")
    app.register_blueprint(blueprint)
    return app.test_client()


def test_places_geojson(client):
    answer = client.get("/weather/places").get_json()
    assert len(answer["features"]) == 12
    subs = client.get("/weather/places?kind=sub_catchment").get_json()["features"]
    assert {f["properties"]["gauge_id"] for f in subs} == {1, 2, 3, 4, 5, 6}


def test_series_of_a_week_and_of_the_gauge_catchment(client):
    sub = alzette.BY_ID[1].place_id
    response = client.get(f"/weather/series?places={sub}&start=2026-11-01T00:00Z&end=2026-11-08T00:00Z")
    body = response.get_json()
    assert response.status_code == 200 and body["level"] == "hour"
    assert len(body["places"][sub]["values"]) == 168 and "volume_m3" in body["places"][sub]
    assert "max-age=86400" in response.headers["Cache-Control"]
    whole = alzette.BY_ID[6].catchment_id
    body = client.get(f"/weather/series?places={whole}&start=2026-11-01T00:00Z&end=2026-11-08T00:00Z").get_json()
    assert body["level"] == "day" and len(body["places"][whole]["values"]) == 7


def test_series_refuses_bad_requests(client):
    assert client.get("/weather/series?start=2026-11-01").status_code == 400
    assert client.get("/weather/series?start=2026-11-01&end=2026-11-02&variable=snow").status_code == 400
    assert client.get("/weather/series?places=nowhere&start=2026-11-01&end=2026-11-02").status_code == 404


def test_value_at_a_time_for_the_map(client):
    body = client.get("/weather/at?time=2026-11-03T12:30Z&level=hour").get_json()
    assert len(body["places"]) == 6  # hours exist for the sub-catchments only
    assert all(v["time"].startswith("2026-11-03T12:00") for v in body["places"].values())
    days = client.get("/weather/at?time=2026-11-03T12:30Z&level=day").get_json()
    assert len(days["places"]) == 12


def test_status(client):
    body = client.get("/weather/status").get_json()
    assert body["places"] == 12
    assert any(c["variable"] == "rain" and c["level"] == "hour" for c in body["coverage"])


def test_tasks_module_imports_and_schedules(fake_eddie):
    from eddie_weather import tasks
    names = {entry["task"] for entry in fake_eddie.conf.beat_schedule.values()}
    assert {"eddie_weather.tasks.update_forecast", "eddie_weather.tasks.update_flow",
            "eddie_weather.tasks.backfill", "eddie_weather.tasks.catch_up"} <= names
    assert tasks.BUDGET.per_day < 10000


def test_run_rejects_unknown_steps(client):
    assert client.post("/weather/run/everything").status_code == 404
    assert client.post("/weather/run/calibrate", json={}).status_code == 400
