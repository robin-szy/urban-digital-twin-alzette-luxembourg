"""The current reach (tasks 4.6, 4.7), against real PostGIS.

Every test runs inside a rolled-back transaction (see conftest.conn). The live
database may already hold a current reach, so tests that need "no selection yet"
clear the row first - inside the same transaction, so nothing real changes.
"""
import pytest
import sqlalchemy as sa

from eddie_aoi.config import FALLBACK_REACH_ENV
from eddie_aoi.current import current_reach, set_current
from eddie_aoi.db import aoi_current
from eddie_aoi.errors import PreparationError
from eddie_aoi.store import get_or_build, mark_failed
from eddie_aoi.builder import build_reach

from .conftest import write_river_sources

pytestmark = pytest.mark.integration

ENV = {FALLBACK_REACH_ENV: "Testbach:35.0:50.0"}


def _build(conn, sources, km_start, km_end):
    return get_or_build(
        conn, "Testbach", km_start, km_end,
        lambda: build_reach(sources, "Testbach", km_start, km_end),
    )


def _clear(conn):
    conn.execute(sa.delete(aoi_current))


def test_no_selection_means_the_fallback_is_current(conn):
    _clear(conn)

    current = current_reach(conn, env=ENV)

    assert current.reach_id == "testbach_035000_050000"
    assert current.is_fallback is True


def test_a_built_selection_becomes_current(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    _clear(conn)
    reach = _build(conn, sources, 40.0, 45.0)

    set_current(conn, reach.reach_id)

    current = current_reach(conn, env=ENV)
    assert current.reach_id == "testbach_040000_045000"
    assert current.is_fallback is False
    assert current.selected_at is not None


def test_selecting_the_fallback_range_is_not_reported_as_the_fallback(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    _clear(conn)
    reach = _build(conn, sources, 35.0, 50.0)

    set_current(conn, reach.reach_id)

    assert current_reach(conn, env=ENV).is_fallback is False


def test_a_failed_build_cannot_become_current_and_leaves_it_unchanged(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    _clear(conn)
    good = _build(conn, sources, 40.0, 45.0)
    set_current(conn, good.reach_id)
    failed_id = mark_failed(conn, "Testbach", 41.0, 44.0, "markers out of order")

    with pytest.raises(PreparationError, match="cannot become the current reach"):
        set_current(conn, failed_id)

    assert current_reach(conn, env=ENV).reach_id == good.reach_id


def test_an_unknown_reach_cannot_become_current(conn):
    with pytest.raises(PreparationError, match="cannot become the current reach"):
        set_current(conn, "testbach_000001_000002")


def test_a_new_selection_replaces_the_previous_one(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    _clear(conn)
    first = _build(conn, sources, 40.0, 45.0)
    second = _build(conn, sources, 36.0, 39.0)

    set_current(conn, first.reach_id)
    set_current(conn, second.reach_id)

    assert current_reach(conn, env=ENV).reach_id == second.reach_id
    assert conn.execute(sa.select(sa.func.count()).select_from(aoi_current)).scalar_one() == 1


def test_the_database_refuses_to_delete_the_current_reach(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    _clear(conn)
    reach = _build(conn, sources, 40.0, 45.0)
    set_current(conn, reach.reach_id)

    savepoint = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.text("DELETE FROM reach_cache WHERE reach_id = :r"),
                     {"r": reach.reach_id})
    savepoint.rollback()
