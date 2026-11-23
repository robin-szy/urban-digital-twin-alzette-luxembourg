"""EDDIE Celery task: Pegah's 15-minute gauge tariff and seasonal-rain scenario 2.

building_population (PostGIS) -> the two models -> extra columns on the same rows
-> <table>_4326 view -> GeoServer -> the existing "Water fees (model result)" layer.

The values are written in place, so nothing new has to be added to the PostGIS schema
of the map: the fee of a building is a column of the row the map already draws.
A run recomputes the columns for one scope only (the newest in the table, or the
``reach_id`` / ``variant_key`` given), leaving the other scopes' rows untouched.

Re-running the population model replaces its scope's rows, which resets these
columns for that scope to NULL; run this task again afterwards.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd
import sqlalchemy as sa
from eddie.digitaltwin import setup_environment
from eddie.tasks import app
from eddie_base_data.ingest import transactional
from eddie_base_data.publish import publish_view

from .gauge_timeseries import (
    GAUGES,
    fee_table,
    load_readings,
    load_water_areas,
    markers_from_postgis,
    period,
    section_areas,
    volume_series,
)
from .rain_fee import DEFAULT_RAINWATER_UNIT_PRICE, rain_table
from .seasonal_rain import SEASONS, normal_rain_mm, seasonal_rain_mm

TABLE = "building_population"
PUBLISH_SRID = 4326
VIEW = f"{TABLE}_{PUBLISH_SRID}"

# Column added to building_population -> its SQL type. Kept in one place: the ALTER
# statements, the UPDATE and the summary route all read it.
COLUMNS: dict[str, str] = {
    # Scenario 1, 15-minute gauge readings.
    "water_area_share_15min": "double precision",
    "allocated_water_volume_15min_m3": "double precision",
    "water_fee_15min_mean_eur": "double precision",
    "water_fee_15min_min_eur": "double precision",
    "water_fee_15min_max_eur": "double precision",
    "tariff_mean_eur_15min": "double precision",
    "tariff_intervals_15min": "integer",
    "gauge_quality_15min": "text",
    "gauge_window_start_utc": "timestamp",
    "gauge_window_end_utc": "timestamp",
    # Scenario 2, seasonal rain on the roofs.
    "rainwater_area_share": "double precision",
    "allocated_rainwater_volume_m3": "double precision",
    "rainwater_cost_eur": "double precision",
    "rain_mm_season": "double precision",
    "rain_volume_m3": "double precision",
    "rainwater_unit_price_eur_per_m3": "double precision",
    "rain_season": "text",
    "rain_source": "text",
}
# Stamped by the UPDATE itself, not carried by the staging table.
COMPUTED_COLUMN = "water_fees_computed_utc"

# Row identity for the UPDATE. ctid is used first: a scope can hold the same gml_id twice
# (the 14,684-row km 35-50 scope has one duplicate), and a fee must land on one row.
BUILDING_ID_COLUMNS = ("row_id", "gml_id", "buildingid", "localid")


def resolve_scope(conn: sa.Connection, reach_id: str | None, variant_key: str | None) -> tuple[str, str]:
    """The scope the columns are written for.

    The newest computed scope by default: that is what the map panel summarises.
    """
    if reach_id and variant_key:
        return reach_id, variant_key
    row = conn.execute(
        sa.text(
            f'SELECT reach_id, variant_key FROM "{TABLE}" '
            "WHERE reach_id IS NOT NULL ORDER BY computed_utc DESC NULLS LAST LIMIT 1"
        )
    ).one_or_none()
    if row is None:
        raise RuntimeError(f'"{TABLE}" is empty: run the population model first (Water-fee model panel).')
    return str(row[0]), str(row[1])


def read_buildings(conn: sa.Connection, reach_id: str, variant_key: str) -> gpd.GeoDataFrame:
    """The rows of one scope, with the columns both models need."""
    return gpd.read_postgis(
        sa.text(f'SELECT ctid::text AS row_id, * FROM "{TABLE}" WHERE reach_id = :reach AND variant_key = :variant'),
        conn,
        geom_col="geometry",
        params={"reach": reach_id, "variant": variant_key},
    )


def _id_column(buildings) -> str:
    """The row key to match the staging table on: the first complete, unique id."""
    for name in BUILDING_ID_COLUMNS:
        if name not in buildings.columns:
            continue
        values = buildings[name]
        if values.isna().any() or values.duplicated().any():
            continue
        return name
    raise KeyError(f"No unique, complete id column among {BUILDING_ID_COLUMNS} in the result rows.")


def recreate_view(conn: sa.Connection) -> None:
    """Rebuild <table>_4326 with the table's current columns.

    ``eddie_base_data.publish.write_derived`` creates this view when it writes results;
    ``publish_view`` only registers it in GeoServer. Adding columns therefore needs the
    view rebuilt here, or the new columns never reach the map.
    """
    columns = [column["name"] for column in sa.inspect(conn).get_columns(TABLE) if column["name"] != "geometry"]
    selected = ", ".join(f'"{name}"' for name in columns)
    conn.execute(sa.text(f'DROP VIEW IF EXISTS "{VIEW}"'))
    conn.execute(sa.text(
        f'CREATE VIEW "{VIEW}" AS SELECT {selected}, '
        f'ST_Transform(geometry, {PUBLISH_SRID})::geometry(Geometry, {PUBLISH_SRID}) AS geometry '
        f'FROM "{TABLE}" '
    ))


def _match(on: str) -> str:
    """The UPDATE join condition for the chosen row key; ctid needs a cast from text."""
    if on == "row_id":
        return 'b.ctid = s."row_id"::tid'
    return f'b."{on}" = s."{on}"'


def write_columns(engine: sa.Engine, values: pd.DataFrame, id_column: str, reach_id: str, variant_key: str) -> None:
    """Add the columns if needed, then set them on the scope's rows, atomically."""
    columns = [name for name in COLUMNS if name in values.columns]
    staged = values[["building_id", *columns]].rename(columns={"building_id": id_column})
    assignments = ", ".join(f'"{name}" = s."{name}"' for name in columns)
    with transactional(engine).begin() as conn:
        existing = {column["name"] for column in sa.inspect(conn).get_columns(TABLE)}
        for name in columns:
            if name not in existing:
                conn.execute(sa.text(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" {COLUMNS[name]}'))
        if COMPUTED_COLUMN not in existing:
            conn.execute(sa.text(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COMPUTED_COLUMN}" timestamp with time zone'))
        conn.execute(sa.text('DROP TABLE IF EXISTS "_water_fees_stage"'))
        staged.to_sql("_water_fees_stage", conn, if_exists="replace", index=False)
        result = conn.execute(
            sa.text(
                f'UPDATE "{TABLE}" AS b SET {assignments}, "{COMPUTED_COLUMN}" = now() '
                'FROM "_water_fees_stage" AS s '
                f'WHERE {_match(on=id_column)} AND b.reach_id = :reach AND b.variant_key = :variant'
            ),
            {"reach": reach_id, "variant": variant_key},
        )
        conn.execute(sa.text('DROP TABLE "_water_fees_stage"'))
        if result.rowcount != len(staged):
            raise RuntimeError(
                f"Updated {result.rowcount} rows for {reach_id}/{variant_key}, expected {len(staged)}."
            )
        recreate_view(conn)


def season_rain(season: str, season_year: int, use_normals: bool) -> tuple[float, str]:
    """The season's rain in mm and where it came from, falling back to the normals."""
    key = season.strip().lower()
    if key not in SEASONS:
        raise ValueError(f"unknown season {season!r}: use one of {', '.join(SEASONS)}")
    if use_normals:
        return normal_rain_mm(key), "normals 1991-2020, Findel"
    try:
        answer = seasonal_rain_mm(season_year, key)
    except (OSError, ValueError) as error:
        return normal_rain_mm(key), f"normals 1991-2020 (archive unavailable: {error})"
    return answer["rain_mm"], (
        f"reanalysis {answer['start']} to {answer['end']} "
        f"({answer['missing_days']} day(s) without data)"
    )


@app.task(name="eddie_water_fees.tasks.compute_water_fees")
def compute_water_fees(
    percentage: float = 0.10,
    unit_price: float = 7.0,
    gauge_start: str | None = None,
    gauge_end: str | None = None,
    readings_json: str | None = None,
    season: str = "summer",
    season_year: int = 2024,
    use_normals: bool = False,
    rainwater_unit_price: float = DEFAULT_RAINWATER_UNIT_PRICE,
    reach_id: str | None = None,
    variant_key: str | None = None,
) -> dict:
    """Add the 15-minute tariff and the seasonal-rain fee to one scope's buildings."""
    engine = setup_environment.get_database()
    with engine.connect() as conn:
        reach, variant = resolve_scope(conn, reach_id, variant_key)
        markers = markers_from_postgis(conn)
        buildings = read_buildings(conn, reach, variant)
    if buildings.empty:
        raise RuntimeError(f'No rows in "{TABLE}" for {reach}/{variant}.')
    id_column = _id_column(buildings)

    print(f" -> Scope {reach}/{variant}: {len(buildings)} buildings")
    water = load_water_areas(markers)
    areas = section_areas(markers, water)
    print(" -> Section areas (m²): " + ", ".join(f"{name} {area:,.0f}" for name, area in areas.items()))
    if not any(areas.values()):
        raise ValueError("No mapped river surface in any section; check OSM_GPKG.")

    start_ts, end_ts = period(gauge_start, gauge_end)
    readings = load_readings(readings_json, start_ts, end_ts)
    for gauge in GAUGES:
        print(f" -> {gauge.name} (station {gauge.station_id}): {len(readings.get(gauge.station_id, []))} readings")
    series = volume_series(readings, areas=areas, percentage=percentage, unit_price=unit_price)
    print(f" -> {len(series)} intervals, quality {series['quality'].value_counts().to_dict()}")

    fees = fee_table(buildings, series)
    rain_mm, rain_source = season_rain(season, season_year, use_normals)
    rain = rain_table(buildings, rain_mm, unit_price=rainwater_unit_price, season=season.strip().lower(),
                      source=rain_source)
    values = fees.merge(rain, on="building_id", how="inner", validate="one_to_one")
    if len(values) != len(buildings):
        raise RuntimeError(f"Matched {len(values)} of {len(buildings)} buildings; refusing to write a partial result.")

    write_columns(engine, values, id_column, reach, variant)
    layer = publish_view(engine, TABLE)

    tariffs = series["tariff_eur"].dropna()
    mean_tariff = float(tariffs.mean())
    fee_buildings = int((values["water_area_share_15min"] > 0).sum())
    print(f" -> Mean tariff {mean_tariff:,.2f} EUR shared by {fee_buildings} non-residential buildings")
    print(f" -> {rain_mm} mm of rain -> {values['rain_volume_m3'].iloc[0]:,.0f} m³, "
          f"{values['rainwater_cost_eur'].sum():,.2f} EUR at {rainwater_unit_price}/m³")
    return {
        "table": TABLE,
        "layer": layer,
        "reach_id": reach,
        "variant_key": variant,
        "buildings": int(len(values)),
        "intervals": int(len(series)),
        "priced_intervals": int(tariffs.size),
        "gauge_quality": str(fees["gauge_quality_15min"].iloc[0]),
        "gauge_window_start_utc": str(fees["gauge_window_start_utc"].iloc[0]),
        "gauge_window_end_utc": str(fees["gauge_window_end_utc"].iloc[0]),
        "section_areas_m2": {name: round(float(area), 1) for name, area in areas.items()},
        "mean_tariff_eur": round(mean_tariff, 2),
        "min_tariff_eur": round(float(tariffs.min()), 2),
        "max_tariff_eur": round(float(tariffs.max()), 2),
        "fee_buildings": fee_buildings,
        "rain_mm": float(rain_mm),
        "rain_source": rain_source,
        "rain_volume_m3": round(float(values["rain_volume_m3"].iloc[0]), 1),
        "rainwater_cost_eur": round(float(values["rainwater_cost_eur"].sum()), 2),
    }
