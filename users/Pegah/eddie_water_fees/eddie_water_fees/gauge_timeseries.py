# -*- coding: utf-8 -*-
"""
15-minute time series of scenario 1: the non-residential river-water tariff.

This is ``users/Pegah/src/timeseries_model_first_scenario.py`` ported to EDDIE: the
classification, the footprint share, the gauge-section geometry and the filling of
missing readings are unchanged. Only the inputs moved:

* the kilometrage markers come from PostGIS ``base_river_km_primary`` instead of the
  GeoPackage file (same layer, same columns),
* the mapped river surface still comes from the OSM extract ``OSM_GPKG``
  (``gis_osm_water_a_free``, classes river / riverbank), which the containers mount,
* the buildings come from ``building_population`` instead of a CSV.

Method
------
Total estimated volume at time t::

    V(t) = sum over gauges g of  A_g * level_g(t) / 100

``A_g`` is the mapped water surface area (m², EPSG:2169) of the river section that
gauge g covers: from the midpoint to its downstream neighbour's gauge to the
midpoint to its upstream neighbour's gauge, along river km 35-50. ``level_g`` is the
gauge stage in cm, used as a uniform effective thickness over that area.

The tariff follows scenario 1::

    extra_pool(t) = V(t) * percentage
    tariff(t)     = extra_pool(t) * unit_price
    fee_b(t)      = tariff(t) * area_share_b        (share constant in time)

The season is charged **once**, on the period's mean level: the gauge stages are
averaged over the season into one height each, that gives one mean standing volume,
one 4 % pool and one tariff, shared by footprint. The 15-minute series is the input
of that average, not 8 832 separate bills (V(t) is linear in the stages, so the mean
of V(t) is the mean-stage volume).

Assumptions (deliberate, not measurements)
------------------------------------------
* The mapped river surface is an approximate fixed top-down area. This is a volume
  *indicator*, not a surveyed volume of water in the river; no riverbed geometry is
  used, so the stage is treated as the effective thickness.
* The tariff is a levy, not a bill for water consumed. Averaging the 15-minute
  series gives a representative fee for a period.
* A gauge without a reading is interpolated across the gauges at that timestamp
  (linear in river km, held flat outside the outer gauges). When no gauge reports,
  the previous 15-minute value is carried forward. Every interval is labelled
  ``observed`` / ``interpolated`` / ``carried`` / ``missing`` so a stale stretch
  stays visible instead of being averaged silently.
* Timestamps are treated as UTC. The source publishes Luxembourg local time and the
  stored history labels it UTC, so period boundaries can shift by an hour around a
  clock change.

Data notes for the current extract
----------------------------------
* The OSM water areas cover about 86 % of the kilometrage markers in the reach. The
  Livange section (km 49.845-50) has no mapped polygon at all, so its stage
  multiplies an area of 0 m² and the total is effectively Pfaffenthal + Hesperange.
* The marker polyline measures ~14.76 km for the 15 km reach, so section boundaries
  are located on measured distance along it, not on ``km * 1000``.

Standalone check, from ``users/Pegah/eddie_water_fees`` (host, no EDDIE needed)::

    python -m eddie_water_fees.gauge_timeseries --start 2026-11-20T14:00 --end 2026-11-20T18:00
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
import sqlalchemy as sa
from shapely.geometry import LineString
from shapely.ops import substring

# ============================================================
# CONFIGURATION
# ============================================================

TARGET_CRS = "EPSG:2169"

RIVER_NAME = "Alzette"
KM_START = 35.0
KM_END = 50.0
# The markers' KILO_KM carries float noise (46.299999); far below the 100 m spacing.
KM_TOLERANCE = 1e-3

# Half-width of the band around the river in which mapped water areas are counted for
# a section. Wide enough to hold the river and its banks.
SECTION_HALF_WIDTH_M = 60.0

MARKERS_TABLE = "base_river_km_primary"
MARKERS_LAYER = "Kilometrierung primäre Gewässer"

# The containers mount the OSM extract at /app/data/buildings/luxembourg.gpkg (OSM_GPKG).
OSM_GPKG = Path(os.environ.get("OSM_GPKG", "/app/data/buildings/luxembourg.gpkg"))
OSM_WATER_AREAS_LAYER = "gis_osm_water_a_free"
# The river surface, as mapped: OSM has no polygons of class "river" in this reach.
OSM_RIVER_AREA_CLASSES = ("river", "riverbank")

# Standalone runs (host) read the GeoPackage instead of PostGIS.
RIVER_KILOMETRAGE_GPKG = Path(
    os.environ.get("RIVER_KILOMETRAGE_GPKG", "data/raw/River_kilometers/kilometrage-des-cours-deau.gpkg")
)

API_ROOT = os.environ.get("GAUGE_API_ROOT", "https://heichwaasser.lu/api/v1")
REQUEST_TIMEOUT_S = 60
REQUEST_PAUSE_S = 0.2

GRID_FREQ = "15min"

# Scenario 1 defaults (main.py).
DEFAULT_PERCENTAGE = 0.04
DEFAULT_UNIT_PRICE = 7.0


@dataclass(frozen=True)
class Gauge:
    """One Alzette gauge of the reach."""

    name: str
    station_id: int
    km: float
    zero_m: float


# Pfaffenthal and Hesperange: eddie_water_level.tasks. Livange: AGE station sheet
# 01-Livange, the upstream gauge just beyond the reach. The zeros are recorded for
# reference only; this model uses the stage itself as the thickness.
GAUGES = (
    Gauge("Pfaffenthal", 33, 35.04, 235.25),
    Gauge("Hesperange", 57, 45.35, 255.74),
    Gauge("Livange", 35, 54.34, 265.80),
)


# ============================================================
# RIVER GEOMETRY
# ============================================================

def _normalise_markers(markers, km_start: float = KM_START, km_end: float = KM_END,
                       river: str = RIVER_NAME) -> gpd.GeoDataFrame:
    """The Alzette kilometrage markers of the reach, EPSG:2169, sorted by km.

    AOI change (Robin, 2026-11-25): km_start / km_end / river used to be the fixed
    KM_START / KM_END / RIVER_NAME. They are parameters now, so that EDDIE can pass the
    reach the user selected (tasks.py reads it from the AOI module). The defaults are your
    original values, so the standalone run and anything else calling this unchanged
    behaves exactly as before.
    """
    frame = markers.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    if "toponyme" not in frame.columns or "kilo_km" not in frame.columns:
        raise KeyError(f"markers need a toponyme and a kilo_km column; got {list(frame.columns)}")
    if frame.crs is None:
        frame = frame.set_crs(TARGET_CRS)
    frame = frame.to_crs(TARGET_CRS)
    # AOI change: river / km_start / km_end instead of RIVER_NAME / KM_START / KM_END.
    selected = frame[
        (frame["toponyme"] == river)
        & (frame["kilo_km"] >= km_start - KM_TOLERANCE)
        & (frame["kilo_km"] <= km_end + KM_TOLERANCE)
    ].sort_values("kilo_km")
    if len(selected) < 2:
        raise ValueError(f"Fewer than 2 {river} markers between km {km_start} and {km_end}.")
    return selected.reset_index(drop=True)


def markers_from_postgis(conn: sa.Connection, km_start: float = KM_START, km_end: float = KM_END,
                         river: str = RIVER_NAME) -> gpd.GeoDataFrame:
    """The reach markers of ``base_river_km_primary`` (EPSG:2169).

    AOI change (Robin, 2026-11-25): the reach is a parameter (default: your km 35-50 on
    the Alzette, as before). See _normalise_markers.
    """
    sql = sa.text(
        f'SELECT toponyme, kilo_km, geometry FROM "{MARKERS_TABLE}" '
        "WHERE toponyme = :river AND kilo_km BETWEEN :low AND :high ORDER BY kilo_km"
    )
    markers = gpd.read_postgis(
        sql, conn, geom_col="geometry",
        params={"river": river, "low": km_start - KM_TOLERANCE, "high": km_end + KM_TOLERANCE},
    )
    if markers.empty:
        raise ValueError(f"No {river} markers between km {km_start} and {km_end} in {MARKERS_TABLE}.")
    return _normalise_markers(markers, km_start, km_end, river)


def markers_from_file(path: str | Path = RIVER_KILOMETRAGE_GPKG) -> gpd.GeoDataFrame:
    """The reach markers of the kilometrage GeoPackage (standalone runs on the host)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"River kilometrage file not found: {path}. Set RIVER_KILOMETRAGE_GPKG, or run inside EDDIE "
            f"where the markers come from {MARKERS_TABLE}."
        )
    return _normalise_markers(gpd.read_file(path, layer=MARKERS_LAYER))


def centreline(markers: gpd.GeoDataFrame) -> tuple[LineString, np.ndarray, np.ndarray]:
    """The river centreline of the reach.

    Returns
    -------
    tuple
        (line, kms, distances): the polyline through the kilometrage markers, the
        river km of each marker, and its measured distance in metres along the line
        from the first marker. The line is slightly shorter than the nominal km span
        because 100 m spacing chords cut the bends, so converting a river km to a
        position must go through these measured distances.
    """
    points = list(markers.geometry)
    line = LineString(points)
    kms = markers["kilo_km"].to_numpy(dtype="float64")
    steps = np.array([points[i].distance(points[i + 1]) for i in range(len(points) - 1)])
    distances = np.concatenate([[0.0], np.cumsum(steps)])
    return line, kms, distances


def distance_at_km(km: float, kms: np.ndarray, distances: np.ndarray) -> float:
    """Measured distance along the centreline of a river km, clamped to the reach."""
    return float(np.interp(km, kms, distances))


def gauge_sections(markers: gpd.GeoDataFrame) -> dict[str, tuple[float, float]]:
    """The stretch of river each gauge covers, in metres along the centreline.

    A gauge covers from the midpoint to its downstream neighbour's gauge to the
    midpoint to its upstream neighbour's gauge; the outermost gauges are extended to
    the reach boundary (km 35 / km 50).

    Returns
    -------
    dict
        Gauge name -> (start_m, end_m) along the centreline.
    """
    line, kms, distances = centreline(markers)
    ordered = sorted(GAUGES, key=lambda gauge: gauge.km)
    boundaries = [float(distances[0])]
    for downstream, upstream in zip(ordered, ordered[1:]):
        midpoint_km = (downstream.km + upstream.km) / 2.0
        boundaries.append(distance_at_km(midpoint_km, kms, distances))
    boundaries.append(float(distances[-1]))
    boundaries = [min(max(value, 0.0), line.length) for value in boundaries]
    return {gauge.name: (boundaries[i], boundaries[i + 1]) for i, gauge in enumerate(ordered)}


def load_water_areas(markers: gpd.GeoDataFrame, osm_path: str | Path = OSM_GPKG) -> gpd.GeoDataFrame:
    """The mapped river surface polygons near the reach (EPSG:2169, classes river / riverbank)."""
    path = Path(osm_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"OSM extract not found: {path}. It provides the mapped river surface; set OSM_GPKG or mount it."
        )
    line, _, _ = centreline(markers)
    bbox = tuple(gpd.GeoSeries([line.buffer(300.0)], crs=TARGET_CRS).to_crs(4326).total_bounds)
    water = gpd.read_file(path, layer=OSM_WATER_AREAS_LAYER, bbox=bbox).to_crs(TARGET_CRS)
    water = water[water["fclass"].isin(OSM_RIVER_AREA_CLASSES)]
    if water.empty:
        raise ValueError(f"No {'/'.join(OSM_RIVER_AREA_CLASSES)} polygons near the reach in {path}.")
    return water


def section_areas(
    markers: gpd.GeoDataFrame,
    water: gpd.GeoDataFrame | None = None,
    half_width_m: float = SECTION_HALF_WIDTH_M,
) -> dict[str, float]:
    """Mapped water surface area of each gauge section, in m².

    The section ribbon is the centreline stretch buffered by half_width_m, cut at the
    section boundaries, so one long riverbank polygon is split between the sections
    it crosses instead of being assigned to one of them whole.

    Returns
    -------
    dict
        Gauge name -> area in m². A section with no mapped polygon gets 0.0.
    """
    line, _, _ = centreline(markers)
    water = load_water_areas(markers) if water is None else water

    areas = {}
    for name, (start_m, end_m) in gauge_sections(markers).items():
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
    """A reading timestamp as a tz-naive UTC timestamp.

    The source sends Luxembourg local time without an offset; the stored history
    labels it UTC, and so does this module.
    """
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp


def fetch_readings(station_id: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Readings of one station in [start, end] from the public API.

    Returns
    -------
    pd.DataFrame
        Columns ``time`` (tz-naive UTC) and ``level_cm``, sorted by time. Empty when
        the station has no reading in the period. The API's -1000 sentinel values are
        dropped, as in the collector.
    """
    path = f"/stations/{station_id}/start/{int(start.timestamp()) - 1}/end/{int(end.timestamp()) + 1}"
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
    """Read gauge readings from a local file, so the model runs without the API.

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
    """Fill missing gauge stages and label how each value was obtained.

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
    areas: dict[str, float],
    percentage: float = DEFAULT_PERCENTAGE,
    unit_price: float = DEFAULT_UNIT_PRICE,
) -> pd.DataFrame:
    """The 15-minute volume indicator and tariff series.

    Parameters
    ----------
    readings : dict
        Station id -> DataFrame with ``time`` and ``level_cm``.
    areas : dict
        Gauge name -> section area in m² (see section_areas).
    percentage : float
        Share of the volume that forms the tariff pool (scenario 1: 0.04).
    unit_price : float
        Tariff per m³ (scenario 1: 7.0).

    Returns
    -------
    pd.DataFrame
        One row per 15-minute interval: the gauge stages in cm and their sources, the
        section volumes and the total, ``extra_pool_m3``, ``tariff_eur`` and the
        interval's ``quality``. Intervals where a gauge with a non-zero area has no
        value at all keep NaN volumes rather than a silently reduced total.
    """
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
    result["quality"] = [_worst_source(pd.Series([sources.at[stamp, name] for name in used])) for stamp in grid]

    result["extra_pool_m3"] = result["total_volume_m3"] * percentage
    result["tariff_eur"] = result["extra_pool_m3"] * unit_price
    result["percentage"] = percentage
    result["unit_price_eur_per_m3"] = unit_price

    if duplicates:
        print(f" -> Warning: {duplicates} duplicated reading timestamp(s) were collapsed by the 15-minute grid.")
    return result


def period(start=None, end=None) -> tuple[pd.Timestamp, pd.Timestamp]:
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


def load_readings(readings_json, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> dict:
    """Station id -> readings, from the local file when given, else from the gauge API."""
    if readings_json is not None:
        return readings_from_json(readings_json)
    return {gauge.station_id: fetch_readings(gauge.station_id, start_ts, end_ts) for gauge in GAUGES}


# ============================================================
# BUILDINGS AND FEES
# ============================================================

def with_footprint(buildings: pd.DataFrame | gpd.GeoDataFrame) -> pd.DataFrame | gpd.GeoDataFrame:
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


def area_share(buildings: pd.DataFrame | gpd.GeoDataFrame) -> np.ndarray:
    """Footprint share of every non-residential building, from scenario 1.

    The classification is ``simple_model_first_scenario.non_residential_water_allocation``:
    only an exact ``building_type == "non-residential"`` shares the pool, by footprint,
    and residential or blank rows get 0. Share is constant in time because the
    building set does not change.
    """
    prepared = with_footprint(buildings)
    if "building_type" in prepared.columns:
        b_type = prepared["building_type"].astype(str).str.lower().str.strip()
        is_non_res = (b_type == "non-residential").to_numpy()
    else:
        is_non_res = np.zeros(len(prepared), dtype=bool)
    footprint = pd.to_numeric(prepared["footprint_m2"], errors="coerce").to_numpy(dtype="float64")
    footprint = np.where(np.isnan(footprint), 0.0, footprint)

    total_non_res_area = float(footprint[is_non_res].sum())
    shares = np.zeros(len(prepared), dtype="float64")
    if total_non_res_area > 0.0:
        shares[is_non_res] = footprint[is_non_res] / total_non_res_area
    return shares


def _worst_quality(qualities: pd.Series) -> str:
    """The weakest interval label of a period: missing beats carried beats interpolated."""
    for label in ("missing", "carried", "interpolated"):
        if (qualities == label).any():
            return label
    return "observed"


def fee_table(buildings: pd.DataFrame | gpd.GeoDataFrame, series: pd.DataFrame) -> pd.DataFrame:
    """The 15-minute tariff of every building, as one row per building.

    Parameters
    ----------
    buildings : pd.DataFrame | gpd.GeoDataFrame
        The buildings of the result set, in the order of the rows to update.
    series : pd.DataFrame
        Output of volume_series.

    Returns
    -------
    pd.DataFrame
        ``building_id`` plus the columns added to building_population: the share, the
        mean / min / max fee over the priced intervals, the season figures (the mean
        standing volume of the river over the period and the charge it implies), and the
        period's constants (mean tariff, number of intervals, worst quality, window).

    The season is charged once, on the mean level: the stages are averaged over the
    period, multiplied by their section areas -> one mean standing volume -> one pool ->
    one tariff, shared by footprint. It is *not* the sum of the period's interval tariffs
    (that would bill the same river once per quarter-hour and inflate the fee ~8 832
    times); ``water_fee_season_eur`` therefore equals ``water_fee_15min_mean_eur`` by
    construction, and ``tariff_intervals_15min`` says how many intervals were averaged.
    """
    shares = area_share(buildings)
    tariffs = series["tariff_eur"].dropna()
    if not len(tariffs):
        raise ValueError("No priced interval in the period; nothing to allocate.")
    mean_pool = float(series["extra_pool_m3"].mean())
    # The season is charged once, on the season's mean water level - not once per interval.
    # V(t) is linear in the stages, so averaging V(t) is the same as taking each gauge's mean
    # stage and multiplying it by its section area: one mean standing volume, one pool.
    season_pool_m3 = float(series["extra_pool_m3"].dropna().mean())
    season_volume_m3 = float(series["total_volume_m3"].dropna().mean())
    season_tariff_eur = float(tariffs.mean())

    table = pd.DataFrame({
        "water_area_share_15min": shares,
        "allocated_water_volume_15min_m3": mean_pool * shares,
        "water_fee_15min_mean_eur": float(tariffs.mean()) * shares,
        "water_fee_15min_min_eur": float(tariffs.min()) * shares,
        "water_fee_15min_max_eur": float(tariffs.max()) * shares,
        "water_fee_season_eur": season_tariff_eur * shares,
        "allocated_water_volume_season_m3": season_pool_m3 * shares,
    })
    table["tariff_mean_eur_15min"] = float(tariffs.mean())
    table["tariff_total_eur_season"] = season_tariff_eur
    table["water_volume_season_m3"] = season_volume_m3
    table["tariff_intervals_15min"] = int(tariffs.size)
    table["gauge_quality_15min"] = _worst_quality(series.loc[tariffs.index, "quality"])
    table["gauge_window_start_utc"] = pd.Timestamp(series["timestamp"].min())
    table["gauge_window_end_utc"] = pd.Timestamp(series["timestamp"].max())

    id_column = next(
        (column for column in ("row_id", "gml_id", "buildingid", "localid") if column in buildings.columns), None
    )
    if id_column is None:
        raise KeyError("The building table has no row_id, gml_id, buildingid or localid column to match rows on.")
    table.insert(0, "building_id", buildings[id_column].to_numpy())
    return table


# ============================================================
# STANDALONE CHECK (host, no EDDIE)
# ============================================================

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gauge sections, 15-minute volumes and tariff, without EDDIE.")
    parser.add_argument("--start", help="period start, ISO 8601 (UTC); default 7 days before end")
    parser.add_argument("--end", help="period end, ISO 8601 (UTC); default now")
    parser.add_argument("--readings-json", help="local readings file instead of the gauge API")
    parser.add_argument("--markers", help=f"kilometrage GeoPackage; default {RIVER_KILOMETRAGE_GPKG}")
    parser.add_argument("--osm", help=f"OSM extract; default {OSM_GPKG}")
    parser.add_argument("--percentage", type=float, default=DEFAULT_PERCENTAGE)
    parser.add_argument("--unit-price", type=float, default=DEFAULT_UNIT_PRICE)
    args = parser.parse_args(argv)

    markers = markers_from_file(args.markers) if args.markers else markers_from_file()
    water = load_water_areas(markers, args.osm) if args.osm else load_water_areas(markers)
    areas = section_areas(markers, water)
    print("Section areas (m²): " + ", ".join(f"{name} {area:,.0f}" for name, area in areas.items()))

    start_ts, end_ts = period(args.start, args.end)
    readings = load_readings(args.readings_json, start_ts, end_ts)
    for gauge in GAUGES:
        print(f" -> {gauge.name} (station {gauge.station_id}): {len(readings.get(gauge.station_id, []))} readings")

    series = volume_series(readings, areas=areas, percentage=args.percentage, unit_price=args.unit_price)
    valid = series["tariff_eur"].dropna()
    print(f"Intervals: {len(series)} ({valid.size} priced), quality {series['quality'].value_counts().to_dict()}")
    print(f"Mean total volume: {series['total_volume_m3'].mean():,.0f} m³")
    print(f"Mean tariff: {valid.mean():,.2f} EUR  (min {valid.min():,.2f}, max {valid.max():,.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
