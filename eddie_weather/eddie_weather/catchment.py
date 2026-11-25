"""Rain per sub-catchment: from a grid of weather points to one value per area.

The reanalysis (ERA5-Land, about 0.1 degree) and the forecasts give values on a regular
grid. Each grid point stands for the cell around it, so the rain of an area is the mean of
the cells it covers, weighted by the share of the area inside each cell:

    rain(area) = sum over cells of  rain(cell) x overlap(cell, area) / area(area)

The weights are computed once per area and kept as a matrix (areas x points); a whole time
series is then one matrix product. Areas are measured in an equal-area projection
(sinusoidal, centred on the Alzette), so no projection library is needed.

The sub-catchments are the areas between consecutive gauges: the catchment of a gauge minus
the catchment of the gauge upstream. Times are UTC everywhere; the levels of detail (day,
month, season, year) are cut in local time, the way people read them.

Pure numpy, pandas and shapely: no database and no ``eddie`` import, so it is tested alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import shapely
from shapely import ops
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry

EARTH_RADIUS_M = 6371008.8
CENTRAL_LON = 6.1  # Alzette basin
LOCAL_TIME_ZONE = "Europe/Luxembourg"
ERA5_LAND_STEP_DEG = 0.1


# --- areas --------------------------------------------------------------------------------

def _sinusoidal(coords: np.ndarray) -> np.ndarray:
    phi = np.radians(coords[:, 1])
    x = EARTH_RADIUS_M * np.radians(coords[:, 0] - CENTRAL_LON) * np.cos(phi)
    y = EARTH_RADIUS_M * phi
    return np.column_stack([x, y])


def equal_area(geometry: BaseGeometry) -> BaseGeometry:
    """The geometry (longitude, latitude) in an equal-area projection, in metres."""
    return shapely.transform(geometry, _sinusoidal)


def area_km2(geometry: BaseGeometry) -> float:
    """Area of a longitude/latitude geometry in km2."""
    return equal_area(geometry).area / 1e6


def _clean(geometry: BaseGeometry) -> BaseGeometry:
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def sub_catchments(catchments: Mapping[str, BaseGeometry], upstream: Mapping[str, Sequence[str]],
                   min_area_km2: float = 0.5) -> Dict[str, BaseGeometry]:
    """The area between each gauge and the gauges directly upstream of it.

    ``catchments`` maps a gauge to its whole catchment (longitude, latitude). ``upstream``
    maps a gauge to the gauges directly upstream (an empty list for a headwater gauge).
    Slivers left by small differences between nested outlines (under ``min_area_km2``) are
    dropped from the result.
    """
    result = {}
    for gauge, whole in catchments.items():
        part = _clean(whole)
        for up in upstream.get(gauge, ()):
            part = part.difference(_clean(catchments[up]))
        part = _clean(part)
        if isinstance(part, MultiPolygon):
            kept = [p for p in part.geoms if area_km2(p) >= min_area_km2]
            part = MultiPolygon(kept) if len(kept) > 1 else (kept[0] if kept else Polygon())
        if part.is_empty:
            raise ValueError(f"the sub-catchment of {gauge} is empty: check the upstream gauges")
        result[gauge] = part
    return result


# --- grid and weights ---------------------------------------------------------------------

def grid_points(geometry: BaseGeometry, step_deg: float = ERA5_LAND_STEP_DEG) -> List[Tuple[float, float]]:
    """Centres (latitude, longitude) of the grid cells that touch ``geometry``.

    The cells are aligned on multiples of ``step_deg``, like the ERA5-Land grid, so asking
    the weather service for these exact points returns the grid values themselves.
    """
    minx, miny, maxx, maxy = geometry.bounds
    lons = np.arange(math.floor(minx / step_deg + 0.5), math.floor(maxx / step_deg + 0.5) + 1) * step_deg
    lats = np.arange(math.floor(miny / step_deg + 0.5), math.floor(maxy / step_deg + 0.5) + 1) * step_deg
    half = step_deg / 2.0
    points = []
    for lat in lats:
        for lon in lons:
            if box(lon - half, lat - half, lon + half, lat + half).intersects(geometry):
                points.append((round(float(lat), 6), round(float(lon), 6)))
    return points


def cell_weights(areas: Mapping[str, BaseGeometry], points: Sequence[Tuple[float, float]],
                 step_deg: float = ERA5_LAND_STEP_DEG) -> np.ndarray:
    """Matrix (areas x points): the share of each area that lies in each point's cell.

    Every row sums to 1 when the cells cover the area. A part of an area outside every cell
    (the grid was too small) is an error, because its rain would silently be lost.
    """
    half = step_deg / 2.0
    cells = [equal_area(box(lon - half, lat - half, lon + half, lat + half)) for lat, lon in points]
    weights = np.zeros((len(areas), len(points)))
    for row, (name, geometry) in enumerate(areas.items()):
        projected = equal_area(_clean(geometry))
        total = projected.area
        if total <= 0:
            raise ValueError(f"{name} has no area")
        for col, cell in enumerate(cells):
            if cell.intersects(projected):
                weights[row, col] = cell.intersection(projected).area / total
        covered = weights[row].sum()
        if covered < 0.999:
            raise ValueError(f"the grid covers only {covered:.1%} of {name}: add points around it")
        weights[row] /= covered
    return weights


@dataclass
class Places:
    """The areas the rain is given for, with their weights on a set of grid points."""

    names: List[str]
    area_km2: np.ndarray
    points: List[Tuple[float, float]]
    weights: np.ndarray  # names x points

    @classmethod
    def build(cls, areas: Mapping[str, BaseGeometry], step_deg: float = ERA5_LAND_STEP_DEG) -> "Places":
        merged = ops.unary_union([_clean(g) for g in areas.values()])
        points = grid_points(merged, step_deg)
        return cls(names=list(areas), area_km2=np.array([area_km2(g) for g in areas.values()]),
                   points=points, weights=cell_weights(areas, points, step_deg))

    def average(self, values_by_point) -> np.ndarray:
        """Values (time x points) to values (time x places).

        A missing value (NaN) at a point is replaced by the weighted mean of the other points
        of the same place at that time; a place with no value at all stays NaN.
        """
        values = np.asarray(values_by_point, dtype=float)
        single = values.ndim == 1
        values = np.atleast_2d(values)
        if values.shape[1] != len(self.points):
            raise ValueError(f"expected {len(self.points)} points, got {values.shape[1]}")
        present = ~np.isnan(values)
        filled = np.where(present, values, 0.0)
        sums = filled @ self.weights.T
        shares = present.astype(float) @ self.weights.T
        with np.errstate(invalid="ignore", divide="ignore"):
            result = np.where(shares > 1e-9, sums / shares, np.nan)
        return result[0] if single else result

    def combine(self, names: Sequence[str], values_by_place) -> np.ndarray:
        """Area-weighted mean of several places, e.g. all sub-catchments above a gauge."""
        index = [self.names.index(n) for n in names]
        values = np.atleast_2d(np.asarray(values_by_place, dtype=float))
        areas = self.area_km2[index]
        return values[:, index] @ areas / areas.sum()

    def to_geojson(self, geometries: Mapping[str, BaseGeometry], extra: Optional[Mapping[str, dict]] = None) -> dict:
        """The places as a GeoJSON feature collection (for the map)."""
        features = []
        for name, a in zip(self.names, self.area_km2):
            props = {"id": name, "area_km2": round(float(a), 2)}
            props.update((extra or {}).get(name, {}))
            features.append({"type": "Feature", "id": name, "properties": props,
                             "geometry": mapping(geometries[name])})
        return {"type": "FeatureCollection", "features": features}


# --- volumes ------------------------------------------------------------------------------

def rain_volume_m3(rain_mm, area_km2_value):
    """Water brought by ``rain_mm`` on ``area_km2_value``: 1 mm on 1 km2 is 1,000 m3."""
    return np.asarray(rain_mm, dtype=float) * np.asarray(area_km2_value, dtype=float) * 1000.0


# --- levels of detail ---------------------------------------------------------------------

LEVELS = ("15min", "hour", "day", "month", "season", "year")
_FREQ = {"15min": "15min", "hour": "h", "day": "D", "month": "MS", "year": "YS"}
_STEP_MINUTES = {"15min": 15, "hour": 60}


def _first_days(years, months, time_zone: str) -> pd.DatetimeIndex:
    """Local midnights of the first day of the given months (never ambiguous in Europe)."""
    naive = pd.to_datetime(pd.DataFrame({"year": np.asarray(years), "month": np.asarray(months), "day": 1}))
    return pd.DatetimeIndex(naive).tz_localize(time_zone)


def _season_start(local: pd.DatetimeIndex, time_zone: str) -> pd.DatetimeIndex:
    """First day (local midnight) of the meteorological season: Dec-Feb, Mar-May, ..."""
    month = np.asarray(local.month)
    year = np.asarray(local.year)
    start_month = np.where(month == 12, 12, np.where(month < 3, 12, (month // 3) * 3))
    start_year = np.where(month < 3, year - 1, year)
    return _first_days(start_year, start_month, time_zone)


def aggregate(series: pd.Series, level: str, how: str = "sum", step_minutes: Optional[int] = None,
              time_zone: str = LOCAL_TIME_ZONE) -> pd.DataFrame:
    """Values at fine times (UTC index) gathered to a level of detail.

    Returns one row per period with ``start`` (UTC), ``value``, ``count`` (values found) and
    ``complete`` (all expected values found). Days, months, seasons and years start at local
    midnight, so a day is 23 or 25 hours long when the clocks change. Periods are labelled
    by their start, as in the stored tables. ``how`` is ``sum`` (rain) or ``mean``
    (temperature, flow). Seasons are labelled by their first day: winter 2024 starts on
    1 December 2023.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}: use one of {', '.join(LEVELS)}")
    if how not in ("sum", "mean", "max"):
        raise ValueError("how must be sum, mean or max")
    if series.index.tz is None:
        raise ValueError("the times must carry a time zone (UTC)")
    series = series.sort_index()
    if step_minutes is None:
        step_minutes = _step_minutes(series.index)
    local = series.index.tz_convert(time_zone)
    if level in _STEP_MINUTES:
        # Luxembourg is a whole number of hours from UTC: cut in UTC, no ambiguous hour.
        keys = series.index.tz_convert("UTC").floor(_FREQ[level]).tz_convert(time_zone)
    elif level == "day":
        keys = local.tz_localize(None).normalize().tz_localize(time_zone)
    elif level == "month":
        keys = _first_days(local.year, local.month, time_zone)
    elif level == "year":
        keys = _first_days(local.year, np.ones(len(local), dtype=int), time_zone)
    else:
        keys = _season_start(local, time_zone)
    frame = pd.DataFrame({"key": keys, "value": series.to_numpy(dtype=float)})
    grouped = frame.groupby("key", sort=True)["value"]
    value = getattr(grouped, how)(min_count=1) if how == "sum" else getattr(grouped, how)()
    count = grouped.count()
    starts = value.index
    ends = [_period_end(s, level, time_zone) for s in starts]
    expected = np.array([(e - s).total_seconds() / 60.0 / step_minutes for s, e in zip(starts, ends)])
    out = pd.DataFrame({
        "start": starts.tz_convert("UTC"),
        "value": value.to_numpy(),
        "count": count.to_numpy(),
        "complete": count.to_numpy() >= np.round(expected),
    })
    return out.reset_index(drop=True)


def _period_end(start: pd.Timestamp, level: str, time_zone: str) -> pd.Timestamp:
    if level == "15min":
        return start + pd.Timedelta(minutes=15)
    if level == "hour":
        return start + pd.Timedelta(hours=1)
    naive = start.tz_localize(None)
    if level == "day":
        nxt = naive + pd.Timedelta(days=1)
    elif level == "month":
        nxt = naive + pd.offsets.MonthBegin(1)
    elif level == "season":
        nxt = naive + pd.offsets.MonthBegin(3)
    else:
        nxt = naive + pd.offsets.YearBegin(1)
    return nxt.tz_localize(time_zone)


def _step_minutes(index: pd.DatetimeIndex) -> int:
    """The usual time step of a series, in minutes (60 for a single value)."""
    if len(index) < 2:
        return 60
    gaps = np.diff(index.tz_convert("UTC").tz_localize(None).values).astype("timedelta64[s]").astype(float)
    return max(1, int(round(float(np.median(gaps)) / 60.0)))


def daily_for_model(series: pd.Series, how: str = "sum", utc_offset_hours: int = 1) -> pd.Series:
    """Daily values on a fixed clock (UTC+1, like CAMELS-LUX), for GR4J.

    The model needs days of equal length; the observed flow of CAMELS-LUX is given in
    UTC+1 all year, so the rain and evaporation fed to the model are cut the same way.
    Days with a missing hour are NaN.
    """
    shifted = series.copy()
    shifted.index = series.index.tz_convert("UTC").tz_localize(None) + pd.Timedelta(hours=utc_offset_hours)
    grouped = shifted.groupby(shifted.index.floor("D"))
    counts = grouped.count()
    values = grouped.sum(min_count=1) if how == "sum" else grouped.mean()
    step = _step_minutes(series.index)
    values[counts < round(1440 / step)] = np.nan
    values.index.name = "day"
    return values


def read_geojson(collection: dict, id_field: str) -> Dict[str, BaseGeometry]:
    """Geometries of a GeoJSON feature collection, keyed by a property."""
    return {str(f["properties"][id_field]): shape(f["geometry"]) for f in collection["features"]}
