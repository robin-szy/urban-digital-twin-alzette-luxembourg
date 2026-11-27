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
import numpy as np  # AOI change: typed NULL columns in not_computed_fee_table
import pandas as pd
import sqlalchemy as sa
from eddie.digitaltwin import setup_environment
from eddie.tasks import app
from eddie_base_data.ingest import transactional
from eddie_base_data.publish import publish_view

from .gauge_timeseries import (
    GAUGES,
    KM_START,  # AOI change: these three are only used as the fallback reach (see reach_extent)
    KM_END,
    RIVER_NAME,
    fee_table,
    load_readings,
    load_water_areas,
    markers_from_postgis,
    period,
    section_areas,
    volume_series,
)
from .rain_fee import DEFAULT_RAINWATER_UNIT_PRICE, rain_table
from .scenarios import (
    DEFAULT_DISTANCE_THRESHOLD_M,
    DEFAULT_FLOOD_RISK_BASE_FEE,
    DEFAULT_M3_PER_PERSON,
    DEFAULT_QUALITY_UNIT_PRICE,
    DEFAULT_UNIT_PRICE_PER_M3,
    DEFAULT_USABLE_SPACE_PCT,
    consumption_table,
    flood_risk_table,
    water_quality_table,
)
from .seasonal_rain import SEASONS, normal_rain_mm, season_dates, seasonal_rain_mm

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
    # AOI change (Robin, 2026-11-25): why scenario 1 is missing or only partial for this
    # reach (the three gauges cover Alzette km 35.0-54.34 only). NULL when fully computed.
    "tariff_note_15min": "text",
    # Scenario 1, the same gauge series charged once on the season's mean level: what the
    # building owes for the season, the mean standing volume, and the pool it implies.
    "water_fee_season_eur": "double precision",
    "allocated_water_volume_season_m3": "double precision",
    "tariff_total_eur_season": "double precision",
    "water_volume_season_m3": "double precision",
    # Scenario 2, seasonal rain on the roofs.
    "rainwater_area_share": "double precision",
    "allocated_rainwater_volume_m3": "double precision",
    "rainwater_cost_eur": "double precision",
    "rain_mm_season": "double precision",
    "rain_volume_m3": "double precision",
    "rainwater_unit_price_eur_per_m3": "double precision",
    "rain_season": "text",
    "rain_source": "text",
    # Scenario 3, flood-risk proximity levy.
    "distance_to_river_m": "double precision",
    "flood_risk_fee_eur": "double precision",
    # Scenario 4, water-quality surcharge.
    "pollution_multiplier": "double precision",
    "water_quality_charge_eur": "double precision",
    # Scenario 5, water consumption by population.
    "water_consumption_m3": "double precision",
    "water_consumption_cost_eur": "double precision",
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


# ============================================================
# AOI change (Robin, 2026-11-25)
# ============================================================
# Before: the buildings already followed the selected reach (resolve_scope), but the river
# markers were always Alzette km 35-50. So for another reach, scenario 1 charged the whole
# km 35-50 river volume to that reach's buildings, and scenario 3 measured distances to a
# river that might not be in the reach at all (every building 0 EUR, which reads as
# "no flood risk" when it really means "not computed").
#
# Now the markers are the reach's own:
#   * scenario 3 uses all markers of the reach (it only needs the river line);
#   * scenario 1 uses the part of the reach the gauges cover. Outside it there is no stage to
#     multiply, so the columns are left NULL with the reason in tariff_note_15min - shown
#     as "not computed", never as 0 EUR. Partly covered: computed for the covered km only,
#     and the note says which km are missing.
# Scenarios 2, 4 and 5 depend on the buildings only, so they are unchanged.

# Lower bound: your original KM_START (Pfaffenthal at km 35.04 covers down to it).
# Upper bound: the Livange gauge, the most upstream of the three.
GAUGE_COVERAGE_KM = (KM_START, max(gauge.km for gauge in GAUGES))


def reach_extent(conn: sa.Connection, reach_id: str) -> tuple[str, float, float]:
    """(river, km_start, km_end) of a reach, from the AOI module's reach cache.

    Falls back to your original Alzette km 35-50 when the AOI module is not installed or
    does not know the reach (e.g. an old scope written before the AOI module existed).
    """
    try:
        from eddie_aoi.store import read_cached  # pylint: disable=import-outside-toplevel
    except ImportError:
        return RIVER_NAME, KM_START, KM_END
    cached = read_cached(conn, reach_id)
    if cached is None:
        return RIVER_NAME, KM_START, KM_END
    return cached.river, float(cached.km_start), float(cached.km_end)


def gauge_covered_part(river: str, km_start: float, km_end: float) -> tuple[tuple[float, float] | None, str | None]:
    """The km range of the reach the gauges cover, and a note on what is missing.

    Returns ((start, end), None) when fully covered, ((start, end), note) when partly
    covered, and (None, note) when not covered at all.
    """
    low, high = GAUGE_COVERAGE_KM
    if river != RIVER_NAME:
        return None, f"not computed: the gauges are on the {RIVER_NAME}, this reach is on the {river}"
    start, end = max(km_start, low), min(km_end, high)
    if start >= end:
        return None, (f"not computed: reach km {km_start:g}-{km_end:g} is outside the gauge coverage "
                      f"(km {low:g}-{high:g})")
    missing = [f"km {a:g}-{b:g}" for a, b in ((km_start, start), (end, km_end)) if a < b]
    if missing:
        return (start, end), ("partial: river volume only for km " f"{start:g}-{end:g}; "
                              + ", ".join(missing) + " not covered by the gauges")
    return (start, end), None


def not_computed_fee_table(buildings: gpd.GeoDataFrame, note: str) -> pd.DataFrame:
    """Scenario 1 columns for a reach the gauges do not cover: all NULL, with the reason.

    Same columns and building_id as gauge_timeseries.fee_table. The NULLs are typed
    (float NaN, NaT) so the staging table in write_columns gets the right SQL types.
    """
    id_column = next(name for name in BUILDING_ID_COLUMNS if name in buildings.columns)
    count = len(buildings)
    table = pd.DataFrame({"building_id": buildings[id_column].to_numpy()})
    for name in ("water_area_share_15min", "allocated_water_volume_15min_m3", "water_fee_15min_mean_eur",
                 "water_fee_15min_min_eur", "water_fee_15min_max_eur", "tariff_mean_eur_15min",
                 "tariff_intervals_15min", "water_fee_season_eur", "allocated_water_volume_season_m3",
                 "tariff_total_eur_season", "water_volume_season_m3"):
        table[name] = np.full(count, np.nan)
    table["gauge_quality_15min"] = "not computed"
    table["gauge_window_start_utc"] = pd.NaT
    table["gauge_window_end_utc"] = pd.NaT
    table["tariff_note_15min"] = note
    return table


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


def recreate_view(conn: sa.Connection, variant_key: str) -> None:
    """Rebuild <table>_4326 with the table's current columns.

    ``eddie_base_data.publish.write_derived`` creates this view when it writes results;
    ``publish_view`` only registers it in GeoServer. Adding columns therefore needs the
    view rebuilt here, or the new columns never reach the map.

    AOI change (Robin, 2026-11-25): the view used to be rebuilt WITHOUT a filter, so the
    map drew building_population of every reach ever computed at once (e.g. km 35-50
    buildings while km 43-46 was selected). The view write_derived creates shows only the
    CURRENT reach; this keeps that filter, using the same helper. variant_key is the
    scope just written (corridor_500 for building_population).
    """
    # pylint: disable=import-outside-toplevel
    from eddie_base_data.publish import current_scope_filter
    from eddie_base_data.publish import recreate_view as rebuild

    rebuild(conn, TABLE, current_scope_filter(conn, variant_key))


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
        recreate_view(conn, variant_key)  # AOI change: keeps the current-reach filter


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
    percentage: float = 0.04,
    unit_price: float = 7.0,
    gauge_start: str | None = None,
    gauge_end: str | None = None,
    readings_json: str | None = None,
    season: str = "summer",
    season_year: int = 2024,
    use_normals: bool = False,
    tariff_period: str = "season",
    rainwater_unit_price: float = DEFAULT_RAINWATER_UNIT_PRICE,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD_M,
    flood_risk_base_fee: float = DEFAULT_FLOOD_RISK_BASE_FEE,
    quality_unit_price: float = DEFAULT_QUALITY_UNIT_PRICE,
    m3_per_person: float = DEFAULT_M3_PER_PERSON,
    unit_price_per_m3: float = DEFAULT_UNIT_PRICE_PER_M3,
    usable_space_pct: float = DEFAULT_USABLE_SPACE_PCT,
    reach_id: str | None = None,
    variant_key: str | None = None,
) -> dict:
    """Add the five water-fee scenarios to one scope's buildings."""
    tariff_period = str(tariff_period).strip().lower()
    if tariff_period not in ("season", "window"):
        raise ValueError('tariff_period must be "season" (the season dates) or "window".')

    engine = setup_environment.get_database()
    with engine.connect() as conn:
        reach, variant = resolve_scope(conn, reach_id, variant_key)
        # AOI change: the reach's own river markers instead of the fixed km 35-50 (see the
        # AOI block above reach_extent). `markers` = whole reach, for scenario 3;
        # `gauge_markers` = the part the gauges cover, for scenario 1 (None if no part).
        river, reach_start, reach_end = reach_extent(conn, reach)
        markers = markers_from_postgis(conn, reach_start, reach_end, river)
        covered, tariff_note = gauge_covered_part(river, reach_start, reach_end)
        gauge_markers = None
        if covered is not None:
            try:
                gauge_markers = markers_from_postgis(conn, covered[0], covered[1], river)
            except ValueError:  # fewer than 2 markers in a sliver of coverage: nothing to measure
                tariff_note = f"not computed: too short a stretch (km {covered[0]:g}-{covered[1]:g}) inside the gauge coverage"
        buildings = read_buildings(conn, reach, variant)
    if buildings.empty:
        raise RuntimeError(f'No rows in "{TABLE}" for {reach}/{variant}.')
    id_column = _id_column(buildings)

    print(f" -> Scope {reach}/{variant}: {len(buildings)} buildings, {river} km {reach_start:g}-{reach_end:g}")
    if tariff_note:
        print(f" -> Scenario 1: {tariff_note}")

    # Scenario 1, unchanged except that it runs on gauge_markers (AOI change), and only when
    # the reach has a gauge-covered part; otherwise its columns are NULL with the reason.
    areas, series = {}, None
    if gauge_markers is not None:
        water = load_water_areas(gauge_markers)
        areas = section_areas(gauge_markers, water)
        print(" -> Section areas (m²): " + ", ".join(f"{name} {area:,.0f}" for name, area in areas.items()))
        if not any(areas.values()):
            raise ValueError("No mapped river surface in any section; check OSM_GPKG.")

        if tariff_period == "season":
            first_day, last_day = season_dates(season_year, season.strip().lower())
            start_ts, end_ts = period(str(first_day), f"{last_day} 23:59")
        else:
            start_ts, end_ts = period(gauge_start, gauge_end)
        print(f" -> Scenario 1 window: {start_ts} to {end_ts} UTC ({tariff_period})")
        readings = load_readings(readings_json, start_ts, end_ts)
        for gauge in GAUGES:
            print(f" -> {gauge.name} (station {gauge.station_id}): {len(readings.get(gauge.station_id, []))} readings")
        series = volume_series(readings, areas=areas, percentage=percentage, unit_price=unit_price)
        print(f" -> {len(series)} intervals, quality {series['quality'].value_counts().to_dict()}")
        fees = fee_table(buildings, series)
        fees["tariff_note_15min"] = tariff_note  # None when fully covered, else the "partial: ..." note
    else:
        fees = not_computed_fee_table(buildings, tariff_note)

    rain_mm, rain_source = season_rain(season, season_year, use_normals)
    rain = rain_table(buildings, rain_mm, unit_price=rainwater_unit_price, season=season.strip().lower(),
                      source=rain_source)
    flood = flood_risk_table(buildings, markers=markers, distance_threshold=distance_threshold,
                             base_fee=flood_risk_base_fee)
    quality = water_quality_table(buildings, unit_price=quality_unit_price)
    consumption = consumption_table(buildings, m3_per_person=m3_per_person,
                                    unit_price_per_m3=unit_price_per_m3,
                                    usable_space_pct=usable_space_pct)
    values = fees.merge(rain, on="building_id", how="inner", validate="one_to_one")
    for extra in (flood, quality, consumption):
        values = values.merge(extra, on="building_id", how="inner", validate="one_to_one")
    if len(values) != len(buildings):
        raise RuntimeError(f"Matched {len(values)} of {len(buildings)} buildings; refusing to write a partial result.")

    write_columns(engine, values, id_column, reach, variant)
    layer = publish_view(engine, TABLE)

    # AOI change: series is None when scenario 1 was not computed for this reach; the
    # scenario-1 numbers in the result are then None (not 0) and the note says why.
    tariffs = series["tariff_eur"].dropna() if series is not None else pd.Series(dtype="float64")
    mean_tariff = float(tariffs.mean()) if len(tariffs) else None
    fee_buildings = int((values["water_area_share_15min"] > 0).sum())
    if mean_tariff is not None:
        print(f" -> Mean tariff {mean_tariff:,.2f} EUR shared by {fee_buildings} non-residential buildings")
    if series is not None:
        print(f" -> Season tariff {values['tariff_total_eur_season'].iloc[0]:,.2f} EUR on the "
              f"mean level over {values['tariff_intervals_15min'].iloc[0]:.0f} intervals, "
              f"mean river volume {values['water_volume_season_m3'].iloc[0]:,.0f} m\u00b3")
    print(f" -> {rain_mm} mm of rain -> {values['rain_volume_m3'].iloc[0]:,.0f} m³, "
          f"{values['rainwater_cost_eur'].sum():,.2f} EUR at {rainwater_unit_price}/m³")
    in_zone = int((values["flood_risk_fee_eur"] > 0).sum())
    print(f" -> Flood-risk levy: {in_zone} buildings within {distance_threshold:g} m, "
          f"{values['flood_risk_fee_eur'].sum():,.2f} EUR at {flood_risk_base_fee:g} EUR")
    bands = values["pollution_multiplier"].value_counts().sort_index()
    print(f" -> Water-quality surcharge: {values['water_quality_charge_eur'].sum():,.2f} EUR "
          f"at {quality_unit_price}/m² per multiplier unit "
          f"({', '.join(f'{band:g}: {count} buildings' for band, count in bands.items())})")
    print(f" -> Water consumption: {values['water_consumption_m3'].sum():,.0f} m³, "
          f"{values['water_consumption_cost_eur'].sum():,.2f} EUR at {unit_price_per_m3}/m\u00b3")
    return {
        "table": TABLE,
        "layer": layer,
        "reach_id": reach,
        "variant_key": variant,
        "buildings": int(len(values)),
        # AOI change: the reach and what scenario 1 could cover of it.
        "reach_km": [reach_start, reach_end],
        "tariff_note": tariff_note,
        "intervals": int(len(series)) if series is not None else None,
        "priced_intervals": int(tariffs.size) if series is not None else None,
        "gauge_quality": str(fees["gauge_quality_15min"].iloc[0]),
        "gauge_window_start_utc": str(fees["gauge_window_start_utc"].iloc[0]),
        "gauge_window_end_utc": str(fees["gauge_window_end_utc"].iloc[0]),
        "section_areas_m2": {name: round(float(area), 1) for name, area in areas.items()},
        "tariff_period": tariff_period,
        "tariff_total_eur_season": (round(float(values["tariff_total_eur_season"].iloc[0]), 2)
                                    if series is not None else None),
        "water_volume_season_m3": (round(float(values["water_volume_season_m3"].iloc[0]), 1)
                                   if series is not None else None),
        "mean_tariff_eur": round(mean_tariff, 2) if mean_tariff is not None else None,
        "min_tariff_eur": round(float(tariffs.min()), 2) if mean_tariff is not None else None,
        "max_tariff_eur": round(float(tariffs.max()), 2) if mean_tariff is not None else None,
        "fee_buildings": fee_buildings,
        "rain_mm": float(rain_mm),
        "rain_source": rain_source,
        "rain_volume_m3": round(float(values["rain_volume_m3"].iloc[0]), 1),
        "rainwater_cost_eur": round(float(values["rainwater_cost_eur"].sum()), 2),
        "flood_risk_buildings": in_zone,
        "flood_risk_fee_eur": round(float(values["flood_risk_fee_eur"].sum()), 2),
        "water_quality_charge_eur": round(float(values["water_quality_charge_eur"].sum()), 2),
        "water_consumption_m3": round(float(values["water_consumption_m3"].sum()), 1),
        "water_consumption_cost_eur": round(float(values["water_consumption_cost_eur"].sum()), 2),
        "all_scenarios_eur": round(float(
            values["water_fee_season_eur"].sum() + values["rainwater_cost_eur"].sum()
            + values["flood_risk_fee_eur"].sum() + values["water_quality_charge_eur"].sum()
            + values["water_consumption_cost_eur"].sum()), 2),
        # AOI change: True when scenario 1 is missing or partial, i.e. the total above
        # leaves out (some of) the river tariff. tariff_note says which.
        "all_scenarios_partial": tariff_note is not None,
    }
