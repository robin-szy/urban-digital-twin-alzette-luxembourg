import geopandas as gpd
import pandas as pd
import sqlalchemy as sa
from geoalchemy2 import Geometry
from geoalchemy2.shape import from_shape
from sqlalchemy.engine import Connection

from eddie_precip_interpolation.transform import PRECIPITATION_VARIABLE

# ============================================================
# HISTORICAL IDW PRECIPITATION
# ============================================================

HISTORY_UNIQUE_KEY = ["grid_id", "datetime"]


def _interpolated_history_table(table_name: str) -> sa.Table:
    """Historical precipitation surfaces for timeline navigation."""

    return sa.Table(
        table_name,
        sa.MetaData(),

        sa.Column("grid_id", sa.Text, nullable=False),

        # IMPORTANT:
        # real PostgreSQL timestamp for GeoServer TIME dimension
        sa.Column(
            "datetime",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column("precipitation_mm", sa.Float),

        sa.Column("method", sa.Text),
        sa.Column("power", sa.Float),

        sa.Column("stations_used", sa.Integer),
        sa.Column("nearest_station_km", sa.Float),
        sa.Column("mean_station_distance_km", sa.Float),
        sa.Column("search_radius_km", sa.Float),

        sa.Column("inside_convex_hull", sa.Boolean),
        sa.Column("spatial_status", sa.Text),
        sa.Column("fallback_used", sa.Boolean),
        sa.Column("quality_class", sa.Text),

        sa.Column("valid_station_count_at_t", sa.Integer),
        sa.Column("missing_station_count_at_t", sa.Integer),

        sa.Column(
            "geometry",
            Geometry("GEOMETRY", srid=2169),
        ),

        sa.UniqueConstraint(
            *HISTORY_UNIQUE_KEY,
            name=f"{table_name}_unique_key",
        ),
    )


def ensure_interpolated_history_table(
    conn: Connection,
    table_name: str,
) -> None:
    """Create the historical precipitation table if needed."""

    _interpolated_history_table(table_name).create(
        conn,
        checkfirst=True,
    )


def write_interpolated_history(
    conn: Connection,
    result_gdf: gpd.GeoDataFrame,
    table_name: str,
) -> int:
    """
    Store one IDW surface in history.

    If that timestamp already exists, replace only that timestamp.
    """

    table = _interpolated_history_table(table_name)

    if result_gdf.empty:
        return 0

    timestamp = pd.Timestamp(
        result_gdf.iloc[0]["datetime"]
    ).to_pydatetime()

    # Replace this timestamp only.
    conn.execute(
        sa.delete(table).where(
            table.c.datetime == timestamp
        )
    )

    rows = []

    for row in result_gdf.itertuples():

        rows.append(
            {
                "grid_id": str(row.grid_id),
                "datetime": timestamp,

                "precipitation_mm":
                    _none_if_nan(row.precipitation_mm),

                "method": str(row.method),
                "power": float(row.power),

                "stations_used": int(row.stations_used),

                "nearest_station_km":
                    _none_if_nan(row.nearest_station_km),

                "mean_station_distance_km":
                    _none_if_nan(row.mean_station_distance_km),

                "search_radius_km":
                    _none_if_nan(row.search_radius_km),

                "inside_convex_hull":
                    bool(row.inside_convex_hull),

                "spatial_status":
                    str(row.spatial_status),

                "fallback_used":
                    bool(row.fallback_used),

                "quality_class":
                    str(row.quality_class),

                "valid_station_count_at_t":
                    int(row.valid_station_count_at_t),

                "missing_station_count_at_t":
                    int(row.missing_station_count_at_t),

                "geometry":
                    from_shape(row.geometry, srid=2169),
            }
        )

    conn.execute(
        table.insert(),
        rows,
    )

    conn.commit()

    return len(rows)

DERIVED_TABLE_UNIQUE_KEY = ["station_id", "datetime"]


def _dialect_insert(conn: Connection):
    # Mirrors eddie_alzette_ingest/db.py's own helper - duplicated rather than
    # imported, since no plugin here depends on another plugin's package
    # (see design.md "No cross-plugin Python import").
    if conn.engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    return dialect_insert


def _derived_table(table_name: str) -> sa.Table:
    return sa.Table(
        table_name,
        sa.MetaData(),
        sa.Column("station_id", sa.Text, nullable=False),
        sa.Column("datetime", sa.Text, nullable=False),
        sa.Column("value_doubled", sa.Float),
        sa.Column("geometry", Geometry("POINT", srid=4326)),
        sa.UniqueConstraint(*DERIVED_TABLE_UNIQUE_KEY, name=f"{table_name}_unique_key"),
    )


def ensure_derived_table(conn: Connection, table_name: str) -> None:
    _derived_table(table_name).create(conn, checkfirst=True)


def read_precipitation_observations(conn: Connection, observations_table: str) -> pd.DataFrame:
    """Plain SQL read of sum_nn050 rows, no geometry involved - testable on
    SQLite (unlike the geometry-bearing functions below, which need real
    PostGIS to decode a Geometry column; see eddie_alzette_ingest/db.py's own
    sqlite-vs-integration split for the identical reason).
    """
    table = sa.Table(observations_table, sa.MetaData(), autoload_with=conn)
    stmt = sa.select(table.c.station_id, table.c.datetime, table.c.value).where(
        table.c.variable == PRECIPITATION_VARIABLE
    )
    rows = conn.execute(stmt).fetchall()
    return pd.DataFrame(rows, columns=["station_id", "datetime", "value"])


def read_station_geometries(conn: Connection, stations_table: str) -> gpd.GeoDataFrame:
    """Needs a real PostGIS connection to decode the geometry column -
    integration-tested only, same boundary as eddie_alzette_ingest's own
    stations table.
    """
    return gpd.read_postgis(f'SELECT station_id, geometry FROM "{stations_table}"', conn, geom_col="geometry")


def attach_station_geometry(observations_df: pd.DataFrame, stations_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Pure merge, no DB involved - by this point geometries are already real
    shapely objects, so this half of "read + join to geometry" is fully
    unit-testable in memory, independent of the SQL/PostGIS split above.
    """
    merged = observations_df.merge(stations_gdf[["station_id", "geometry"]], on="station_id", how="inner")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=stations_gdf.crs)


def write_derived_table(conn: Connection, derived_gdf: gpd.GeoDataFrame, table_name: str) -> int:
    """Upsert keyed by (station_id, datetime), overwriting value_doubled and
    geometry on conflict - unlike the raw observations' do-nothing
    append-only contract, a derived value is recomputed from the same
    transform each run, so "re-run overwrites" is the correct semantics here.
    Needs real PostGIS (see read_station_geometries); integration-tested only.
    """
    if derived_gdf.empty:
        return 0
    table = _derived_table(table_name)
    rows = [
        {
            "station_id": row.station_id,
            "datetime": row.datetime,
            "value_doubled": None if pd.isna(row.value_doubled) else row.value_doubled,
            "geometry": from_shape(row.geometry, srid=4326),
        }
        for row in derived_gdf.itertuples()
    ]
    insert = _dialect_insert(conn)
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=DERIVED_TABLE_UNIQUE_KEY,
        set_={"value_doubled": stmt.excluded.value_doubled, "geometry": stmt.excluded.geometry},
    )
    result = conn.execute(stmt)
    conn.commit()
    return result.rowcount

# ============================================================
# REAL IDW INTERPOLATION OUTPUT
# ============================================================

INTERPOLATED_LATEST_UNIQUE_KEY = ["grid_id"]


def _interpolated_latest_table(table_name: str) -> sa.Table:
    """Table containing the latest IDW precipitation surface.

    One row = one 100 m grid cell.
    Geometry is stored in Luxembourg's metric CRS EPSG:2169.
    """
    return sa.Table(
        table_name,
        sa.MetaData(),

        sa.Column("grid_id", sa.Text, nullable=False),
        sa.Column("datetime", sa.Text, nullable=False),

        sa.Column("precipitation_mm", sa.Float),

        sa.Column("method", sa.Text),
        sa.Column("power", sa.Float),

        sa.Column("stations_used", sa.Integer),
        sa.Column("nearest_station_km", sa.Float),
        sa.Column("mean_station_distance_km", sa.Float),
        sa.Column("search_radius_km", sa.Float),

        sa.Column("inside_convex_hull", sa.Boolean),
        sa.Column("spatial_status", sa.Text),
        sa.Column("fallback_used", sa.Boolean),
        sa.Column("quality_class", sa.Text),

        sa.Column("valid_station_count_at_t", sa.Integer),
        sa.Column("missing_station_count_at_t", sa.Integer),

        sa.Column(
            "geometry",
            Geometry("GEOMETRY", srid=2169),
        ),

        sa.UniqueConstraint(
            *INTERPOLATED_LATEST_UNIQUE_KEY,
            name=f"{table_name}_unique_key",
        ),
    )


def ensure_interpolated_latest_table(
    conn: Connection,
    table_name: str,
) -> None:
    """Create the interpolation output table if it does not exist."""
    _interpolated_latest_table(table_name).create(
        conn,
        checkfirst=True,
    )


def read_aoi(
    conn: Connection,
    table_name: str,
) -> gpd.GeoDataFrame:
    """Read the AOI polygon from PostGIS."""
    return gpd.read_postgis(
        f'SELECT geometry FROM "{table_name}"',
        conn,
        geom_col="geometry",
    )


def _none_if_nan(value):
    """Convert pandas/numpy missing numbers to SQL NULL."""
    return None if pd.isna(value) else float(value)


def replace_interpolated_latest(
    conn: Connection,
    result_gdf: gpd.GeoDataFrame,
    table_name: str,
) -> int:
    """Replace the currently published latest precipitation surface.

    The table represents only one timestamp, so each successful run removes
    the previous grid values and inserts the new latest surface.
    """

    table = _interpolated_latest_table(table_name)

    # Remove the previous latest surface.
    conn.execute(sa.delete(table))

    if result_gdf.empty:
        conn.commit()
        return 0

    rows = []

    for row in result_gdf.itertuples():

        rows.append(
            {
                "grid_id": str(row.grid_id),
                "datetime": str(row.datetime),

                "precipitation_mm":
                    _none_if_nan(row.precipitation_mm),

                "method": str(row.method),
                "power": float(row.power),

                "stations_used": int(row.stations_used),

                "nearest_station_km":
                    _none_if_nan(row.nearest_station_km),

                "mean_station_distance_km":
                    _none_if_nan(row.mean_station_distance_km),

                "search_radius_km":
                    _none_if_nan(row.search_radius_km),

                "inside_convex_hull":
                    bool(row.inside_convex_hull),

                "spatial_status":
                    str(row.spatial_status),

                "fallback_used":
                    bool(row.fallback_used),

                "quality_class":
                    str(row.quality_class),

                "valid_station_count_at_t":
                    int(row.valid_station_count_at_t),

                "missing_station_count_at_t":
                    int(row.missing_station_count_at_t),

                "geometry":
                    from_shape(row.geometry, srid=2169),
            }
        )

    conn.execute(
        table.insert(),
        rows,
    )

    conn.commit()

    return len(rows)