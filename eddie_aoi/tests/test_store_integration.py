"""Reach cache against a real PostGIS.

Marked integration: needs db_postgres_digital_twin running. Each test works
in a transaction that is rolled back, so the shared database is left as it
was found.

They use a river name that does not exist in the real marker source. The
database is shared with the running stack, which holds genuine reaches for
real rivers - a test asserting a cache MISS would otherwise fail as soon as
someone built that same reach for real.
"""
import pytest
import sqlalchemy as sa

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.db import METRIC_SRID, STATUS_BUILDING, STATUS_FAILED, STATUS_READY, create_schema, get_engine, reach_cache
from eddie_aoi.store import (
    CachedReach,
    ReachNotReady,
    get_or_build,
    is_stale,
    mark_building,
    mark_failed,
    read_cached,
    save_reach,
    set_pinned,
    touch,
)

from .conftest import markers_at

pytestmark = pytest.mark.integration

KMS = [35.0 + i for i in range(16)]


@pytest.fixture
def reach(straight_river, straight_markers):
    return build_reach_from_geometries(
        "Testbach", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )


# --- 4.1 schema --------------------------------------------------------------


def test_schema_exists_and_geometry_is_srid_2169(conn):
    srid = conn.execute(
        sa.text(
            "select srid from geometry_columns "
            "where f_table_name = 'reach_cache' and f_geometry_column = 'centerline'"
        )
    ).scalar_one()
    assert srid == METRIC_SRID == 2169


def test_schema_creation_is_idempotent(engine):
    # Additive only: running it again against a populated database is safe.
    with engine.begin() as conn:
        create_schema(conn)
        create_schema(conn)


def test_range_constraint_rejects_a_reversed_reach(conn):
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            reach_cache.insert().values(
                reach_id="x_050000_035000",
                river="Testbach",
                km_start=50.0,
                km_end=35.0,
                status=STATUS_READY,
            )
        )


def test_status_constraint_rejects_an_unknown_status(conn):
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            reach_cache.insert().values(
                reach_id="x_035000_050000",
                river="Testbach",
                km_start=35.0,
                km_end=50.0,
                status="nonsense",
            )
        )


# --- 4.2 persistence, reuse and status --------------------------------------


def test_saves_and_reads_back_a_reach(conn, reach):
    save_reach(conn, reach)
    cached = read_cached(conn, reach.reach_id)

    assert cached.is_ready
    assert cached.reach.reach_id == reach.reach_id
    assert cached.reach.centerline.length == pytest.approx(reach.centerline.length)
    assert cached.reach.marker_km == reach.marker_km
    # Chainage survives the round trip, so the marker source is not re-read.
    assert cached.reach.position_for_km(42.5) == pytest.approx(7500.0)


def test_cache_hit_does_not_rebuild(conn, reach):
    save_reach(conn, reach)
    calls = []

    def build():
        calls.append(1)
        return reach

    returned = get_or_build(conn, "Testbach", 35.0, 50.0, build,
                            current_source_sha256="sha-one")

    assert not calls, "a ready, current cache entry must not rebuild"
    assert returned.reach_id == reach.reach_id


def test_cache_hit_updates_last_used(conn, reach):
    save_reach(conn, reach)
    before = conn.execute(
        sa.select(reach_cache.c.last_used_at).where(
            reach_cache.c.reach_id == reach.reach_id
        )
    ).scalar_one()

    touch(conn, reach.reach_id)

    after = conn.execute(
        sa.select(reach_cache.c.last_used_at).where(
            reach_cache.c.reach_id == reach.reach_id
        )
    ).scalar_one()
    assert after >= before


def test_a_building_reach_is_not_served(conn, reach):
    mark_building(conn, "Testbach", 35.0, 50.0)

    cached = read_cached(conn, reach.reach_id)
    assert cached.status == STATUS_BUILDING
    assert cached.reach is None, "no geometry may be served while building"

    with pytest.raises(ReachNotReady, match="still building"):
        get_or_build(conn, "Testbach", 35.0, 50.0, lambda: reach)


def test_a_failed_reach_returns_its_recorded_reason(conn, reach):
    mark_failed(conn, "Testbach", 35.0, 50.0, "Markers do not follow the river route")

    with pytest.raises(ReachNotReady, match="Markers do not follow the river route"):
        get_or_build(conn, "Testbach", 35.0, 50.0, lambda: reach)


def test_a_failed_reach_loses_any_previous_geometry(conn, reach):
    save_reach(conn, reach)
    mark_failed(conn, "Testbach", 35.0, 50.0, "source changed and no longer validates")

    cached = read_cached(conn, reach.reach_id)
    assert cached.status == STATUS_FAILED
    assert cached.reach is None


def test_build_failure_is_recorded_and_reraised(conn):
    def build():
        raise ValueError("mapped river is disconnected")

    with pytest.raises(ValueError, match="disconnected"):
        get_or_build(conn, "Testbach", 35.0, 50.0, build)

    cached = read_cached(conn, "testbach_035000_050000")
    assert cached.status == STATUS_FAILED
    assert "disconnected" in cached.error


def test_a_miss_builds_and_persists(conn, reach):
    calls = []

    def build():
        calls.append(1)
        return reach

    get_or_build(conn, "Testbach", 35.0, 50.0, build)

    assert len(calls) == 1
    assert read_cached(conn, reach.reach_id).is_ready


# --- 4.3 source-hash staleness ----------------------------------------------


def test_unchanged_source_serves_from_cache(conn, reach):
    save_reach(conn, reach)
    calls = []

    get_or_build(conn, "Testbach", 35.0, 50.0,
                 lambda: calls.append(1) or reach,
                 current_source_sha256="sha-one")

    assert not calls


def test_changed_source_triggers_a_rebuild(conn, reach):
    save_reach(conn, reach)
    calls = []

    def build():
        calls.append(1)
        return reach

    get_or_build(conn, "Testbach", 35.0, 50.0, build,
                 current_source_sha256="sha-two")

    assert len(calls) == 1, "a superseded source must rebuild"


def test_staleness_needs_both_hashes():
    base = dict(
        reach_id="r", river="Testbach", km_start=35.0, km_end=50.0,
        status=STATUS_READY, pinned=False, error=None, reach=object(),
    )
    assert is_stale(CachedReach(source_sha256="a", **base), "b")
    assert not is_stale(CachedReach(source_sha256="a", **base), "a")
    # Nothing to compare: do not rebuild on every request.
    assert not is_stale(CachedReach(source_sha256=None, **base), "b")
    assert not is_stale(CachedReach(source_sha256="a", **base), None)


# --- pinning -----------------------------------------------------------------


def test_pinning_is_persisted(conn, reach):
    save_reach(conn, reach, pinned=True)
    assert read_cached(conn, reach.reach_id).pinned is True

    set_pinned(conn, reach.reach_id, False)
    assert read_cached(conn, reach.reach_id).pinned is False


def test_resaving_does_not_clear_an_existing_pin(conn, reach):
    save_reach(conn, reach, pinned=True)
    save_reach(conn, reach)  # a plain rebuild must not unpin the fallback

    assert read_cached(conn, reach.reach_id).pinned is True
