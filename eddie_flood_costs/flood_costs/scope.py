"""The area (AOI) the flood-cost module works on.

The team's AOI contract is eddie_base_data.query.AoiScope, obtained through
resolve_aoi(conn, dataset): "the seam for the reach/variant resolver". Once the AOI
module binds the dataset "flood_costs" there, get_scope() returns that AOI and nothing in
this package changes. Until then the AOI is built here from the official river-kilometre
markers: a km range on the Alzette (default km 35-50, the study area of the reviewed run).
"""
import shapely
import sqlalchemy as sa
from eddie_base_data.query import AoiScope, resolve_aoi

DATASET = "flood_costs"
DEFAULT_KM = (35.0, 50.0)
DEFAULT_MARGIN_M = 500.0  # same margin as the team's provisional km 35-50 box (eddie_base_data.query)


def reach_id(river: str, km_start: float, km_end: float) -> str:
    """'alzette_035000_050000' for Alzette km 35-50: the format of eddie_base_data.query.ALZETTE_35_50."""
    return f"{river.lower()}_{round(km_start * 1000):06d}_{round(km_end * 1000):06d}"


def km_range_scope(conn: sa.Connection, km_start: float, km_end: float,
                   margin_m: float = DEFAULT_MARGIN_M, river: str = "Alzette") -> AoiScope:
    """The bounding box of the river's km markers between km_start and km_end, widened by margin_m."""
    if km_end <= km_start:
        raise ValueError(f"km_end ({km_end}) must be greater than km_start ({km_start})")
    wkb = conn.execute(sa.text("""
        SELECT ST_AsHEXEWKB(ST_Expand(ST_Extent(geometry)::box2d::geometry, :margin))
        FROM base_river_km_primary
        WHERE toponyme = :river AND kilo_km >= :km_start - 0.001 AND kilo_km <= :km_end + 0.001"""),
        {"margin": margin_m, "river": river, "km_start": km_start, "km_end": km_end}).scalar_one()
    if wkb is None:
        raise ValueError(f"No river-km markers for {river} km {km_start}-{km_end}")
    return AoiScope(reach_id(river, km_start, km_end), f"km_bbox_{margin_m:.0f}", shapely.from_wkb(wkb))


def get_scope(conn: sa.Connection, km_start: float | None = None, km_end: float | None = None) -> AoiScope:
    """The AOI to work on.

    An explicit km range wins (testing, a chosen reach). Otherwise the AOI module's
    resolver, once it covers "flood_costs"; until then the default km 35-50 range.
    """
    if km_start is not None or km_end is not None:
        if km_start is None or km_end is None:
            raise ValueError("Give both km_start and km_end, or neither")
        return km_range_scope(conn, km_start, km_end)
    try:
        return resolve_aoi(conn, DATASET)
    except ValueError:
        return km_range_scope(conn, *DEFAULT_KM)
