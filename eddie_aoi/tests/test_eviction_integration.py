"""Eviction (tasks 11.1, 11.2), against real PostGIS inside a rolled-back transaction.

The test reaches are aged artificially to the year 2000 and eviction asks for "older
than 20 years", so only they qualify - real reaches on the shared database are never
candidates, and the rollback undoes everything anyway.
"""
from datetime import timedelta

import pytest
import sqlalchemy as sa

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.config import FALLBACK_REACH_ENV
from eddie_aoi.current import set_current
from eddie_aoi.db import aoi_current, aoi_variant_cache, reach_cache
from eddie_aoi.eviction import derived_tables, evict
from eddie_aoi.store import read_cached, save_reach, set_pinned
from eddie_aoi.variant_store import get_or_build_variant
from eddie_aoi.variants import clear_source_cache, default_registry

pytestmark = pytest.mark.integration

OLD = "2000-01-01T00:00:00+00:00"
AGE = timedelta(days=365 * 20)
ENV = {FALLBACK_REACH_ENV: "Testbach:35.0:50.0"}
DERIVED = "test_aoi_eviction_derived"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_source_cache()
    yield
    clear_source_cache()


def _reach(conn, straight_river, km_start, km_end):
    # A sub-range of the 15 km synthetic river: markers km_start..km_end, 1 km apart.
    markers = [m for m in _markers() if km_start <= 35.0 + m.x / 1000 <= km_end]
    kms = [35.0 + m.x / 1000 for m in markers]
    r = build_reach_from_geometries("Testbach", km_start, km_end, straight_river, markers, kms)
    save_reach(conn, r)
    get_or_build_variant(conn, r, default_registry(), "corridor", half_width_m=500.0)
    conn.execute(sa.update(reach_cache).where(reach_cache.c.reach_id == r.reach_id)
                 .values(last_used_at=OLD))
    return r


def _markers():
    from shapely.geometry import Point
    return [Point(float(x), 2.0) for x in range(0, 15001, 1000)]


@pytest.fixture
def derived(conn):
    """A scratch derived table shaped like write_derived's output (rolled back with the test)."""
    conn.execute(sa.text(f'CREATE TABLE "{DERIVED}" (reach_id text, variant_key text, value int)'))
    return DERIVED


def _rows(conn, reach_id):
    return conn.execute(sa.text(f'SELECT count(*) FROM "{DERIVED}" WHERE reach_id = :r'),
                        {"r": reach_id}).scalar_one()


def test_derived_tables_are_discovered_and_aoi_tables_are_not(conn, derived):
    tables = derived_tables(conn)
    assert derived in tables
    for own in ("reach_cache", "aoi_variant_cache", "aoi_current"):
        assert own not in tables


def test_unused_reach_is_removed_with_its_polygons_and_derived_rows(conn, straight_river, derived):
    conn.execute(sa.delete(aoi_current))
    r = _reach(conn, straight_river, 40.0, 45.0)
    conn.execute(sa.text(f"INSERT INTO \"{derived}\" VALUES (:r, 'corridor_500', 1), (:r, 'corridor_500', 2)"),
                 {"r": r.reach_id})

    report = evict(conn, AGE, env=ENV)

    removed = {e["reach_id"]: e for e in report["removed"]}
    assert r.reach_id in removed
    assert removed[r.reach_id]["derived_rows_removed"] == {derived: 2}
    assert read_cached(conn, r.reach_id) is None
    assert _rows(conn, r.reach_id) == 0
    assert conn.execute(sa.select(sa.func.count()).select_from(aoi_variant_cache)
                        .where(aoi_variant_cache.c.reach_id == r.reach_id)).scalar_one() == 0


def test_pinned_fallback_is_retained(conn, straight_river, derived):
    conn.execute(sa.delete(aoi_current))
    r = _reach(conn, straight_river, 35.0, 50.0)
    set_pinned(conn, r.reach_id, True)

    report = evict(conn, AGE, env=ENV)

    retained = {e["reach_id"]: e["reason"] for e in report["retained"]}
    assert "pinned" in retained[r.reach_id]
    assert read_cached(conn, r.reach_id) is not None


def test_current_reach_and_its_derived_rows_are_retained(conn, straight_river, derived):
    r = _reach(conn, straight_river, 36.0, 44.0)
    set_current(conn, r.reach_id)
    conn.execute(sa.update(reach_cache).where(reach_cache.c.reach_id == r.reach_id)
                 .values(last_used_at=OLD))
    conn.execute(sa.text(f"INSERT INTO \"{derived}\" VALUES (:r, 'corridor_500', 1)"), {"r": r.reach_id})

    report = evict(conn, AGE, env=ENV)

    retained = {e["reach_id"]: e["reason"] for e in report["retained"]}
    assert retained[r.reach_id] == "the current reach"
    assert read_cached(conn, r.reach_id) is not None
    assert _rows(conn, r.reach_id) == 1


def test_recently_used_reach_is_not_a_candidate(conn, straight_river, derived):
    r = _reach(conn, straight_river, 41.0, 43.0)
    conn.execute(sa.update(reach_cache).where(reach_cache.c.reach_id == r.reach_id)
                 .values(last_used_at=sa.func.now()))

    report = evict(conn, AGE, env=ENV)

    assert r.reach_id not in {e["reach_id"] for e in report["removed"] + report["retained"]}


def test_age_must_be_positive(conn):
    with pytest.raises(ValueError, match="positive"):
        evict(conn, timedelta(0), env=ENV)
