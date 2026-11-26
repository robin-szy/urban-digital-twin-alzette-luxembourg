"""Selecting an area (task 12.2): the river is fixed to the Alzette, and a bad selection is
refused with its reason BEFORE anything is built, leaving the current area as it was.

The unit tests need nothing; the endpoint tests need PostGIS (the current reach is read
before and after) and skip without it.
"""
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import RIVER, validate_selection

ALZETTE = (0.1, 68.4)   # first and last Alzette marker in the real source


# --- unit --------------------------------------------------------------------


def test_a_valid_selection_is_returned_rounded_to_the_markers():
    assert validate_selection(None, 37, "39.0", ALZETTE) == (RIVER, 37.0, 39.0)
    assert validate_selection("Alzette", 35.0000004, 50, ALZETTE) == (RIVER, 35.0, 50.0)


@pytest.mark.parametrize(
    "river, km_start, km_end, reason",
    [
        ("Sûre", 35, 50, "Only the Alzette is available"),
        (None, 50, 35, "increasing kilometre range"),
        (None, 40, 40, "increasing kilometre range"),
        (None, 0.0, 10, "outside the Alzette's kilometre markers (km 0.1-68.4)"),
        (None, 60, 70, "km_end 70 is outside"),
        (None, 37.25, 39, "lies between two markers"),
        (None, "abc", 39, "'km_start' must be a number"),
        (None, None, 39, "Provide 'km_start'"),
        (None, 37, "", "Provide 'km_end'"),
    ],
)
def test_each_bad_selection_is_refused_with_its_reason(river, km_start, km_end, reason):
    with pytest.raises(PreparationError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        validate_selection(river, km_start, km_end, ALZETTE)


def test_the_range_is_not_checked_when_the_marker_source_is_unreadable():
    assert validate_selection(None, 70, 75, None) == (RIVER, 70.0, 75.0)


# --- endpoint (PostGIS) --------------------------------------------------------


@pytest.fixture
def client(engine, monkeypatch):
    flask = pytest.importorskip("flask")
    from eddie_aoi import blueprint as module
    from eddie_aoi import sources, tasks

    sent = []
    monkeypatch.setattr(sources, "river_km_range", lambda *_: ALZETTE)
    def fake_delay(*args, **kwargs):
        sent.append(args)
        return SimpleNamespace(id="test-task")

    monkeypatch.setattr(tasks.build_aoi_reach, "delay", fake_delay)
    app = flask.Flask(__name__)
    app.register_blueprint(module.blueprint)
    test_client = app.test_client()
    test_client.sent = sent
    return test_client


def _current(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT reach_id FROM aoi_current")).scalar()


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"river": "Sûre", "km_start": 35, "km_end": 50}, "Only the Alzette"),
        ({"km_start": 50, "km_end": 35}, "increasing kilometre range"),
        ({"km_start": 60, "km_end": 70}, "outside the Alzette"),
        ({"km_start": 37.25, "km_end": 39}, "between two markers"),
        ({"km_end": 39}, "Provide 'km_start'"),
    ],
)
def test_a_refused_selection_builds_nothing_and_keeps_the_current_area(client, engine, payload, reason):
    before = _current(engine)

    response = client.post("/aoi/reach", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert reason in body["message"]
    assert body["river"] == RIVER and body["km_range"] == list(ALZETTE)
    assert client.sent == []              # no build queued
    assert _current(engine) == before     # the served area is unchanged


@pytest.mark.integration
def test_a_valid_selection_queues_an_alzette_build(client, engine):
    response = client.post("/aoi/reach", json={"km_start": 37, "km_end": 39})

    assert response.status_code == 202
    assert response.get_json()["reach_id"] == "alzette_037000_039000"
    assert client.sent == [(RIVER, 37.0, 39.0)]
