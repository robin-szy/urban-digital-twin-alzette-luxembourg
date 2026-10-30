import sqlalchemy as sa
import pytest


@pytest.fixture
def sqlite_conn():
    # Only the geometry-free observations read is exercised here - the
    # geometry-bearing functions (read_station_geometries, write_derived_table)
    # need real PostGIS and are integration-tested, same split as
    # eddie_alzette_ingest/tests/conftest.py uses for its own stations table.
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        yield conn
