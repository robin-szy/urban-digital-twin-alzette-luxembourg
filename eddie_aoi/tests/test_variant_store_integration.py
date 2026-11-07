"""Variant cache against a real PostGIS.

Uses a river name absent from the real marker source, for the same reason as
the reach cache tests: the database is shared with the running stack.
"""
import geopandas as gpd
import pytest
import sqlalchemy as sa
from shapely.geometry import box

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.db import METRIC_SRID, aoi_variant_cache, create_schema, reach_cache
from eddie_aoi.store import save_reach
from eddie_aoi.variant_store import (
    get_or_build_variant,
    is_variant_stale,
    read_variant,
    save_variant,
    touch_variant,
)
from eddie_aoi.variants import (
    MODE_FEATURES,
    LookupVariant,
    clear_source_cache,
    default_registry,
)

pytestmark = pytest.mark.integration

KMS = [35.0 + i for i in range(16)]


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_source_cache()
    yield
    clear_source_cache()


@pytest.fixture
def reach(conn, straight_river, straight_markers):
    r = build_reach_from_geometries(
        "Testbach", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )
    # The variant cache has a foreign key onto the reach.
    save_reach(conn, r)
    return r


def write_layer(path, polygons):
    gpd.GeoDataFrame(
        [{"NAME": f"f{i}", "geometry": g} for i, g in enumerate(polygons)],
        crs=METRIC_CRS,
    ).to_file(path, driver="GPKG")
    return path


# --- 7.1 schema --------------------------------------------------------------


def test_table_exists_with_srid_2169(conn):
    srid = conn.execute(
        sa.text(
            "select srid from geometry_columns "
            "where f_table_name = 'aoi_variant_cache'"
        )
    ).scalar_one()
    assert srid == METRIC_SRID == 2169


def test_schema_creation_is_idempotent(engine):
    with engine.begin() as c:
        create_schema(c)
        create_schema(c)


def test_composite_key_rejects_a_duplicate(conn, reach):
    registry = default_registry()
    resolution = registry.resolve_variant("corridor", reach, half_width_m=300)
    save_variant(conn, reach, resolution, requested_key="corridor_300")

    # A second plain INSERT on the same (reach, variant_key) must be refused.
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            aoi_variant_cache.insert().values(
                reach_id=reach.reach_id,
                variant_key="corridor_300",
                geometry=sa.func.ST_GeomFromText(
                    resolution.frame.geometry.iloc[0].wkt, METRIC_SRID
                ),
                variant="corridor",
                requested_variant="corridor",
            )
        )


def test_variants_are_removed_with_their_reach(conn, reach):
    registry = default_registry()
    get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)
    assert read_variant(conn, reach.reach_id, "corridor_300") is not None

    conn.execute(reach_cache.delete().where(reach_cache.c.reach_id == reach.reach_id))

    assert read_variant(conn, reach.reach_id, "corridor_300") is None


# --- 7.2 caching keyed on parameters ---------------------------------------


def test_round_trips_geometry(conn, reach):
    registry = default_registry()
    cached = get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)

    direct = registry.build("corridor", reach, half_width_m=300).geometry.iloc[0]
    assert cached.geometry.area == pytest.approx(direct.area, rel=1e-9)
    assert cached.variant == "corridor"
    assert cached.params == {"half_width_m": 300.0}
    assert cached.degraded is False


def test_repeat_request_is_a_cache_hit(conn, reach, monkeypatch):
    registry = default_registry()
    get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)

    calls = []
    original = registry.resolve_variant

    def counting(*a, **k):
        calls.append(1)
        return original(*a, **k)

    monkeypatch.setattr(registry, "resolve_variant", counting)
    get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)

    assert not calls, "a current cached variant must not be rebuilt"


def test_different_parameters_are_a_separate_entry(conn, reach):
    registry = default_registry()
    narrow = get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)
    wide = get_or_build_variant(conn, reach, registry, "corridor", half_width_m=500)

    assert narrow.variant_key == "corridor_300"
    assert wide.variant_key == "corridor_500"
    assert wide.geometry.area > narrow.geometry.area

    rows = conn.execute(
        sa.select(sa.func.count())
        .select_from(aoi_variant_cache)
        .where(aoi_variant_cache.c.reach_id == reach.reach_id)
    ).scalar_one()
    assert rows == 2, "parameters are part of the key"


def test_cache_hit_updates_last_used(conn, reach):
    registry = default_registry()
    get_or_build_variant(conn, reach, registry, "corridor", half_width_m=300)

    def last_used():
        # Scope to this test's reach: the shared database also holds real
        # reaches with a corridor_300 row of their own.
        return conn.execute(
            sa.select(aoi_variant_cache.c.last_used_at).where(
                sa.and_(
                    aoi_variant_cache.c.reach_id == reach.reach_id,
                    aoi_variant_cache.c.variant_key == "corridor_300",
                )
            )
        ).scalar_one()

    before = last_used()
    touch_variant(conn, reach.reach_id, "corridor_300")
    assert last_used() >= before


def test_multipolygon_geometry_survives(conn, reach, tmp_path):
    # Lookup variants dissolve to a MultiPolygon; the column must accept it.
    path = write_layer(
        tmp_path / "ws.gpkg",
        [box(0, -500, 4000, 500), box(9000, -500, 15000, 500)],  # disjoint
    )
    registry = default_registry()
    registry.register(
        LookupVariant(name="watersheds", source_path=path, mode=MODE_FEATURES)
    )

    cached = get_or_build_variant(conn, reach, registry, "watersheds")
    assert cached.geometry.geom_type == "MultiPolygon"


# --- 7.3 invalidation -------------------------------------------------------


def test_unchanged_source_serves_from_cache(conn, reach, tmp_path):
    path = write_layer(tmp_path / "ws.gpkg", [box(0, -500, 15000, 500)])
    registry = default_registry()
    registry.register(LookupVariant(name="watersheds", source_path=path))

    first = get_or_build_variant(conn, reach, registry, "watersheds")
    second = get_or_build_variant(conn, reach, registry, "watersheds")

    assert second.source_sha256 == first.source_sha256
    assert second.geometry.area == pytest.approx(first.geometry.area)


def test_changed_source_triggers_a_rebuild(conn, reach, tmp_path):
    path = tmp_path / "ws.gpkg"
    write_layer(path, [box(0, -500, 15000, 500)])
    registry = default_registry()
    registry.register(LookupVariant(name="watersheds", source_path=path))
    before = get_or_build_variant(conn, reach, registry, "watersheds")

    clear_source_cache()
    write_layer(path, [box(0, -100, 15000, 100)])  # republished, smaller
    after = get_or_build_variant(conn, reach, registry, "watersheds")

    assert after.source_sha256 != before.source_sha256
    assert after.geometry.area < before.geometry.area


def test_a_degraded_entry_is_rebuilt_once_its_source_appears(conn, reach, tmp_path):
    # Watersheds unavailable -> degraded substitute gets cached.
    degraded_registry = default_registry()
    degraded = get_or_build_variant(conn, reach, degraded_registry, "watersheds")

    assert degraded.degraded is True
    assert degraded.variant == "buffer"
    assert degraded.requested_variant == "watersheds"

    # The real source arrives. The entry's own hash has not changed, so only
    # the degraded marker can tell us to rebuild.
    path = write_layer(tmp_path / "ws.gpkg", [box(0, -500, 15000, 500)])
    real_registry = default_registry(watersheds_path=path)

    rebuilt = get_or_build_variant(conn, reach, real_registry, "watersheds")

    assert rebuilt.degraded is False
    assert rebuilt.variant == "watersheds"
    assert rebuilt.geometry.area != pytest.approx(degraded.geometry.area)


def test_a_degraded_entry_is_still_served_while_the_source_is_missing(conn, reach):
    registry = default_registry()
    first = get_or_build_variant(conn, reach, registry, "watersheds")
    second = get_or_build_variant(conn, reach, registry, "watersheds")

    assert second.degraded is True
    assert second.geometry.area == pytest.approx(first.geometry.area)


def test_degradation_reason_is_persisted(conn, reach):
    cached = get_or_build_variant(conn, reach, default_registry(), "watersheds")
    assert cached.degraded_reason
    assert "has not been supplied yet" in cached.degraded_reason


def test_staleness_rules(conn, reach):
    from eddie_aoi.variant_store import CachedVariant

    registry = default_registry()
    base = dict(
        reach_id=reach.reach_id, variant_key="corridor_300", variant="corridor",
        requested_variant="corridor", geometry=None, params={},
        degraded=False, degraded_reason=None,
    )
    assert is_variant_stale(CachedVariant(source_sha256="a", **base), registry, "b")
    assert not is_variant_stale(CachedVariant(source_sha256="a", **base), registry, "a")
    # Nothing to compare against: do not rebuild on every request.
    assert not is_variant_stale(CachedVariant(source_sha256=None, **base), registry, "b")
    assert not is_variant_stale(CachedVariant(source_sha256="a", **base), registry, None)


def test_cached_variant_converts_to_the_provider_frame_shape(conn, reach):
    cached = get_or_build_variant(
        conn, reach, default_registry(), "corridor", half_width_m=300
    )
    frame = cached.to_frame()

    assert len(frame) == 1
    assert frame.crs.to_epsg() == 2169
    for column in ("variant", "variant_key", "params", "reach_id", "degraded"):
        assert column in frame.columns
