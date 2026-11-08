"""Write-back against the live PostGIS, on a scratch table. Run inside the worker container."""
import geopandas as gpd
import pytest
import sqlalchemy as sa
from shapely.geometry import MultiPolygon, box

from eddie_base_data.publish import view_name, write_derived
from eddie_base_data.query import AoiScope, read_in_aoi

TABLE = "test_base_data_derived"
REACH = AoiScope("testbach_000000_001000", "bbox_500")
OTHER_VARIANT = AoiScope("testbach_000000_001000", "corridor_300", degraded=True, degraded_reason="source missing")


@pytest.fixture
def engine():
    from eddie.digitaltwin import setup_environment
    engine = setup_environment.get_database()
    yield engine
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP VIEW IF EXISTS "{view_name(TABLE)}"'))
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{TABLE}"'))


def frame(value, geometry=None):
    return gpd.GeoDataFrame({"value": [value]}, geometry=[geometry or box(77000, 75000, 77010, 75010)], crs=2169)


def rows(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text(f'SELECT variant_key, value, degraded, degraded_reason FROM "{TABLE}" '
                                    "ORDER BY variant_key")).all()


def test_rerun_replaces_its_own_scope_and_keeps_others(engine):
    write_derived(engine, TABLE, frame(1.0), REACH)
    write_derived(engine, TABLE, frame(2.0), OTHER_VARIANT)
    write_derived(engine, TABLE, frame(3.0), REACH)
    assert rows(engine) == [("bbox_500", 3.0, False, None), ("corridor_300", 2.0, True, "source missing")]


def test_failed_write_keeps_the_previous_result_and_view(engine):
    write_derived(engine, TABLE, frame(1.0), REACH)
    with pytest.raises(Exception, match="not_a_column"):
        write_derived(engine, TABLE, frame(2.0).assign(not_a_column=1), REACH)
    assert rows(engine) == [("bbox_500", 1.0, False, None)]
    with engine.connect() as conn:
        assert conn.execute(sa.text(f'SELECT count(*) FROM "{view_name(TABLE)}"')).scalar_one() == 1


def test_a_later_scope_may_have_multipart_geometry(engine):
    write_derived(engine, TABLE, frame(1.0), REACH)
    multi = MultiPolygon([box(77000, 75000, 77010, 75010), box(78000, 75000, 78010, 75010)])
    write_derived(engine, TABLE, frame(2.0, multi), OTHER_VARIANT)
    assert len(rows(engine)) == 2


def test_table_from_before_attribution_is_replaced(engine):
    with engine.begin() as conn:
        frame(9.0).to_postgis(TABLE, conn, if_exists="replace", index=False)
    write_derived(engine, TABLE, frame(1.0), REACH)
    assert rows(engine) == [("bbox_500", 1.0, False, None)]


def test_table_stays_in_2169_and_only_the_view_is_4326(engine):
    write_derived(engine, TABLE, frame(1.0), REACH)
    with engine.connect() as conn:
        table_srid, view_srid, x = conn.execute(sa.text(
            f'SELECT (SELECT ST_SRID(geometry) FROM "{TABLE}"), ST_SRID(geometry), ST_X(ST_Centroid(geometry)) '
            f'FROM "{view_name(TABLE)}"')).one()
    assert (table_srid, view_srid) == (2169, 4326)
    assert 5 < x < 7


def test_other_crs_is_refused(engine):
    with pytest.raises(ValueError, match="2169"):
        write_derived(engine, TABLE, frame(1.0).to_crs(4326), REACH)


def test_read_in_aoi_filters_in_sql(engine):
    near, far = box(77000, 75000, 77010, 75010), box(90000, 90000, 90010, 90010)
    with engine.begin() as conn:
        gpd.GeoDataFrame({"value": [1, 2]}, geometry=[near, far], crs=2169).to_postgis(TABLE, conn, index=False,
                                                                                         if_exists="replace")
    with engine.connect() as conn:
        assert read_in_aoi(conn, TABLE, box(76000, 74000, 78000, 76000))["value"].tolist() == [1]
        assert read_in_aoi(conn, TABLE, box(77020, 75000, 77030, 75010))["value"].tolist() == []
        assert read_in_aoi(conn, TABLE, box(77020, 75000, 77030, 75010), margin_m=15)["value"].tolist() == [1]
