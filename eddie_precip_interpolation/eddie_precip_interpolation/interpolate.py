"""Pure precipitation interpolation logic for the Alzette digital twin.

This module intentionally has no imports from ``eddie.*``.  It receives
GeoPandas/Pandas objects and returns a GeoDataFrame, so it can be unit-tested
without Docker, PostGIS, GeoServer, Celery, or the EDDIE frontend.

Version 1 design defaults
-------------------------
* CRS used for metric distance calculations: EPSG:2169
* Grid resolution: 100 m
* Interpolation: inverse-distance weighting (IDW)
* IDW power: p = 1 (provisional; selected in the first LOOCV experiment)
* Dynamic station selection: up to 5 nearest valid gauges within 15 km
* Minimum valid gauges: 3
* No silent radius expansion by default.  An optional fallback radius can be
  enabled explicitly; when used, the result is flagged as lower quality.
* Quality metadata includes station count, distance, convex-hull support,
  interpolation/extrapolation status, missingness, and fallback status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union

METRIC_CRS = "EPSG:2169"


@dataclass(frozen=True)
class IDWConfig:
    """Configuration for one interpolation run.

    Values are parameters, not permanent scientific constants.  In particular,
    ``power``, ``max_stations`` and ``search_radius_m`` should later be tuned
    jointly by historical cross-validation.
    """

    resolution_m: float = 100.0
    power: float = 1.0
    max_stations: int = 5
    search_radius_m: float = 15_000.0
    min_stations: int = 3
    fallback_radius_m: float | None = None
    metric_crs: str = METRIC_CRS

    def validate(self) -> None:
        if self.resolution_m <= 0:
            raise ValueError("resolution_m must be > 0")
        if self.power <= 0:
            raise ValueError("power must be > 0")
        if self.max_stations < 1:
            raise ValueError("max_stations must be >= 1")
        if self.min_stations < 1:
            raise ValueError("min_stations must be >= 1")
        if self.min_stations > self.max_stations:
            raise ValueError("min_stations cannot exceed max_stations")
        if self.search_radius_m <= 0:
            raise ValueError("search_radius_m must be > 0")
        if self.fallback_radius_m is not None and self.fallback_radius_m <= self.search_radius_m:
            raise ValueError("fallback_radius_m must be greater than search_radius_m")


def _require_crs(gdf: gpd.GeoDataFrame, name: str) -> None:
    if gdf.crs is None:
        raise ValueError(f"{name} must have a CRS")


def _to_metric(gdf: gpd.GeoDataFrame, metric_crs: str) -> gpd.GeoDataFrame:
    _require_crs(gdf, "GeoDataFrame")
    if str(gdf.crs).upper() == metric_crs.upper():
        return gdf.copy()
    return gdf.to_crs(metric_crs)


def build_grid(
    aoi_gdf: gpd.GeoDataFrame,
    resolution_m: float = 100.0,
    metric_crs: str = METRIC_CRS,
) -> gpd.GeoDataFrame:
    """Build a stable square grid and clip each retained cell to the AOI.

    ``grid_id`` is derived from row/column indices in the AOI bounding box and
    is therefore stable as long as the AOI, CRS, resolution and origin remain
    unchanged.
    """

    if resolution_m <= 0:
        raise ValueError("resolution_m must be > 0")
    if aoi_gdf.empty:
        raise ValueError("AOI is empty")

    aoi = _to_metric(aoi_gdf, metric_crs)
    aoi_geom = unary_union(aoi.geometry)
    minx, miny, maxx, maxy = aoi_geom.bounds

    # Snap grid origin down to a resolution multiple.  This improves stability
    # if an AOI is recreated with tiny floating-point differences.
    origin_x = np.floor(minx / resolution_m) * resolution_m
    origin_y = np.floor(miny / resolution_m) * resolution_m

    ncols = int(np.ceil((maxx - origin_x) / resolution_m))
    nrows = int(np.ceil((maxy - origin_y) / resolution_m))

    records: list[dict] = []
    for row in range(nrows):
        y0 = origin_y + row * resolution_m
        y1 = y0 + resolution_m
        for col in range(ncols):
            x0 = origin_x + col * resolution_m
            x1 = x0 + resolution_m
            cell = box(x0, y0, x1, y1)
            if not cell.intersects(aoi_geom):
                continue
            clipped = cell.intersection(aoi_geom)
            if clipped.is_empty:
                continue
            center = cell.centroid
            records.append(
                {
                    "grid_id": f"r{row:05d}_c{col:05d}",
                    "center_x": center.x,
                    "center_y": center.y,
                    "geometry": clipped,
                }
            )

    return gpd.GeoDataFrame(records, geometry="geometry", crs=metric_crs)


def _prepare_active_stations(
    stations_gdf: gpd.GeoDataFrame,
    observations_df: pd.DataFrame,
    timestamp: str | pd.Timestamp,
    metric_crs: str,
) -> tuple[gpd.GeoDataFrame, int]:
    """Return valid station observations at exactly ``timestamp``.

    Missing precipitation values are excluded, never converted to zero.
    The returned integer is the count of station observations that existed for
    the timestamp before dropping missing values, used as transparent
    missingness metadata.
    """

    required_station_cols = {"station_id", "geometry"}
    required_obs_cols = {"station_id", "datetime", "value"}
    if not required_station_cols.issubset(stations_gdf.columns):
        missing = required_station_cols - set(stations_gdf.columns)
        raise ValueError(f"stations_gdf missing columns: {sorted(missing)}")
    if not required_obs_cols.issubset(observations_df.columns):
        missing = required_obs_cols - set(observations_df.columns)
        raise ValueError(f"observations_df missing columns: {sorted(missing)}")

    stations = _to_metric(stations_gdf, metric_crs)

    obs = observations_df.copy()
    obs["datetime"] = pd.to_datetime(obs["datetime"], utc=True)
    target = pd.to_datetime(timestamp, utc=True)
    obs_at_t = obs.loc[obs["datetime"] == target, ["station_id", "datetime", "value"]].copy()
    observations_at_t = len(obs_at_t)
    obs_at_t["value"] = pd.to_numeric(obs_at_t["value"], errors="coerce")
    obs_at_t = obs_at_t.dropna(subset=["value"])

    # If duplicates exist for station/timestamp, keep the last row rather than
    # double-weighting one physical gauge.
    obs_at_t = obs_at_t.drop_duplicates(subset=["station_id", "datetime"], keep="last")

    active = stations[["station_id", "geometry"]].merge(obs_at_t, on="station_id", how="inner")
    active = gpd.GeoDataFrame(active, geometry="geometry", crs=stations.crs)
    return active, observations_at_t


def _candidate_pool_for_aoi(
    active: gpd.GeoDataFrame,
    aoi_metric: gpd.GeoDataFrame,
    radius_m: float,
) -> gpd.GeoDataFrame:
    if active.empty:
        return active.copy()
    aoi_geom = unary_union(aoi_metric.geometry)
    distance_to_aoi = active.geometry.distance(aoi_geom)
    return active.loc[distance_to_aoi <= radius_m].copy()


def _active_hull(active_pool: gpd.GeoDataFrame):
    if len(active_pool) < 3:
        return None
    return unary_union(active_pool.geometry).convex_hull


def _quality_class(
    stations_used: int,
    min_stations: int,
    inside_hull: bool,
    fallback_used: bool,
) -> str:
    """A deliberately transparent first categorical summary.

    Distance is retained as raw metadata and is *not* collapsed into this
    class yet; distance thresholds should be calibrated from historical error
    before being used as hard quality boundaries.
    """

    if stations_used < min_stations:
        return "INSUFFICIENT"
    if fallback_used:
        return "LOW"
    if not inside_hull:
        return "LOW"
    if stations_used >= 4:
        return "HIGH"
    return "MODERATE"


def idw_grid(
    stations_gdf: gpd.GeoDataFrame,
    observations_df: pd.DataFrame,
    aoi_gdf: gpd.GeoDataFrame,
    timestamp: str | pd.Timestamp,
    config: IDWConfig | None = None,
    grid_gdf: gpd.GeoDataFrame | None = None,
) -> gpd.GeoDataFrame:
    """
    Optimized IDW precipitation interpolation for one timestamp.

    Preserves:
    - Normal search radius and fallback radius
    - Maximum and minimum station requirements
    - IDW weighting and zero-distance handling
    - Convex-hull classification
    - All quality metadata
    """

    cfg = config or IDWConfig()
    cfg.validate()

    # 1. Prepare AOI and grid
    aoi_metric = _to_metric(aoi_gdf, cfg.metric_crs)

    grid = (
        build_grid(
            aoi_metric,
            cfg.resolution_m,
            cfg.metric_crs,
        )
        if grid_gdf is None
        else _to_metric(grid_gdf, cfg.metric_crs)
    )

    target_time = pd.to_datetime(timestamp, utc=True)

    # 2. Prepare active stations
    active, observations_at_t = _prepare_active_stations(
        stations_gdf,
        observations_df,
        target_time,
        cfg.metric_crs,
    )

    active_pool = _candidate_pool_for_aoi(
        active,
        aoi_metric,
        cfg.search_radius_m,
    )

    hull = _active_hull(active_pool)

    valid_station_count_at_t = len(active_pool)
    missing_station_count_at_t = max(
        observations_at_t - len(active), 0
    )

    # 3. Extract station coordinates and values ONCE
    pool_x = active_pool.geometry.x.to_numpy(dtype=float)
    pool_y = active_pool.geometry.y.to_numpy(dtype=float)
    pool_values = active_pool["value"].to_numpy(dtype=float)

    active_x = active.geometry.x.to_numpy(dtype=float)
    active_y = active.geometry.y.to_numpy(dtype=float)
    active_values = active["value"].to_numpy(dtype=float)

    # 4. Prepare all grid centers ONCE
    grid_x = grid["center_x"].to_numpy(dtype=float)
    grid_y = grid["center_y"].to_numpy(dtype=float)

    # Preserve the original convex-hull test,
    # but create center points only once.
    center_points = gpd.points_from_xy(
        grid_x,
        grid_y,
        crs=cfg.metric_crs,
    )

    if hull is None:
        inside_hull_flags = [False] * len(grid)
    else:
        inside_hull_flags = [
            bool(hull.contains(point) or hull.touches(point))
            for point in center_points
        ]

    results = []

    # 5. Compute IDW using NumPy arrays
    for i, cell in enumerate(grid.itertuples(index=False)):

        center_x = grid_x[i]
        center_y = grid_y[i]

        fallback_used = False
        radius_used = cfg.search_radius_m

        # Default to the normal station pool
        selected_distances = np.empty(0, dtype=float)
        selected_values = np.empty(0, dtype=float)

        if len(pool_x) > 0:

            dx = pool_x - center_x
            dy = pool_y - center_y

            pool_distances = np.sqrt(dx * dx + dy * dy)

            selected_indices = np.flatnonzero(
                pool_distances <= cfg.search_radius_m
            )

            distance_source = pool_distances
            value_source = pool_values

            # Apply the original fallback rule
            if (
                selected_indices.size < cfg.min_stations
                and cfg.fallback_radius_m is not None
            ):

                dx2 = active_x - center_x
                dy2 = active_y - center_y

                fallback_distances = np.sqrt(
                    dx2 * dx2 + dy2 * dy2
                )

                fallback_indices = np.flatnonzero(
                    fallback_distances <= cfg.fallback_radius_m
                )

                if fallback_indices.size >= cfg.min_stations:
                    selected_indices = fallback_indices
                    distance_source = fallback_distances
                    value_source = active_values

                    fallback_used = True
                    radius_used = cfg.fallback_radius_m

            # Sort nearest stations and respect max_stations
            if selected_indices.size > 0:

                order = np.argsort(
                    distance_source[selected_indices],
                    kind="stable",
                )

                selected_indices = selected_indices[
                    order
                ][:cfg.max_stations]

                selected_distances = distance_source[
                    selected_indices
                ]

                selected_values = value_source[
                    selected_indices
                ]

        # 6. Compute station metadata
        stations_used = len(selected_distances)

        if stations_used > 0:
            nearest_m = float(np.min(selected_distances))
            mean_m = float(np.mean(selected_distances))
        else:
            nearest_m = np.nan
            mean_m = np.nan

        # 7. Calculate IDW precipitation
        precip = np.nan

        if stations_used >= cfg.min_stations:

            zero_distance = np.flatnonzero(
                selected_distances == 0
            )

            if zero_distance.size > 0:
                precip = float(
                    selected_values[zero_distance[0]]
                )
            else:
                weights = 1.0 / np.power(
                    selected_distances,
                    cfg.power,
                )

                precip = float(
                    np.sum(weights * selected_values)
                    / np.sum(weights)
                )

        # 8. Preserve spatial and quality classifications
        inside_hull = inside_hull_flags[i]

        spatial_status = (
            "interpolation"
            if inside_hull
            else "extrapolation"
        )

        quality = _quality_class(
            stations_used=stations_used,
            min_stations=cfg.min_stations,
            inside_hull=inside_hull,
            fallback_used=fallback_used,
        )

        # 9. Preserve the original output schema
        results.append(
            {
                "grid_id": cell.grid_id,
                "datetime": target_time.isoformat(),
                "precipitation_mm": precip,
                "method": "IDW",
                "power": float(cfg.power),
                "stations_used": stations_used,
                "nearest_station_km": (
                    nearest_m / 1000.0
                    if np.isfinite(nearest_m)
                    else np.nan
                ),
                "mean_station_distance_km": (
                    mean_m / 1000.0
                    if np.isfinite(mean_m)
                    else np.nan
                ),
                "search_radius_km": radius_used / 1000.0,
                "inside_convex_hull": inside_hull,
                "spatial_status": spatial_status,
                "fallback_used": fallback_used,
                "quality_class": quality,
                "valid_station_count_at_t":
                    valid_station_count_at_t,
                "missing_station_count_at_t":
                    missing_station_count_at_t,
                "geometry": cell.geometry,
            }
        )

    return gpd.GeoDataFrame(
        results,
        geometry="geometry",
        crs=cfg.metric_crs,
    )

