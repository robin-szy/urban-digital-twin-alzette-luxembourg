"""Precipitation follows the area of interest (Robin, 2026-11-26; openspec aoi-reach-variants 9-10).

Lagloire: this file is new and holds everything AOI-related, so your files change as little as
possible (each edit there is marked "AOI change" and points here). What it does:

* AREA. Until now every grid was built over the fixed table aoi_alzette_35_50_300m (the 300 m
  band along Alzette km 35-50). The area now comes from the AOI module: the CURRENT reach's
  "precipitation" binding, i.e. its watersheds - today a 5 km buffer around the reach, flagged as
  degraded until a real watershed layer exists (eddie_aoi.resolver.BINDINGS). current_aoi().

* RESOLUTION. That area is about 20x larger than the old band (174 km2 against 8.4 km2 for
  km 35-50), so 100 m cells would make ~17,000 cells per timestamp and ~7 GB of history per
  reach. The AOI grid therefore uses 500 m cells (Robin's decision, 2026-11-26): ~700 cells, fewer
  than before. IDW from stations ~15 km apart has no real detail at 100 m. AOI_IDW_CONFIG is your
  IDW_CONFIG with only resolution_m changed.

* ATTRIBUTION. Every stored row carries reach_id, variant_key, degraded and degraded_reason, so
  a surface is never reused for another area (task 9.3). The history is stored in
  HISTORY_STORE; the name you and the map used, precipitation_interpolated_history, is now a VIEW
  of the current reach only (task 9.5), so GeoServer, its TIME dimension and every reader keep
  working unchanged. prepare_history() migrates the old table once: renames it, adds the
  columns, attributes the old rows to the area they were computed for (Alzette km 35-50, variant
  "aoi_alzette_35_50_300m"; task 9.2) and widens the unique key to the scope.
"""
from __future__ import annotations

import dataclasses

import geopandas as gpd
import sqlalchemy as sa

#: The AOI dataset name precipitation is bound under (eddie_aoi.resolver.BINDINGS).
DATASET = "precipitation"

#: 500 m cells for the AOI grid (see the module docstring).
AOI_RESOLUTION_M = 500.0

#: Where the history rows of every reach are stored.
HISTORY_STORE = "precipitation_interpolated_history_by_reach"
#: The published name (GeoServer layer, map, EDDIE's period check): the current reach only.
HISTORY_VIEW = "precipitation_interpolated_history"
#: An older view over the history found in the database (not created by any code here).
LEGACY_WMS_VIEW = "precipitation_interpolated_history_wms"

#: What the rows from before the AOI change were computed for (task 9.2).
LEGACY_REACH_ID = "alzette_035000_050000"
LEGACY_VARIANT_KEY = "aoi_alzette_35_50_300m"

SCOPE_COLUMNS = {"reach_id": "text", "variant_key": "text", "degraded": "boolean", "degraded_reason": "text"}


def aoi_config(config):
    """Lagloire's IDW configuration with the AOI grid's 500 m cells; nothing else changes."""
    return dataclasses.replace(config, resolution_m=AOI_RESOLUTION_M)


def current_aoi(conn) -> tuple[gpd.GeoDataFrame, object]:
    """(AOI polygon as a one-row GeoDataFrame in EPSG:2169, its AoiScope) for the current reach.

    Replaces db.read_aoi(conn, AOI_TABLE): same shape (a "geometry" column), new source.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_base_data.query import resolve_aoi

    scope = resolve_aoi(conn, DATASET)
    if scope.geometry is None or scope.geometry.is_empty:
        raise RuntimeError(f"The AOI module returned no area for {DATASET!r} on reach {scope.reach_id}.")
    frame = gpd.GeoDataFrame({"geometry": [scope.geometry]}, geometry="geometry", crs="EPSG:2169")
    return frame, scope


def scope_values(scope) -> dict:
    """The four attribution columns of one AoiScope."""
    return {"reach_id": scope.reach_id, "variant_key": scope.variant_key,
            "degraded": bool(scope.degraded), "degraded_reason": scope.degraded_reason}


def scope_where(table, scope):
    """SQLAlchemy condition: rows of exactly this scope (reach, variant and degraded state).

    Matching on degraded too means surfaces computed over the 5 km stand-in are not reused once
    the real watershed layer exists (task 9.4).
    """
    return sa.and_(table.c.reach_id == scope.reach_id, table.c.variant_key == scope.variant_key,
                   table.c.degraded.is_(bool(scope.degraded)))


def _relkind(conn, name):
    return conn.execute(sa.text("SELECT relkind FROM pg_class WHERE relname = :n AND relnamespace = "
                                "'public'::regnamespace"), {"n": name}).scalar()


def prepare_history(conn, ensure_table) -> None:
    """Make HISTORY_STORE ready and HISTORY_VIEW show the current reach. Idempotent; commits.

    ensure_table is db.ensure_interpolated_history_table (Lagloire's), used for a fresh database.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_base_data.publish import current_scope_filter

    if _relkind(conn, HISTORY_VIEW) == "r" and _relkind(conn, HISTORY_STORE) is None:
        # The old table: keep its rows, under the storage name.
        conn.execute(sa.text(f'ALTER TABLE "{HISTORY_VIEW}" RENAME TO "{HISTORY_STORE}"'))
    if _relkind(conn, HISTORY_STORE) is None:
        ensure_table(conn, HISTORY_STORE)
    for column, kind in SCOPE_COLUMNS.items():
        conn.execute(sa.text(f'ALTER TABLE "{HISTORY_STORE}" ADD COLUMN IF NOT EXISTS {column} {kind}'))
    conn.execute(sa.text(
        f'UPDATE "{HISTORY_STORE}" SET reach_id = :r, variant_key = :v, degraded = false '
        "WHERE reach_id IS NULL"), {"r": LEGACY_REACH_ID, "v": LEGACY_VARIANT_KEY})
    # Unattributed rows can no longer exist (task 9.5).
    for column in ("reach_id", "variant_key", "degraded"):
        conn.execute(sa.text(f'ALTER TABLE "{HISTORY_STORE}" ALTER COLUMN {column} SET NOT NULL'))
    # The unique key now includes the scope: the same cell and time may exist once per area.
    conn.execute(sa.text(f'ALTER TABLE "{HISTORY_STORE}" DROP CONSTRAINT IF EXISTS "{HISTORY_VIEW}_unique_key"'))
    if not conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :c"),
                        {"c": f"{HISTORY_STORE}_unique_key"}).scalar():
        conn.execute(sa.text(f'ALTER TABLE "{HISTORY_STORE}" ADD CONSTRAINT "{HISTORY_STORE}_unique_key" '
                             "UNIQUE (reach_id, variant_key, grid_id, datetime)"))
    # The published view: the current reach's surfaces of precipitation's bound variant, looked
    # up live, so a reach switch needs no rebuild (as for building_population_4326).
    from eddie_aoi.resolver import BINDINGS

    variant = BINDINGS[DATASET].key
    # precipitation_interpolated_history_wms: an older view over the history (made outside this
    # code) giving GeoServer the time as timestamp without time zone (datetime_utc), as
    # eddie_water_level's water_level_history_wms does. PostgreSQL moved it along with the rename,
    # so it showed every area at once; it is rebuilt here on the current-area view, with its
    # original columns. Dropped first: once it reads HISTORY_VIEW, HISTORY_VIEW can't be dropped.
    conn.execute(sa.text(f'DROP VIEW IF EXISTS "{LEGACY_WMS_VIEW}"'))
    conn.execute(sa.text(f'DROP VIEW IF EXISTS "{HISTORY_VIEW}"'))
    conn.execute(sa.text(f'CREATE VIEW "{HISTORY_VIEW}" AS SELECT * FROM "{HISTORY_STORE}" '
                         f"WHERE {current_scope_filter(conn, variant)}"))
    conn.execute(sa.text(f'''
        CREATE VIEW "{LEGACY_WMS_VIEW}" AS
        SELECT grid_id, datetime AT TIME ZONE 'UTC' AS datetime_utc, precipitation_mm, method, power,
               stations_used, nearest_station_km, mean_station_distance_km, search_radius_km,
               inside_convex_hull, spatial_status, fallback_used, quality_class,
               valid_station_count_at_t, missing_station_count_at_t, geometry
        FROM "{HISTORY_VIEW}"'''))
    conn.commit()


def stamp_latest(conn, table: str, scope) -> None:
    """Attribute a "latest" table (one surface, replaced whole each run) to the scope it shows."""
    for column, kind in SCOPE_COLUMNS.items():
        conn.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {kind}'))
    conn.execute(sa.text(f'UPDATE "{table}" SET reach_id = :reach_id, variant_key = :variant_key, '
                         "degraded = :degraded, degraded_reason = :degraded_reason"), scope_values(scope))
    conn.commit()


def latest_reach(conn, table: str) -> str | None:
    """The reach a "latest" table currently shows (None before attribution or when empty)."""
    if _relkind(conn, table) is None:
        return None
    columns = {c["name"] for c in sa.inspect(conn).get_columns(table)}
    if "reach_id" not in columns:
        return None
    return conn.execute(sa.text(f'SELECT reach_id FROM "{table}" LIMIT 1')).scalar()
