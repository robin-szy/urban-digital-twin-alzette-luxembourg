"""
Depths from the twin's REM, at the cost-model sample points: for a period of stored gauge
readings (sample_depth_series), or for chosen gauge stages such as the front-end slider's
(depths_at_stages). Runs INSIDE EDDIE (backend
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


class _Sampler:
    """Reads a level's depth at the sample points; grid cells are located once."""

    def __init__(self, samples: pd.DataFrame):
        from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel

        grid = flood_grid.dem_grid()
        self.cells = _grid_cells(samples, grid)
        inside = self.cells >= 0
        self.ground_known = np.zeros(len(self.cells), dtype=bool)
        self.ground_known[inside] = np.isfinite(grid["mean"].ravel()[self.cells[inside]])
        self._cache = {}

    def depths(self, key) -> np.ndarray:
        """0 in a dry cell, the water depth in a wet one, NaN outside the grid or the DEM."""
        if key not in self._cache:
            computed = _level_depth(key)
            wet = computed["wet"].ravel()
            depth = computed["depth"].ravel()
            known = self.ground_known
            values = np.full(len(self.cells), np.nan)
            values[known] = 0.0
            wet_here = known.copy()
            wet_here[known] = wet[self.cells[known]]
            values[wet_here] = np.maximum(depth[self.cells[wet_here]], 0.0)
            self._cache[key] = values
        return self._cache[key]


def depths_at_stages(stages_cm, samples: pd.DataFrame) -> pd.DataFrame:
    """Depth at every sample for each stage, as the front-end slider sets it: the same stage
    (cm) at Pfaffenthal, Hesperange and Livange. Wide table: samples x stages."""
    from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel

    sampler = _Sampler(samples)
    columns = {}
    for stage in stages_cm:
        key = flood_grid.level_key(stage, stage, stage)
        columns[stage] = sampler.depths(key)
        print(f"  stage {stage} cm: {int(np.nansum(columns[stage] > 0))} wet samples")
    table = pd.DataFrame(columns, index=samples.set_index(["buildingid", "sample_index"]).index)
    table.columns.name = "stage_cm"
    return table


def sample_depth_series(start_iso: str, end_iso: str, samples: pd.DataFrame) -> pd.DataFrame:
    """Long table buildingid, sample_index, timestamp, depth_m for every stored keyframe."""
    from eddie_water_level.blueprint import water_levels  # pylint: disable=import-outside-toplevel

    levels = water_levels({"start": start_iso, "end": end_iso})
    sampler = _Sampler(samples)
    frames = []
    for time, key in zip(levels["times"], levels["keyframe_levels"]):
        frame = samples[["buildingid", "sample_index"]].copy()
        frame["timestamp"] = pd.Timestamp(time)
        frame["depth_m"] = sampler.depths(key)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)
