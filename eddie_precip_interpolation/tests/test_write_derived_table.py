import os

import geopandas as gpd
import pytest
import sqlalchemy as sa
from shapely.geometry import Point

from eddie_precip_interpolation import db

DERIVED_TABLE = "precipitation_doubled_test"


def _derived_gdf(value_doubled: float) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"station_id": ["AGM_013"], "datetime": ["2026-09-25T01:00:00Z"], "value_doubled": [value_doubled]},
        geometry=[Point(6.13, 49.58)],
        crs="EPSG:4326",
    )


@pytest.mark.integration
def test_write_derived_table_rerun_does_not_duplicate_rows():
    password = os.environ.get("TEST_POSTGRES_PASSWORD")
    if not password:
        pytest.skip("set TEST_POSTGRES_PASSWORD to run against the local db_postgres_digital_twin container")

    # Explicit +psycopg2: this host venv has psycopg2-binary installed but not
    # psycopg (v3), and SQLAlchemy 2.x's bare "postgresql://" here resolves to
    # the v3 dialect by default - a pre-existing host-environment quirk also
    # hit by eddie_alzette_ingest's own integration test, not introduced here.
    engine = sa.create_engine(f"postgresql+psycopg2://postgres:{password}@localhost:5431/db")

    with engine.connect() as conn:
        db.ensure_derived_table(conn, DERIVED_TABLE)
        try:
            first = db.write_derived_table(conn, _derived_gdf(3.0), DERIVED_TABLE)
            assert first == 1

            # Re-run with a different value for the same (station_id, datetime):
            # row count must stay at 1 (no duplicate), and the overwrite must
            # have taken effect (recompute wins, unlike append-only observations).
            db.write_derived_table(conn, _derived_gdf(99.0), DERIVED_TABLE)

            table = sa.Table(DERIVED_TABLE, sa.MetaData(), autoload_with=conn)
            count = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()
            assert count == 1

            row = conn.execute(sa.select(table.c.value_doubled)).first()
            assert row[0] == 99.0
        finally:
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {DERIVED_TABLE}"))
            conn.commit()
