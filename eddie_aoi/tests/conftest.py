"""Synthetic river and marker fixtures, in metres as EPSG:2169 would be.

Real sources are not needed to pin the routing invariants, and synthetic
geometry is preferable for it: each fixture violates exactly one invariant,
which a real extract cannot be made to do.
"""
import os

import geopandas as gpd
import pytest
import sqlalchemy as sa
from shapely.geometry import LineString, Point

from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.db import create_schema, get_engine
from eddie_aoi.sources import MARKER_LAYER, RiverSources


@pytest.fixture(scope="session")
def engine():
    """The live PostGIS engine, or a skip when it is not reachable."""
    if not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("POSTGRES_PASSWORD is not set; cannot reach PostGIS")
    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.execute(sa.text("select 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostGIS is not reachable: {exc}")
    with eng.begin() as conn:
        create_schema(conn)
    return eng


@pytest.fixture
def conn(engine):
    """A rolled-back transaction, so the shared database is not mutated."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def write_river_sources(tmp_path, crs=METRIC_CRS, rivers=("Alzette", "Sûre")):
    """A synthetic marker/mapped-river source pair, shaped like the real GPKG.

    Each river is a straight 15 km line from km 35 to km 50, offset from the
    others so selections cannot overlap.
    """
    marker_rows = []
    for index, river in enumerate(rivers):
        offset = index * 50000.0
        for km in range(35, 51):
            marker_rows.append(
                {
                    "TOPONYME": river,
                    "KILO_KM": float(km),
                    "geometry": Point(offset + (km - 35) * 1000.0, 2.0),
                }
            )
    markers_path = tmp_path / "kilometrage.gpkg"
    gpd.GeoDataFrame(marker_rows, crs=crs).to_file(
        markers_path, layer=MARKER_LAYER, driver="GPKG"
    )

    line_rows = [
        {
            "FIRST_TOPO": river,
            "geometry": LineString(
                [(index * 50000.0, 0.0), (index * 50000.0 + 15000.0, 0.0)]
            ),
        }
        for index, river in enumerate(rivers)
    ]
    lines_path = tmp_path / "rivers.gpkg"
    gpd.GeoDataFrame(line_rows, crs=crs).to_file(lines_path, driver="GPKG")

    return RiverSources(markers_path=markers_path, river_lines_path=lines_path)


def markers_at(xs, offset=2.0):
    """Marker points beside a river running along y=0, offset metres away.

    Markers in the real source sit near the watercourse rather than on it,
    which is why the routing projects them onto the line.
    """
    return [Point(x, offset) for x in xs]


@pytest.fixture
def straight_river():
    """A 15 km river along y=0, as one mapped segment."""
    return [LineString([(0.0, 0.0), (15000.0, 0.0)])]


@pytest.fixture
def straight_markers():
    """Markers every 1 km from km 0 to km 15, all 2 m off the line."""
    return markers_at([float(x) for x in range(0, 15001, 1000)])


@pytest.fixture
def split_river_float_gap():
    """Two segments meeting with a sub-micrometre floating-point mismatch."""
    return [
        LineString([(0.0, 0.0), (7000.0, 0.0)]),
        LineString([(7000.0 + 1e-9, 0.0), (15000.0, 0.0)]),
    ]


@pytest.fixture
def split_river_real_gap():
    """Two segments with a real 1 km gap, which must not be bridged."""
    return [
        LineString([(0.0, 0.0), (7000.0, 0.0)]),
        LineString([(8000.0, 0.0), (15000.0, 0.0)]),
    ]
