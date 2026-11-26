"""
Water depth at the cost-model sample points, from the twin's own water model.

The depths come from eddie_water_level (the REM / flood grid the map draws, read only), so
they exist only in the BACKEND container: it has the DEM, the REM cache and their settings.
The dynamic tasks run in the Celery worker, so they ask the backend over HTTP:

  backend   LocalDepths            eddie_water_level -> depth per sample, for a level key
            GET /flood-costs/dynamic/depths.json?stage=<cm> | ?key=<level key>
            GET /flood-costs/dynamic/keyframes.json?start=..&end=..   (stored gauge readings)
  worker    BackendDepths          calls those two routes (FLOOD_COSTS_BACKEND_URL)

Both sides read the same samples from the database in the same order; every answer carries
the run's dataset_version and a fingerprint of the sample order, and the worker refuses
depths that do not match.

A sample in a dry cell gets 0; outside the grid or the DEM it gets NaN (unknown).
Moved from users/devika/dynamic_flood_cost_3models/dynamic_flood_costs/rem_depths.py.
"""
import json
import math
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import pandas as pd

DEFAULT_BACKEND_URL = "http://backend:5000"


class DepthsUnavailable(RuntimeError):
    """The water model could not give depths (backend down, wrong run, no gauge readings)."""


# ----------------------------------------------------------------------- backend side


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


class LocalDepths:
    """Depths read from eddie_water_level, inside the backend. Grid cells are located once."""

    def __init__(self, samples: pd.DataFrame):
        from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel

        grid = flood_grid.dem_grid()
        self.cells = _grid_cells(samples.reset_index(), grid)
        inside = self.cells >= 0
        self.ground_known = np.zeros(len(self.cells), dtype=bool)
        self.ground_known[inside] = np.isfinite(grid["mean"].ravel()[self.cells[inside]])

    @staticmethod
    def stage_key(stage_cm: float):
        """The level the front-end slider shows: the same stage at the three gauges."""
        from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel
        return flood_grid.level_key(stage_cm, stage_cm, stage_cm)

    @staticmethod
    def parse_key(text: str):
        from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel
        return flood_grid.parse_key(text)

    @staticmethod
    def key_text(key) -> str:
        from eddie_water_level import flood_grid  # pylint: disable=import-outside-toplevel
        return flood_grid.key_text(key)

    def depths(self, key) -> np.ndarray:
        """0 in a dry cell, the water depth in a wet one, NaN outside the grid or the DEM."""
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import flood_grid, rem

        computed = rem.compute_level(key) if flood_grid.uses_rem(key) else flood_grid.compute_level(key)
        wet = computed["wet"].ravel()
        depth = computed["depth"].ravel()
        known = self.ground_known
        values = np.full(len(self.cells), np.nan)
        values[known] = 0.0
        wet_here = known.copy()
        wet_here[known] = wet[self.cells[known]]
        values[wet_here] = np.maximum(depth[self.cells[wet_here]], 0.0)
        return values

    @staticmethod
    def keyframes(start_iso: str, end_iso: str) -> dict:
        """Times and level keys of the stored gauge readings of a period (as the map replays them)."""
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import flood_grid
        from eddie_water_level.blueprint import water_levels

        levels = water_levels({"start": start_iso, "end": end_iso})
        return {"times": [pd.Timestamp(t).isoformat() for t in levels["times"]],
                "keys": [flood_grid.key_text(k) for k in levels["keyframe_levels"]]}


def encode(values: np.ndarray) -> list:
    """Depths as JSON: rounded to the millimetre, NaN -> null."""
    return [None if not math.isfinite(v) else round(float(v), 3) for v in values]


# ----------------------------------------------------------------------- worker side


class BackendDepths:
    """Depths from the backend's routes; checks that they belong to the expected run and sample order."""

    def __init__(self, run: dict, signature: str, n_samples: int, base_url: str | None = None,
                 timeout: float = 300, attempts: int = 3, wait_s: float = 30):
        self.base_url = (base_url or os.environ.get("FLOOD_COSTS_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
        self.run, self.signature, self.n_samples, self.timeout = run, signature, n_samples, timeout
        self.attempts, self.wait_s = attempts, wait_s

    def _get(self, path: str, **query) -> dict:
        """GET a backend route; retried when the backend is busy or (re)starting."""
        url = f"{self.base_url}{path}?{urlencode(query)}"
        error = None
        for attempt in range(self.attempts):
            try:
                with urlopen(url, timeout=self.timeout) as response:  # nosec - internal service URL
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:          # the backend answered: its message is the result
                try:
                    return json.loads(exc.read().decode("utf-8"))
                except ValueError:
                    error = exc
            except Exception as exc:  # pylint: disable=broad-except  # timeout, refused, reset
                error = exc
            if attempt + 1 < self.attempts:
                time.sleep(self.wait_s * (attempt + 1))
        raise DepthsUnavailable(f"Backend water model not reachable at {url} "
                                f"after {self.attempts} attempts: {error}") from error

    def _checked(self, document: dict) -> np.ndarray:
        if document.get("status") != "success":
            raise DepthsUnavailable(document.get("message") or "The backend returned no depths.")
        if document.get("dataset_version") != self.run["dataset_version"] or document.get("signature") != self.signature:
            raise DepthsUnavailable("The backend's samples belong to a different published run; re-run the task.")
        values = np.array([np.nan if v is None else float(v) for v in document["depths_m"]], dtype=float)
        if len(values) != self.n_samples:
            raise DepthsUnavailable("The backend returned a different number of samples.")
        return values

    def at_stage(self, stage_cm: float) -> np.ndarray:
        return self._checked(self._get("/flood-costs/dynamic/depths.json", stage=f"{stage_cm:g}"))

    def at_key(self, key_text: str) -> np.ndarray:
        return self._checked(self._get("/flood-costs/dynamic/depths.json", key=key_text))

    def keyframes(self, start_iso: str, end_iso: str) -> dict:
        document = self._get("/flood-costs/dynamic/keyframes.json", start=start_iso, end=end_iso)
        if document.get("status") != "success":
            raise DepthsUnavailable(document.get("message") or "No gauge readings for that period.")
        return document
