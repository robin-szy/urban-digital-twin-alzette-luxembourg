"""The read side: which area a dataset is processed for, and AOI-filtered reads of base tables."""
from dataclasses import dataclass

import geopandas as gpd
import pandas as pd
import shapely
import sqlalchemy as sa
from shapely.geometry.base import BaseGeometry

from .ingest import WORKING_SRID

# The reach every module runs for until the AOI selection reaches them.
ALZETTE_35_50 = "alzette_035000_050000"
# Pegah's provisional AOI: the bounding box of the km 35-50 markers, widened by this margin.
BBOX_MARGIN_M = 500.0

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


def _alzette_35_50_bbox(conn: sa.Connection) -> AoiScope:
    geometry = conn.execute(sa.text(f"""
        SELECT ST_AsHEXEWKB(ST_Expand(ST_Extent(geometry)::box2d::geometry, :margin))
        FROM base_river_km_primary
        WHERE toponyme = 'Alzette' AND kilo_km >= 34.999 AND kilo_km <= 50.001"""),
                            {"margin": BBOX_MARGIN_M}).scalar_one()
    return AoiScope(ALZETTE_35_50, f"bbox_{BBOX_MARGIN_M:.0f}", shapely.from_wkb(geometry))


# dataset -> how its AOI is obtained.
_BINDINGS = {"building_population": _alzette_35_50_bbox}


def resolve_aoi(conn: sa.Connection, dataset: str) -> AoiScope:
    """The AOI a dataset is processed for.

    This is the seam for the reach/variant resolver (openspec aoi-reach-variants,
    group 8): replace its body with the resolver, and no module changes.
    """
    try:
        return _BINDINGS[dataset](conn)
    except KeyError:
        raise ValueError(f"No AOI binding for dataset {dataset!r}; bound: {sorted(_BINDINGS)}") from None


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
