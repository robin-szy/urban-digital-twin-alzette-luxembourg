"""
Depth time series from the twin's gauge history and REM. Runs INSIDE EDDIE (backend
container): it needs the gauge history database, the DEM and eddie_water_level.

For every timestep of a period:
  1. eddie_water_level.blueprint.water_levels(start, end) gives the stored gauge
     readings as keyframes and the level drawn at each (flood_grid.level_key);
  2. the level's water is computed on the 1 m computation grid, by the same method the
     map uses (rem.compute_level for gauge levels with LIVE_WATER_METHOD=rem,
     otherwise flood_grid.compute_level);
  3. its depth is read at each cost-model sample point.

A sample in a dry cell gets 0; a sample outside the grid or the DEM gets NaN (unknown).

Note: the static HQ10/HQ100 depths come from the reconstructed water surface over the
5 m DEM; these come from REM over the 1 m grid. Similar, but not identical.
"""

import numpy as np
import pandas as pd


def _grid_cells(samples: pd.DataFrame, grid: dict) -> np.ndarray:
    """Flat grid-cell index of every sample (-1 outside the grid)."""
    import geopandas as gpd  # pylint: disable=import-outside-toplevel
    from rasterio.transform import rowcol  # pylint: disable=import-outside-toplevel

    points = gpd.GeoSeries(gpd.points_from_xy(samples["x_epsg2169"], samples["y_epsg2169"]), crs=2169)
    points = points.to_crs(grid["crs"])
    rows, cols = rowcol(grid["transform"], points.x.to_numpy(), points.y.to_numpy())
    rows, cols = np.asarray(rows), np.asarray(cols)
    inside = (rows >= 0) & (rows < grid["shape"][0]) & (cols >= 0) & (cols < grid["shape"][1])
    return np.where(inside, rows * grid["shape"][1] + cols, -1)


def _level_depth(key) -> dict:
    """Water of one level key, by the method the map uses for it."""
    from eddie_water_level import flood_grid, rem  # pylint: disable=import-outside-toplevel

    return rem.compute_level(key) if flood_grid.uses_rem(key) else flood_grid.compute_level(key)


def sample_depth_series(start_iso: str, end_iso: str, samples: pd.DataFrame) -> pd.DataFrame:
    """Long table buildingid, sample_index, timestamp, depth_m for every stored keyframe."""
    from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel
    from eddie_water_level.blueprint import water_levels  # pylint: disable=import-outside-toplevel

    levels = water_levels({"start": start_iso, "end": end_iso})
    grid = flood_grid.dem_grid()
    cells = _grid_cells(samples, grid)
    inside = cells >= 0
    ground_known = np.zeros(len(cells), dtype=bool)
    ground_known[inside] = np.isfinite(grid["mean"].ravel()[cells[inside]])

    by_key = {}
    frames = []
    for time, key in zip(levels["times"], levels["keyframe_levels"]):
        if key not in by_key:
            computed = _level_depth(key)
            wet = computed["wet"].ravel()
            depth = computed["depth"].ravel()
            values = np.full(len(cells), np.nan)
            values[ground_known] = 0.0
            wet_here = ground_known.copy()
            wet_here[ground_known] = wet[cells[ground_known]]
            values[wet_here] = np.maximum(depth[cells[wet_here]], 0.0)
            by_key[key] = values
        frame = samples.index.to_frame(index=False)
        frame["timestamp"] = pd.Timestamp(time)
        frame["depth_m"] = by_key[key]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)
