# -*- coding: utf-8 -*-
"""
15-minute time series of scenario 1: the non-residential river-water tariff.

Companion to ``simple_model_first_scenario.py``. The classification (exact
``building_type == "non-residential"``) and the footprint allocation are reused
from it unchanged; only the single ``total_water_volume`` input is replaced by a
volume indicator computed for every 15-minute gauge reading.

Method
------
Total estimated volume at time t::

    V(t) = sum over gauges g of  A_g * level_g(t) / 100

``A_g`` is the mapped water surface area (m², EPSG:2169) of the river section that
gauge g covers: from the midpoint to its downstream neighbour's gauge to the
midpoint to its upstream neighbour's gauge, along river km 35-50. ``level_g`` is
the gauge stage in cm, used as a uniform effective thickness over that area.

The tariff follows scenario 1::

    extra_pool(t) = V(t) * percentage
    tariff(t)     = extra_pool(t) * unit_price
    fee_b(t)      = tariff(t) * area_share_b        (share constant in time)

Assumptions (deliberate, not measurements)
------------------------------------------
* The riverbank polygons are an approximate fixed top-down surface. This is a
  volume *indicator*, not a surveyed volume of water in the river; no riverbed
  geometry is used, so the stage is treated as the effective thickness.
* The tariff is a levy, not a bill for water consumed. Averaging the 15-minute
  series gives a representative fee for a period; it is not the amount of water
  used in that period.
* A gauge without a reading is interpolated across the gauges at that timestamp
  (linear in river km, held flat outside the outer gauges). When no gauge
  reports, the previous 15-minute value is carried forward. Every interval is
  labelled ``observed`` / ``interpolated`` / ``carried`` / ``missing`` so a
  stale stretch stays visible instead of being averaged silently.
* Timestamps are treated as UTC. The source publishes Luxembourg local time and
  the stored history labels it UTC, so period boundaries can shift by an hour
  around a clock change.

Data notes for the current extract
----------------------------------
* The OSM water areas (``river``/``riverbank``) cover about 86 % of the
  kilometrage markers in the reach. The Livange section (km 49.845-50) has no
  mapped polygon at all, so its stage multiplies an area of 0 m² and the total
  is effectively Pfaffenthal + Hesperange.
* The marker polyline measures ~14.76 km for the 15 km reach, so section
  boundaries are located on measured distance along it, not on ``km * 1000``.

Usage
-----
From the project root::

    python users/Pegah/src/timeseries_model_first_scenario.py --start 2026-11-14 --end 2026-11-20
    python users/Pegah/src/timeseries_model_first_scenario.py --readings-json readings.json

It can also be imported::

    from src.timeseries_model_first_scenario import run
    result = run(start="2026-11-14", end="2026-11-20")

No file is written outside ``outputs/``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import substring

try:  # imported as src.timeseries_model_first_scenario
    from .config import KM_END, KM_START, RIVER_NAME
    from .simple_model_first_scenario import non_residential_water_allocation
except ImportError:  # run as a script from the project root
    from config import KM_END, KM_START, RIVER_NAME
    from simple_model_first_scenario import non_residential_water_allocation


# ============================================================
# CONFIGURATION
# ============================================================

# src/timeseries_model_first_scenario.py -> users/Pegah -> users -> project root
REPO_ROOT = Path(__file__).resolve().parents[3]

DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "outputs"

KILOMETRAGE_GPKG = Path(
    os.environ.get(
        "RIVER_KILOMETRAGE_GPKG",
        DATA_DIR / "raw" / "River_kilometers" / "kilometrage-des-cours-deau.gpkg",
    )
)
KILOMETRAGE_LAYER = "Kilometrierung primäre Gewässer"

OSM_GPKG = Path(
    os.environ.get("OSM_GPKG", DATA_DIR / "raw" / "Buildings" / "luxembourg.gpkg")
)
OSM_WATER_AREAS_LAYER = "gis_osm_water_a_free"
# The river surface, as mapped: OSM has no polygons of class "river" in this reach.
OSM_RIVER_AREA_CLASSES = ("river", "riverbank")

# Building table with building_type, footprint_m2 and (optionally) geometry, i.e.
# the export of the main pipeline. The raw ACT2023v2 footprints have neither
# building_type nor footprint_m2, so they cannot classify non-residential buildings.
DEFAULT_BUILDINGS_PATH = OUTPUT_DIR / "buildings_population_final.csv"

TARGET_CRS = "EPSG:2169"

# KILO_KM carries float noise (46.299999); far below the 100 m marker spacing.
KM_TOLERANCE = 1e-3

# Half-width of the band around the river in which mapped water areas are
# counted for a section. Wide enough to hold the river and its banks.
SECTION_HALF_WIDTH_M = 60.0

API_ROOT = os.environ.get("GAUGE_API_ROOT", "https://heichwaasser.lu/api/v1")
REQUEST_TIMEOUT_S = 60
REQUEST_PAUSE_S = 0.2

GRID_FREQ = "15min"

# Scenario 1 defaults (main.py).
DEFAULT_PERCENTAGE = 0.10
DEFAULT_UNIT_PRICE = 7.0

# Above this volume of rows the per-interval building table is not written by
# default; the per-building summary is always written.
BUILDING_SERIES_ROW_LIMIT = 2_000_000


@dataclass(frozen=True)
class Gauge:
    """One Alzette gauge of the reach."""

    name: str
    station_id: int
    km: float
    zero_m: float


# Pfaffenthal and Hesperange: eddie_water_level.tasks. Livange: AGE station sheet
# 01-Livange, the upstream gauge just beyond the reach. The zeros are recorded
# for reference only; this model uses the stage itself as the thickness.
GAUGES = (
    Gauge("Pfaffenthal", 33, 35.04, 235.25),
    Gauge("Hesperange", 57, 45.35, 255.74),
    Gauge("Livange", 35, 54.34, 265.80),
)


# ============================================================
# RIVER GEOMETRY
# ============================================================

def _alzette_markers() -> gpd.GeoDataFrame:
    """The Alzette kilometrage markers of the reach, EPSG:2169, sorted by km."""
    if not KILOMETRAGE_GPKG.is_file():
        raise FileNotFoundError(
            f"River kilometrage file not found: {KILOMETRAGE_GPKG}. "
            "Download kilometrage-des-cours-deau.gpkg from data.public.lu into data/raw/River_kilometers/."
        )
    markers = gpd.read_file(KILOMETRAGE_GPKG, layer=KILOMETRAGE_LAYER).to_crs(TARGET_CRS)
    selected = markers[
        (markers["TOPONYME"] == RIVER_NAME)
        & (markers["KILO_KM"] >= KM_START - KM_TOLERANCE)
        & (markers["KILO_KM"] <= KM_END + KM_TOLERANCE)
    ].sort_values("KILO_KM")
    if len(selected) < 2:
        raise ValueError(
            f"Fewer than 2 {RIVER_NAME} markers between km {KM_START} and {KM_END} in {KILOMETRAGE_GPKG}."
        )
    return selected.reset_index(drop=True)


def centreline() -> tuple[LineString, np.ndarray, np.ndarray]:
    """
    The river centreline of the reach.

    Returns
    -------
    tuple
        (line, kms, distances): the polyline through the kilometrage markers, the
        river km of each marker, and its measured distance in metres along the
        line from the first marker. The line is slightly shorter than the nominal
        km span because 100 m spacing chords cut the bends, so converting a
        river km to a position must go through these measured distances.
    """
    markers = _alzette_markers()
    points = list(markers.geometry)
    line = LineString(points)
    kms = markers["KILO_KM"].to_numpy(dtype="float64")
    steps = np.array([points[i].distance(points[i + 1]) for i in range(len(points) - 1)])
    distances = np.concatenate([[0.0], np.cumsum(steps)])
    return line, kms, distances


def distance_at_km(km: float, kms: np.ndarray, distances: np.ndarray) -> float:
    """Measured distance along the centreline of a river km, clamped to the reach."""
    return float(np.interp(km, kms, distances))


def gauge_sections() -> dict[str, tuple[float, float]]:
    """
    The stretch of river each gauge covers, in metres along the centreline.

    A gauge covers from the midpoint to its downstream neighbour's gauge to the
    midpoint to its upstream neighbour's gauge; the outermost gauges are extended
    to the reach boundary (km 35 / km 50).

    Returns
    -------
    dict
        Gauge name -> (start_m, end_m) along the centreline.
    """
    line, kms, distances = centreline()
    ordered = sorted(GAUGES, key=lambda gauge: gauge.km)
    boundaries = [float(distances[0])]
    for downstream, upstream in zip(ordered, ordered[1:]):
        midpoint_km = (downstream.km + upstream.km) / 2.0
        boundaries.append(distance_at_km(midpoint_km, kms, distances))
    boundaries.append(float(distances[-1]))
    boundaries = [min(max(value, 0.0), line.length) for value in boundaries]
    return {
        gauge.name: (boundaries[i], boundaries[i + 1])
        for i, gauge in enumerate(ordered)
    }


def section_areas(half_width_m: float = SECTION_HALF_WIDTH_M) -> dict[str, float]:
    """
    Mapped water surface area of each gauge section, in m².

    The section ribbon is the centreline stretch buffered by half_width_m, cut at
    the section boundaries, so one long riverbank polygon is split between the
    sections it crosses instead of being assigned to one of them whole.

    Returns
    -------
    dict
        Gauge name -> area in m². A section with no mapped polygon gets 0.0.
    """
    line, _, _ = centreline()
    if not OSM_GPKG.is_file():
        raise FileNotFoundError(
            f"OSM extract not found: {OSM_GPKG}. It provides the mapped river surface."
        )
    bbox = tuple(gpd.GeoSeries([line.buffer(300.0)], crs=TARGET_CRS).to_crs(4326).total_bounds)
    water = gpd.read_file(OSM_GPKG, layer=OSM_WATER_AREAS_LAYER, bbox=bbox).to_crs(TARGET_CRS)
    water = water[water["fclass"].isin(OSM_RIVER_AREA_CLASSES)]
    if water.empty:
        raise ValueError(f"No {'/'.join(OSM_RIVER_AREA_CLASSES)} polygons near the reach in {OSM_GPKG}.")

    areas = {}
    for name, (start_m, end_m) in gauge_sections().items():
        if end_m - start_m <= 0.0:
            areas[name] = 0.0
            continue
        stretch = substring(line, start_m, min(end_m, line.length))
        ribbon = stretch.buffer(half_width_m)
        clipped = water.geometry.intersection(ribbon)
        clipped = clipped[~clipped.is_empty]
        areas[name] = float(clipped.area.sum())
    return areas


# ============================================================
# GAUGE READINGS
# ============================================================

def _get_json(path: str) -> dict:
    """One call of the Héichwaasser API, as the gauge_collector does it."""
    with urllib.request.urlopen(f"{API_ROOT}{path}", timeout=REQUEST_TIMEOUT_S) as response:
        payload = json.load(response)
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload


def _as_utc(value: str) -> pd.Timestamp:
    """
    A reading timestamp as a tz-naive UTC timestamp.

    The source sends Luxembourg local time without an offset; the stored history
    labels it UTC, and so does this module. Keeping the same convention means the
    series lines up with water_level_history.
    """
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp


def fetch_readings(station_id: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """
    Readings of one station in [start, end] from the public API.

    Returns
    -------
    pd.DataFrame
        Columns ``time`` (tz-naive UTC) and ``level_cm``, sorted by time. Empty
        when the station has no reading in the period. The API's -1000 sentinel
        values are dropped, as in the collector.
    """
    path = (
        f"/stations/{station_id}/start/{int(start.timestamp()) - 1}"
        f"/end/{int(end.timestamp()) + 1}"
    )
    data = _get_json(path)
    rows = []
    for measurement in data.get("measurements") or []:
        value, stamp = measurement.get("value"), measurement.get("timestamp")
        if value is None or stamp is None or float(value) < -1000:
            continue
        timestamp = _as_utc(stamp)
        if start <= timestamp <= end:
            rows.append((timestamp, float(value)))
    table = pd.DataFrame(rows, columns=["time", "level_cm"]).sort_values("time")
    time.sleep(REQUEST_PAUSE_S)
    return table.reset_index(drop=True)


def readings_from_json(path: str | Path) -> dict[int, pd.DataFrame]:
    """
    Read gauge readings from a local file, so the model runs without the API.

    Accepted formats
    ----------------
    * ``{"33": [{"timestamp": "...", "value": 73.3}, ...], "57": [...], ...}``
    * a list of records ``[{"station_id": 33, "timestamp": "...", "water_level_cm": 73.3}, ...]``
    * a CSV with columns ``station_id, datetime, water_level_cm``

    Returns
    -------
    dict
        Station id -> DataFrame with ``time`` and ``level_cm``.
    """
    path = Path(path)
    records: list[tuple[int, str, float]] = []
    if path.suffix.lower() == ".csv":
        table = pd.read_csv(path)
        renamed = table.rename(columns={c: c.strip().lower() for c in table.columns})
        for row in renamed.itertuples(index=False):
            records.append((int(row.station_id), str(row.datetime), float(row.water_level_cm)))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for station_id, measurements in payload.items():
                for measurement in measurements:
                    records.append((
                        int(station_id),
                        str(measurement.get("timestamp", measurement.get("datetime"))),
                        float(measurement.get("value", measurement.get("water_level_cm"))),
                    ))
        else:
            for record in payload:
                records.append((
                    int(record["station_id"]),
                    str(record.get("timestamp", record.get("datetime"))),
                    float(record.get("value", record.get("water_level_cm"))),
                ))

    by_station: dict[int, list[tuple[pd.Timestamp, float]]] = {}
    for station_id, stamp, value in records:
        by_station.setdefault(station_id, []).append((_as_utc(stamp), value))
    return {
        station_id: pd.DataFrame(sorted(values), columns=["time", "level_cm"])
        for station_id, values in by_station.items()
    }


# ============================================================
# STAGE GRID: OBSERVED -> INTERPOLATED -> CARRIED
# ============================================================

def _fill_stages(levels: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fill missing gauge stages and label how each value was obtained.

    Step 1, observed: a reading at that timestamp.
    Step 2, interpolated: interpolated across the gauges in river km, holding the
        nearest gauge flat outside the outer gauges.
    Step 3, carried: the previous interval's value, for intervals where no gauge
        reports at all.
    Step 4, missing: no reading yet at the very start of the series.

    Returns
    -------
    tuple
        (stages, sources): the filled stages (cm) and, per gauge, one label of
        observed / interpolated / carried / missing per timestamp.
    """
    names = [gauge.name for gauge in GAUGES]
    kms = np.array([gauge.km for gauge in GAUGES], dtype="float64")
    values = levels[names].to_numpy(dtype="float64")
    sources = np.full(values.shape, "", dtype=object)
    sources[~np.isnan(values)] = "observed"

    for row in range(values.shape[0]):
        known = ~np.isnan(values[row])
        if known.all() or not known.any():
            continue
        for column in np.where(~known)[0]:
            values[row, column] = float(np.interp(kms[column], kms[known], values[row][known]))
            sources[row, column] = "interpolated"

    previous = None
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            if np.isnan(values[row, column]):
                if previous is not None and not np.isnan(previous[column]):
                    values[row, column] = previous[column]
                    sources[row, column] = "carried"
                else:
                    sources[row, column] = "missing"
        previous = values[row].copy()

    filled = pd.DataFrame(values, index=levels.index, columns=names)
    labels = pd.DataFrame(sources, index=levels.index, columns=names)
    return filled, labels


def _worst_source(labels: pd.Series) -> str:
    """The weakest label of an interval: missing beats carried beats interpolated."""
    for label in ("missing", "carried", "interpolated"):
        if (labels == label).any():
            return label
    return "observed"


# ============================================================
# VOLUME AND TARIFF SERIES
# ============================================================

def volume_series(
    readings: dict[int, pd.DataFrame],
    areas: dict[str, float] | None = None,
    percentage: float = DEFAULT_PERCENTAGE,
    unit_price: float = DEFAULT_UNIT_PRICE,
) -> pd.DataFrame:
    """
    The 15-minute volume indicator and tariff series.

    Parameters
    ----------
    readings : dict
        Station id -> DataFrame with ``time`` and ``level_cm``.
    areas : dict | None
        Gauge name -> section area in m²; computed from the mapped river surface
        when omitted.
    percentage : float
        Share of the volume that forms the tariff pool (scenario 1: 0.10).
    unit_price : float
        Tariff per m³ (scenario 1: 7.0).

    Returns
    -------
    pd.DataFrame
        One row per 15-minute interval: the gauge stages in cm and their sources,
        the section volumes and the total, ``extra_pool_m3``, ``tariff_eur`` and
        the interval's ``quality``. Intervals where a gauge with a non-zero area
        has no value at all keep NaN volumes rather than a silently reduced total.
    """
    areas = section_areas() if areas is None else areas
    names = [gauge.name for gauge in GAUGES]

    times = [table["time"] for table in readings.values() if not table.empty]
    if not times:
        raise ValueError("No gauge readings in the period.")
    first = min(table.min() for table in times).floor(GRID_FREQ)
    last = max(table.max() for table in times).ceil(GRID_FREQ)
    grid = pd.date_range(first, last, freq=GRID_FREQ)

    levels = pd.DataFrame(index=grid)
    duplicates = 0
    for gauge in GAUGES:
        table = readings.get(gauge.station_id)
        if table is None or table.empty:
            levels[gauge.name] = np.nan
            continue
        duplicates += int(table["time"].duplicated().sum())
        # One reading per timestamp: the last one wins.
        ordered = table.drop_duplicates("time", keep="last").set_index("time")["level_cm"]
        levels[gauge.name] = ordered.reindex(grid)

    stages, sources = _fill_stages(levels)

    area_series = pd.Series({name: float(areas.get(name, 0.0)) for name in names})
    contributions = stages.mul(area_series, axis=1) / 100.0
    # A section without mapped surface has no volume whatever its gauge reports.
    empty = area_series[area_series == 0.0].index
    if len(empty):
        contributions[list(empty)] = 0.0
    total = contributions.sum(axis=1, min_count=len(names))

    result = pd.DataFrame({"timestamp": grid})
    result["total_volume_m3"] = total.to_numpy()
    for name in names:
        result[f"{name.lower()}_level_cm"] = stages[name].to_numpy()
        result[f"{name.lower()}_section_volume_m3"] = contributions[name].to_numpy()
        result[f"{name.lower()}_source"] = sources[name].to_numpy()

    used = [name for name in names if area_series[name] > 0.0] or names
    result["quality"] = [
        _worst_source(pd.Series([sources.at[stamp, name] for name in used]))
        for stamp in grid
    ]

    result["extra_pool_m3"] = result["total_volume_m3"] * percentage
    result["tariff_eur"] = result["extra_pool_m3"] * unit_price
    result["percentage"] = percentage
    result["unit_price_eur_per_m3"] = unit_price

    if duplicates:
        print(f" -> Warning: {duplicates} duplicated reading timestamp(s) were collapsed by the 15-minute grid.")
    return result


# ============================================================
# BUILDINGS AND FEES
# ============================================================

def load_buildings(path: str | Path = DEFAULT_BUILDINGS_PATH) -> pd.DataFrame | gpd.GeoDataFrame:
    """
    Read the building table used for the fee split.

    Needs ``building_type`` (for the non-residential classification) and a
    footprint column: ``footprint_m2``, else ``area``, else the geometry area.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Building table not found: {path}. Run main.py to export "
            "outputs/buildings_population_final.csv, or pass --buildings."
        )
    if path.suffix.lower() == ".csv":
        table = pd.read_csv(path)
    else:
        table = gpd.read_file(path)
    table.columns = table.columns.str.strip().str.lower()
    return table


def _with_footprint(buildings: pd.DataFrame | gpd.GeoDataFrame) -> pd.DataFrame | gpd.GeoDataFrame:
    """Ensure a footprint_m2 column, from footprint_m2, area or the geometry."""
    if "footprint_m2" in buildings.columns:
        return buildings
    table = buildings.copy()
    if "area" in table.columns:
        table["footprint_m2"] = pd.to_numeric(table["area"], errors="coerce")
        return table
    if hasattr(table, "geometry"):
        table["footprint_m2"] = table.geometry.area
        return table
    raise KeyError(
        "The building table has no footprint_m2 or area column (and no geometry), "
        "so non-residential footprints cannot be computed."
    )


def building_shares(buildings: pd.DataFrame | gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Footprint share of every non-residential building, from scenario 1.

    The classification and the share are taken from
    ``non_residential_water_allocation`` so both models agree; its cost column is
    not used (it multiplies a zero-initialised column and stays 0). Share is
    constant in time because the building set does not change.

    Returns
    -------
    pd.DataFrame
        The buildings with ``area_share`` (0 for residential / unclassified).
    """
    prepared = _with_footprint(buildings)
    allocated = non_residential_water_allocation(
        prepared, total_water_volume=1.0, percentage=1.0, unit_price=1.0
    )
    return allocated


def building_fees(
    series: pd.DataFrame,
    allocated: pd.DataFrame,
    write_series: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """
    The tariff of each non-residential building: every interval, and one fee.

    Parameters
    ----------
    series : pd.DataFrame
        Output of volume_series.
    allocated : pd.DataFrame
        Output of building_shares.
    write_series : bool
        Also return the full per-interval table (one row per building and
        interval), which is large for a multi-week period.

    Returns
    -------
    tuple
        (fees, per_interval): one row per building with the mean / min / max fee
        over the intervals that have a volume, and optionally the long table.
    """
    shares = allocated["area_share"].to_numpy(dtype="float64")
    tariffs = series["tariff_eur"]
    valid = tariffs.dropna()

    identified = allocated
    id_column = next(
        (column for column in ("buildingid", "gml_id", "localid") if column in identified.columns),
        None,
    )
    fees = pd.DataFrame({
        "building_id": identified[id_column] if id_column else identified.index,
        "footprint_m2": pd.to_numeric(identified["footprint_m2"], errors="coerce"),
        "area_share": shares,
    }, index=identified.index)
    mean_volume = float(series["total_volume_m3"].mean())
    mean_pool = float(series["extra_pool_m3"].mean())
    fees["mean_allocated_volume_m3"] = mean_pool * shares
    fees["mean_total_volume_m3"] = mean_volume
    fees["mean_fee_eur"] = valid.mean() * shares
    fees["min_fee_eur"] = valid.min() * shares
    fees["max_fee_eur"] = valid.max() * shares
    fees["intervals_used"] = int(valid.size)

    per_interval = None
    rows = int(valid.size) * len(shares)
    if write_series and rows <= BUILDING_SERIES_ROW_LIMIT:
        stamps = series.loc[valid.index, "timestamp"].to_numpy()
        costs = np.outer(valid.to_numpy(), shares)
        per_interval = pd.DataFrame(costs, columns=fees["building_id"].to_numpy())
        per_interval.insert(0, "timestamp", stamps)
    elif write_series:
        print(
            f" -> Per-interval building table skipped: {rows:,} rows exceed "
            f"BUILDING_SERIES_ROW_LIMIT ({BUILDING_SERIES_ROW_LIMIT:,})."
        )
    return fees, per_interval


# ============================================================
# RUN
# ============================================================

def _period(start, end) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The period as tz-naive UTC timestamps; default the 7 days ending now."""
    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.utcnow().tz_localize(None)
    start_ts = pd.Timestamp(start) if start is not None else end_ts - pd.Timedelta(days=7)
    if start_ts.tzinfo is not None:
        start_ts = start_ts.tz_convert("UTC").tz_localize(None)
    if end_ts.tzinfo is not None:
        end_ts = end_ts.tz_convert("UTC").tz_localize(None)
    if start_ts >= end_ts:
        raise ValueError("start must be before end.")
    return start_ts, end_ts


def _load_readings(readings_json, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> dict:
    """Station id -> readings, from the local file when given, else from the gauge API."""
    if readings_json is not None:
        return readings_from_json(readings_json)
    return {gauge.station_id: fetch_readings(gauge.station_id, start_ts, end_ts) for gauge in GAUGES}


def period_fees(
    buildings: pd.DataFrame | gpd.GeoDataFrame,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    readings_json: str | Path | None = None,
    percentage: float = DEFAULT_PERCENTAGE,
    unit_price: float = DEFAULT_UNIT_PRICE,
    areas: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None, dict[str, float]]:
    """
    The 15-minute series and the fee of every building of an in-memory frame.

    This is the entry point for the main pipeline, where the buildings are already
    filtered and enriched instead of read from a file.

    Parameters
    ----------
    buildings : pd.DataFrame | gpd.GeoDataFrame
        Buildings with ``building_type`` and a footprint column (see building_shares).
    start, end : str | pd.Timestamp | None
        Period bounds; default the 7 days ending now.
    readings_json : str | Path | None
        Local readings file instead of the gauge API.
    percentage, unit_price : float
        Scenario 1 tariff parameters.
    areas : dict | None
        Gauge name -> section area in m²; computed from the mapped river surface when omitted.

    Returns
    -------
    tuple
        (series, fees, per_interval, areas): the interval table (volume_series), the fee of
        every building in ``buildings`` order, None for the long table, and the section areas.
    """
    start_ts, end_ts = _period(start, end)
    areas = section_areas() if areas is None else areas
    readings = _load_readings(readings_json, start_ts, end_ts)
    series = volume_series(readings, areas=areas, percentage=percentage, unit_price=unit_price)
    allocated = building_shares(buildings)
    fees, per_interval = building_fees(series, allocated, write_series=False)
    return series, fees, per_interval, areas

def run(
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    readings_json: str | Path | None = None,
    buildings_path: str | Path | None = None,
    percentage: float = DEFAULT_PERCENTAGE,
    unit_price: float = DEFAULT_UNIT_PRICE,
    output_dir: str | Path | None = None,
    write_building_series: bool = False,
) -> dict:
    """
    Build the 15-minute series, the tariffs and the per-building fees, and write them.

    Parameters
    ----------
    start, end : str | pd.Timestamp | None
        Period bounds (UTC, tz-naive or aware). Default: the 7 days ending now.
    readings_json : str | Path | None
        Local readings file instead of the API.
    buildings_path : str | Path | None
        Building table; default outputs/buildings_population_final.csv. Without it
        the tariff series is still produced and the fee split is skipped.
    percentage, unit_price : float
        Scenario 1 tariff parameters.
    output_dir : str | Path | None
        Where the CSV files go; default <project>/outputs.
    write_building_series : bool
        Also write the per-interval per-building table, when it is not too large.

    Returns
    -------
    dict
        ``series``, ``fees`` (or None), ``per_interval`` (or None), ``areas``,
        ``paths`` of the written files and a ``summary`` of the period.
    """
    start_ts, end_ts = _period(start, end)

    print("Step 1: Areas of the gauge sections from the mapped river surface...")
    areas = section_areas()
    for name, area in areas.items():
        print(f" -> {name}: {area:,.0f} m²")
    if not any(areas.values()):
        raise ValueError("No mapped river surface in any section; check OSM_GPKG.")

    print(f"Step 2: Gauge readings {start_ts:%Y-%m-%d %H:%M} to {end_ts:%Y-%m-%d %H:%M} (UTC)...")
    readings = _load_readings(readings_json, start_ts, end_ts)
    for gauge in GAUGES:
        count = len(readings.get(gauge.station_id, pd.DataFrame()))
        print(f" -> {gauge.name} (station {gauge.station_id}): {count} readings")

    print("Step 3: 15-minute volume indicator and tariff...")
    series = volume_series(readings, areas=areas, percentage=percentage, unit_price=unit_price)
    counted = series["quality"].value_counts().to_dict()
    print(f" -> {len(series)} intervals, quality: {counted}")

    fees = per_interval = None
    print("Step 4: Footprint share of the non-residential buildings...")
    path = Path(buildings_path) if buildings_path is not None else DEFAULT_BUILDINGS_PATH
    if path.is_file():
        allocated = building_shares(load_buildings(path))
        fees, per_interval = building_fees(series, allocated, write_series=write_building_series)
        print(f" -> {int((fees['area_share'] > 0).sum())} non-residential buildings")
    else:
        print(f" -> No building table at {path}; tariffs written, fee split skipped.")

    out_dir = Path(output_dir) if output_dir is not None else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"series": out_dir / "timeseries_first_scenario_tariff.csv"}
    series.to_csv(paths["series"], index=False)
    if fees is not None:
        paths["fees"] = out_dir / "timeseries_first_scenario_building_fees.csv"
        fees.to_csv(paths["fees"], index=False)
        if per_interval is not None:
            if len(per_interval) <= BUILDING_SERIES_ROW_LIMIT:
                paths["building_series"] = out_dir / "timeseries_first_scenario_building_costs.csv"
                per_interval.to_csv(paths["building_series"], index=False)
            else:
                print(f" -> Per-interval building table skipped: {len(per_interval):,} rows.")

    valid = series["tariff_eur"].dropna()
    summary = {
        "intervals": int(len(series)),
        "intervals_with_tariff": int(valid.size),
        "mean_total_volume_m3": float(series["total_volume_m3"].mean()),
        "mean_tariff_eur": float(valid.mean()) if valid.size else float("nan"),
        "min_tariff_eur": float(valid.min()) if valid.size else float("nan"),
        "max_tariff_eur": float(valid.max()) if valid.size else float("nan"),
        "quality": counted,
    }
    print("Step 5: Done.")
    print(f" -> Intervals: {summary['intervals']} ({summary['intervals_with_tariff']} priced)")
    print(f" -> Mean total volume: {summary['mean_total_volume_m3']:,.0f} m³")
    print(f" -> Mean tariff: {summary['mean_tariff_eur']:,.2f} EUR")
    for label, where in paths.items():
        print(f" -> {label}: {where}")

    return {
        "series": series,
        "fees": fees,
        "per_interval": per_interval,
        "areas": areas,
        "summary": summary,
        "paths": paths,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="15-minute scenario 1: gauge-derived river volume, tariff and non-residential fee."
    )
    parser.add_argument("--start", help="period start, ISO 8601 (UTC); default 7 days before end")
    parser.add_argument("--end", help="period end, ISO 8601 (UTC); default now")
    parser.add_argument("--readings-json", help="local readings file instead of the gauge API")
    parser.add_argument("--buildings", help=f"building table; default {DEFAULT_BUILDINGS_PATH}")
    parser.add_argument("--percentage", type=float, default=DEFAULT_PERCENTAGE,
                        help="share of the volume forming the tariff pool (default 0.10)")
    parser.add_argument("--unit-price", type=float, default=DEFAULT_UNIT_PRICE,
                        help="tariff per m³ (default 7.0)")
    parser.add_argument("--output-dir", help="where the CSV files go; default <project>/outputs")
    parser.add_argument("--building-series", action="store_true",
                        help="also write the per-interval cost of every building")
    args = parser.parse_args(argv)

    run(
        start=args.start,
        end=args.end,
        readings_json=args.readings_json,
        buildings_path=args.buildings,
        percentage=args.percentage,
        unit_price=args.unit_price,
        output_dir=args.output_dir,
        write_building_series=args.building_series,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
