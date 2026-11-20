"""The read side: which area a dataset is processed for, and AOI-filtered reads of base tables."""
from dataclasses import dataclass

import geopandas as gpd
import pandas as pd
import shapely
import sqlalchemy as sa
from shapely.geometry.base import BaseGeometry

from .ingest import WORKING_SRID

# Bind an AOI exactly: WKB round-trips the coordinates, WKT need not.
AOI_SQL = f"ST_SetSRID(CAST(:aoi AS geometry), {WORKING_SRID})"


@dataclass(frozen=True)
class AoiScope:
    """The area a result was computed for. Stamped on every derived row (publish.write_derived)."""

    reach_id: str
    variant_key: str
    geometry: BaseGeometry | None = None
    degraded: bool = False
    degraded_reason: str | None = None


def resolve_aoi(conn: sa.Connection, dataset: str, reach_id: str | None = None) -> AoiScope:
    """The AOI a dataset is processed for: its bound variant of the CURRENT reach.

    The area comes from the AOI module's binding registry (eddie_aoi.resolver.BINDINGS),
    not from here and not from the module. The current reach is the last selection that
    finished building, or the configured fallback (Alzette km 35-50) before any selection.
    Modules keep calling this unchanged; what they get follows the selection.

    An unregistered dataset raises, listing the registered ones, rather than getting a
    default area.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_aoi.resolver import resolve

    result = resolve(conn, dataset, reach_id)
    return AoiScope(result.reach_id, result.variant_key, result.geometry,
                    result.degraded, result.degraded_reason)


def aoi_params(aoi: BaseGeometry) -> dict:
    return {"aoi": shapely.to_wkb(shapely.force_2d(aoi), hex=True)}


def read_in_aoi(conn: sa.Connection, table: str, aoi: BaseGeometry, margin_m: float = 0.0) -> gpd.GeoDataFrame:
    """The rows of a base table that intersect the AOI, optionally widened by margin_m."""
    where = f"ST_DWithin(geometry, {AOI_SQL}, :margin)" if margin_m else f"ST_Intersects(geometry, {AOI_SQL})"
    return gpd.read_postgis(sa.text(f'SELECT * FROM "{table}" WHERE {where}'), conn, geom_col="geometry",
                            params={**aoi_params(aoi), "margin": margin_m})


def read_all(conn: sa.Connection, table: str, order_by: str | None = None) -> gpd.GeoDataFrame:
    order = f' ORDER BY "{order_by}"' if order_by else ""
    return gpd.read_postgis(sa.text(f'SELECT * FROM "{table}"{order}'), conn, geom_col="geometry")


def read_table(conn: sa.Connection, table: str) -> pd.DataFrame:
    return pd.read_sql(sa.text(f'SELECT * FROM "{table}"'), conn)
