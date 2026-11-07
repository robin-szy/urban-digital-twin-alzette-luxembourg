# -*- coding: utf-8 -*-
"""
Relative Elevation Model (REM) of the Alzette corridor, and the water of gauge levels (live, day
replay, manual) computed from it, drawn by terriajs/water-level-control.js like the flood scenarios.

Base surface
    The river's own surface in the LiDAR DEM. Every centreline point of flood_grid.centreline()
    (Alzette and tributaries, CENTRELINE_STEP_M apart) looks across the river, BASE_SEARCH_M to each
    side, for the lowest DEM cell, inside the river channel (water_surface.river_channel) where the
    transect crosses it. That lowest height, smoothed along the river (rolling median over
    BASE_SMOOTHING_M) and made never rising downstream (the closest such profile, least squares:
    a bridge deck does not lift the reach upstream of it), is the base surface; the position of the lowest cells is the channel point. A tributary starts no lower
    than the Alzette base at its confluence.
REM
    For every DEM cell of the corridor (water_cells(REM_CORRIDOR_M): Alzette and tributaries):
    REM = DEM height - base surface at the nearest channel point of its own river. Built once and
    cached in REM_CACHE_DIR (rem.tif: REM, base surface, nearest channel point), rebuilt only when
    the DEM changes (rem.json holds the DEM it was built from). The DEM is only read.
Water rise
    At each enabled gauge, the water rise above the base surface: the gauge level (gauge zero +
    stage, water_surface.rendered_gauge_wse) minus the base surface at the gauge's river km. The
    gauges outside the DEM take the base surface at the DEM edge, carried to them along the gauge
    zero profile. The rise is interpolated by river km between the enabled gauges and held constant
    beyond the outermost ones; at least MIN_RISE_M, so the channel the LiDAR recorded as ground
    stays filled at low stages. The tributaries take the rise at their confluence. Where the
    resulting surface would fall going upstream, it is held at the level downstream.
Water
    Wet = REM < local rise, connected (4-neighbour) to the river channel; water-surface elevation =
    base surface + local rise: level across the river, never rising downstream. Drawn with the
    grid mesh of flood_grid (a ring of cells under the terrain clips the shoreline), with the
    Manning velocity estimate and flow arrows of flood_grid.
REM view
    The REM as colour (COLOUR_LOW at the river surface, grading to COLOUR_HIGH at OVERLAY_MAX_M) with
    contour lines every CONTOUR_INTERVALS_M, precomputed once per interval (overlay_<m>m.tif in
    REM_CACHE_DIR) and served as web map tiles (tile_png).
"""

import functools
import io
import json
import math
import os
import pathlib
import threading

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio import features
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from scipy import ndimage
from scipy.spatial import cKDTree

from eddie_water_level.flood_grid import (
    CENTRELINE_STEP_M,
    LEVEL_CACHE_SIZE,
    MANNING_N,
    _mesh,
    _profile_slope,
    centreline,
    corridor,
    dem_grid,
)
from eddie_water_level.water_surface import (
    RIVER_NAME,
    TERRAIN_DEM,
    _GAUGE_KMS,
    _channel_lines,
    _is_stage,
    _never_rising_downstream,
    _wse_profile,
    rendered_gauge_wse,
    river_channel,
    water_cells,
)


# ============================================================
# CONFIGURATION
# ============================================================

# Where the REM and its overlays are cached (writable; git-ignored).
REM_CACHE_DIR = pathlib.Path(os.environ.get("REM_CACHE_DIR", "/app/data/rem"))

# Half-width in metres of the Alzette corridor the REM covers (the tributaries keep their own
# corridor, water_surface.TRIBUTARY_CORRIDOR_M). 700 m holds the HQextreme extent.
REM_CORRIDOR_M = float(os.environ.get("REM_CORRIDOR_M", "700"))

# Distance in metres searched on each side of a centreline point for the lowest DEM cell.
BASE_SEARCH_M = float(os.environ.get("REM_BASE_SEARCH_M", "30"))

# Length in metres of the rolling median along the river that smooths the base surface.
BASE_SMOOTHING_M = float(os.environ.get("REM_BASE_SMOOTHING_M", "15"))

# Smallest water rise above the base surface (m): the DEM has no river bed, so below this the
# channel would show its LiDAR surface instead of water.
MIN_RISE_M = float(os.environ.get("REM_MIN_RISE_M", "0.15"))

# Version of the cached REM, rebuilt when it changes.
REM_VERSION = 2

# Version of the REM level meshes, in the level urls, so browsers do not reuse an older mesh.
REM_FORMAT = "r1"

# REM view: colour at the river surface and at OVERLAY_MAX_M (REM above it is not coloured),
# opacity of the colour, contour colour, and the contour intervals offered (m).
COLOUR_LOW = (20, 90, 200)
COLOUR_HIGH = (250, 230, 120)
OVERLAY_MAX_M = float(os.environ.get("REM_OVERLAY_MAX_M", "20"))
OVERLAY_ALPHA = 200
CONTOUR_RGBA = (25, 25, 25, 255)
CONTOUR_INTERVALS_M = (1.0, 2.0, 5.0, 10.0)
DEFAULT_CONTOUR_M = 5.0

TILE_PX = 256
WEB_MERCATOR = rasterio.crs.CRS.from_epsg(3857)
_HALF_WORLD_M = math.pi * 6378137.0

_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()


# ============================================================
# BASE SURFACE
# ============================================================

def _lowest_across(heights: np.ndarray, offsets: np.ndarray, in_channel: np.ndarray) -> tuple:
    """Lowest height on one transect and the middle of the run of lowest cells nearest the centreline."""
    usable = np.isfinite(heights) & (in_channel if in_channel.any() else True)
    # At the DEM edge a transect half outside it can miss the channel.
    if not usable.any() or np.isfinite(heights).mean() < 0.5:
        return np.nan, np.nan
    lowest = np.min(heights[usable])
    low = usable & (heights <= lowest + 0.02)
    runs, count = ndimage.label(low)
    best = min(range(1, count + 1), key=lambda run: np.min(np.abs(offsets[runs == run])))
    return float(lowest), float(np.median(offsets[runs == best]))


def _isotonic(values: np.ndarray) -> np.ndarray:
    """
    The closest (least squares) profile that never falls (pool adjacent violators), for values
    ordered from downstream to upstream: never rising downstream, without a local bump (a bridge
    deck) lifting the whole reach upstream of it, or a dip lowering the whole reach below it.
    """
    means, weights, sizes = [], [], []
    for value in values:
        means.append(float(value))
        weights.append(1.0)
        sizes.append(1)
        while len(means) > 1 and means[-2] > means[-1]:
            weight = weights[-2] + weights[-1]
            means[-2:] = [(means[-2] * weights[-2] + means[-1] * weights[-1]) / weight]
            weights[-2:] = [weight]
            sizes[-2:] = [sizes[-2] + sizes[-1]]
    return np.repeat(means, sizes)


def _base_profile() -> pd.DataFrame:
    """
    The base surface at every centreline() point (see the module docstring).

    Returns
    -------
    pd.DataFrame
        centreline() plus base (NG-L, NaN outside the DEM) and cx, cy (the channel point, grid CRS).
    """
    points = centreline().copy()
    channel = gpd.GeoSeries([river_channel()], crs=2169).to_crs(dem_grid()["crs"]).iloc[0]
    with rasterio.open(TERRAIN_DEM) as dem:
        heights = dem.read(1, masked=True).astype("float32").filled(np.nan)
        transform = dem.transform
        channel_cells = features.rasterize([(channel, 1)], out_shape=heights.shape, transform=transform,
                                           fill=0, dtype="uint8", all_touched=True).astype(bool)
    step = abs(transform.a)
    offsets = np.arange(-BASE_SEARCH_M, BASE_SEARCH_M + step / 2, step)
    # Across the river: left of the downstream direction.
    nx, ny = -points["dy"].to_numpy(), points["dx"].to_numpy()
    xs = points["x"].to_numpy()[:, None] + offsets[None, :] * nx[:, None]
    ys = points["y"].to_numpy()[:, None] + offsets[None, :] * ny[:, None]
    cols = np.floor((xs - transform.c) / transform.a).astype(int)
    rows = np.floor((ys - transform.f) / transform.e).astype(int)
    on = (rows >= 0) & (rows < heights.shape[0]) & (cols >= 0) & (cols < heights.shape[1])
    sampled = np.where(on, heights[np.clip(rows, 0, heights.shape[0] - 1), np.clip(cols, 0, heights.shape[1] - 1)], np.nan)
    in_channel = on & channel_cells[np.clip(rows, 0, heights.shape[0] - 1), np.clip(cols, 0, heights.shape[1] - 1)]
    del heights

    lowest, offset = np.full(len(points), np.nan), np.full(len(points), np.nan)
    for index in range(len(points)):
        lowest[index], offset[index] = _lowest_across(sampled[index], offsets, in_channel[index])

    window = max(int(round(BASE_SMOOTHING_M / CENTRELINE_STEP_M)) | 1, 1)
    base = np.full(len(points), np.nan)
    shift = np.zeros(len(points))
    alzette_base = None
    for river in dict.fromkeys(points["river"]):
        rows_r = np.flatnonzero((points["river"] == river).to_numpy())
        smooth = pd.Series(lowest[rows_r]).rolling(window, center=True, min_periods=1).median().to_numpy()
        smooth[np.isnan(lowest[rows_r])] = np.nan
        known = np.isfinite(smooth)
        shift[rows_r] = pd.Series(offset[rows_r]).rolling(window, center=True, min_periods=1).median().fillna(0.0).to_numpy()
        if not known.any():
            continue
        kms = points["river_km"].to_numpy()[rows_r]
        start = -np.inf
        if river != RIVER_NAME and alzette_base is not None:
            # A tributary lies nowhere lower than the Alzette at its confluence.
            start = np.interp(points["confluence_m"].to_numpy()[rows_r][0] / 1000.0, *alzette_base)
        filled = smooth.copy()
        order = np.argsort(kms[known], kind="stable")
        rising = np.empty(order.size)
        rising[order] = _isotonic(smooth[known][order])
        filled[known] = np.maximum(rising, start)
        base[rows_r] = filled
        if river == RIVER_NAME:
            order = np.argsort(kms[known])
            alzette_base = (kms[known][order], filled[known][order])
    points["base"] = base
    points["cx"] = points["x"].to_numpy() + shift * nx
    points["cy"] = points["y"].to_numpy() + shift * ny
    return points


# ============================================================
# REM (CACHED)
# ============================================================

def _dem_stamp() -> dict:
    stat = TERRAIN_DEM.stat()
    return {"dem": str(TERRAIN_DEM), "size": stat.st_size, "mtime": int(stat.st_mtime), "version": REM_VERSION,
            "corridor_m": REM_CORRIDOR_M, "search_m": BASE_SEARCH_M, "smoothing_m": BASE_SMOOTHING_M}


def _cache_valid() -> bool:
    stamp_file = REM_CACHE_DIR / "rem.json"
    if not (REM_CACHE_DIR / "rem.tif").is_file() or not (REM_CACHE_DIR / "base.csv").is_file() or not stamp_file.is_file():
        return False
    try:
        return json.loads(stamp_file.read_text())["stamp"] == _dem_stamp()
    except (ValueError, KeyError):
        return False


def build(force: bool = False) -> pathlib.Path:
    """
    Build the REM into REM_CACHE_DIR (rem.tif, base.csv, rem.json and the overlays), unless it is
    already built from the current DEM.

    Returns
    -------
    pathlib.Path
        rem.tif.
    """
    if not TERRAIN_DEM.is_file():
        raise FileNotFoundError(f"Terrain DEM not found: {TERRAIN_DEM}")
    with _BUILD_LOCK:
        if not force and _cache_valid():
            return REM_CACHE_DIR / "rem.tif"
        REM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        profile = _base_profile()
        points = profile[["x", "y"]].to_numpy()
        channel_xy = profile[["cx", "cy"]].to_numpy()
        rivers = list(dict.fromkeys(profile["river"]))

        with rasterio.open(TERRAIN_DEM) as dem:
            meta = dem.meta.copy()
            transform, shape, crs = dem.transform, dem.shape, dem.crs
            cells = water_cells(REM_CORRIDOR_M).to_crs(crs)
            river_id = features.rasterize(
                ((geometry, rivers.index(river) + 1) for river, geometry in zip(cells["river"], cells.geometry)
                 if river in rivers),
                out_shape=shape, transform=transform, fill=0, dtype="uint8",
            )
            trees = {}
            for index, river in enumerate(rivers):
                own = np.flatnonzero((profile["river"] == river).to_numpy() & np.isfinite(profile["base"].to_numpy()))
                if own.size:
                    trees[index + 1] = (own, cKDTree(channel_xy[own]))
            meta.update(count=3, dtype="float32", nodata=np.nan, compress="deflate", predictor=3, tiled=True,
                        blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
            target = REM_CACHE_DIR / "rem.tmp.tif"
            with rasterio.open(target, "w", **meta) as out:
                out.set_band_description(1, "REM (m above the base surface)")
                out.set_band_description(2, "Base surface at the nearest channel point (m NG-L)")
                out.set_band_description(3, "Nearest channel point (index in flood_grid.centreline)")
                band = 512
                for start in range(0, shape[0], band):
                    count = min(band, shape[0] - start)
                    window = rasterio.windows.Window(0, start, shape[1], count)
                    heights = dem.read(1, window=window, masked=True).astype("float32").filled(np.nan)
                    ids = river_id[start: start + count]
                    rem = np.full(heights.shape, np.nan, dtype="float32")
                    base = np.full(heights.shape, np.nan, dtype="float32")
                    nearest = np.full(heights.shape, np.nan, dtype="float32")
                    for number, (own, tree) in trees.items():
                        rows, cols = np.nonzero((ids == number) & np.isfinite(heights))
                        if rows.size == 0:
                            continue
                        x = transform.c + (cols + 0.5) * transform.a
                        y = transform.f + (start + rows + 0.5) * transform.e
                        _, found = tree.query(np.column_stack([x, y]))
                        index = own[found]
                        base[rows, cols] = profile["base"].to_numpy()[index]
                        nearest[rows, cols] = index
                        rem[rows, cols] = heights[rows, cols] - base[rows, cols]
                    out.write(rem, 1, window=window)
                    out.write(base, 2, window=window)
                    out.write(nearest, 3, window=window)
        os.replace(target, REM_CACHE_DIR / "rem.tif")
        profile[["river", "river_km", "s_m", "x", "y", "cx", "cy", "base"]].to_csv(REM_CACHE_DIR / "base.csv", index=False)
        for interval in CONTOUR_INTERVALS_M:
            _write_overlay(interval)
        (REM_CACHE_DIR / "rem.json").write_text(json.dumps({"stamp": _dem_stamp(), "points": len(points)}, indent=1))
        _clear_caches()
        return REM_CACHE_DIR / "rem.tif"


def _clear_caches():
    for cached in (rem_grid, base_profile, overlay, _level):
        cached.cache_clear()


@functools.lru_cache(maxsize=1)
def base_profile() -> pd.DataFrame:
    """The cached base surface: one row per centreline() point (river, river_km, s_m, x, y, cx, cy, base)."""
    build()
    return pd.read_csv(REM_CACHE_DIR / "base.csv")


@functools.lru_cache(maxsize=1)
def rem_grid() -> dict:
    """
    The REM on the computation grid (flood_grid.dem_grid): rem (block mean of the REM cells, m),
    nearest (centreline() index of the nearest channel point, -1 outside the corridor) and channel
    (cells holding a channel point, or of the river channel, inside the corridor).
    """
    path = build()
    grid = dem_grid()
    rows, cols = grid["shape"]
    with rasterio.open(path) as source:
        factor = max(int(round(abs(grid["transform"].a) / source.res[0])), 1)
        rem = np.full((rows, cols), np.nan, dtype="float32")
        nearest = np.full((rows, cols), -1, dtype="int32")
        band = 500
        for start in range(0, rows, band):
            count = min(band, rows - start)
            window = rasterio.windows.Window(0, start * factor, cols * factor, count * factor)
            blocks = source.read(1, window=window).reshape(count, factor, cols, factor)
            valid = np.isfinite(blocks).sum(axis=(1, 3))
            with np.errstate(invalid="ignore"):
                rem[start: start + count] = np.where(valid > 0, np.nansum(blocks, axis=(1, 3)) / np.maximum(valid, 1), np.nan)
            index = source.read(3, window=window)[::factor, ::factor]
            nearest[start: start + count] = np.where(np.isfinite(index), index, -1).astype("int32")
    nearest[~np.isfinite(rem)] = -1
    profile = base_profile()
    transform = grid["transform"]
    point_cols = np.floor((profile["cx"].to_numpy() - transform.c) / transform.a).astype(int)
    point_rows = np.floor((profile["cy"].to_numpy() - transform.f) / transform.e).astype(int)
    on = (point_rows >= 0) & (point_rows < rows) & (point_cols >= 0) & (point_cols < cols)
    channel = corridor(None)["channel"].copy()
    channel[point_rows[on], point_cols[on]] = True
    channel &= nearest >= 0
    return {"rem": rem, "nearest": nearest, "channel": channel}


# ============================================================
# WATER OF A GAUGE LEVEL
# ============================================================

def _alzette_base_at(kms: np.ndarray) -> np.ndarray:
    """Base surface on the Alzette at river kms; beyond the DEM carried along the gauge zero profile."""
    profile = base_profile()
    alzette = profile[(profile["river"] == RIVER_NAME) & profile["base"].notna()].sort_values("river_km")
    known_km, known_base = alzette["river_km"].to_numpy(), alzette["base"].to_numpy()
    kms = np.asarray(kms, dtype="float64")
    base = np.interp(kms, known_km, known_base)
    zero = _wse_profile(kms, 0.0, 0.0, 0.0)
    low, high = kms < known_km[0], kms > known_km[-1]
    base[low] = known_base[0] + zero[low] - _wse_profile(known_km[:1], 0.0, 0.0, 0.0)[0]
    base[high] = known_base[-1] + zero[high] - _wse_profile(known_km[-1:], 0.0, 0.0, 0.0)[0]
    return base


def gauge_rises(key) -> list:
    """(river km, rise above the base surface in m) at each enabled gauge of a level key (pf, he, li)."""
    rises = []
    for km, stage in zip(_GAUGE_KMS, key):
        if not _is_stage(stage):
            continue
        stages = [None, None, None]
        stages[list(_GAUGE_KMS).index(km)] = stage
        level = rendered_gauge_wse(_wse_profile(np.array([km]), *stages))[0]
        rises.append((float(km), float(level - _alzette_base_at([km])[0])))
    if not rises:
        raise ValueError("No gauge stage given.")
    return rises


def centreline_rise(key) -> np.ndarray:
    """Water rise above the base surface at every centreline() point for a gauge level key."""
    rises = gauge_rises(key)
    points = centreline()
    kms = points["river_km"].to_numpy().copy()
    tributary = (points["river"] != RIVER_NAME).to_numpy()
    kms[tributary] = points["confluence_m"].to_numpy()[tributary] / 1000.0
    # np.interp holds the rise of the outermost gauges beyond them.
    rise = np.maximum(np.interp(kms, [km for km, _ in rises], [value for _, value in rises]), MIN_RISE_M)
    # The water surface (base + rise) never rises downstream, as every gauge profile
    # (water_surface._never_rising_downstream): where the rise falls faster upstream than the base
    # climbs (a flat reach), the water is held at the level downstream. A tributary's water is never
    # below the Alzette's at its confluence.
    base = base_profile()["base"].to_numpy()
    river_km = points["river_km"].to_numpy()
    alzette_wse = None
    for river in dict.fromkeys(points["river"]):
        mine = (points["river"] == river).to_numpy() & np.isfinite(base)
        if not mine.any():
            continue
        wse = base[mine] + rise[mine]
        if river != RIVER_NAME and alzette_wse is not None:
            wse = np.maximum(wse, np.interp(points["confluence_m"].to_numpy()[mine][0] / 1000.0, *alzette_wse))
        wse = _never_rising_downstream(river_km[mine], wse)
        rise[mine] = wse - base[mine]
        if river == RIVER_NAME:
            order = np.argsort(river_km[mine])
            alzette_wse = (river_km[mine][order], wse[order])
    return rise


def compute_level(key) -> dict:
    """
    The water of one gauge level (see the module docstring), as flood_grid.compute_level returns it.

    Returns
    -------
    dict
        wet, surface (NG-L), depth, speed (m/s, NaN where dry), disconnected (cells under the
        local rise not connected to the channel), centreline_wse and slope (per centreline point).
    """
    grid = dem_grid()
    remg = rem_grid()
    nearest, channel = remg["nearest"], remg["channel"]
    inside = nearest >= 0
    rise = centreline_rise(key)
    base = base_profile()["base"].to_numpy()
    point_wse = base + rise

    local_rise = np.full(grid["shape"], np.nan, dtype="float32")
    local_rise[inside] = rise[nearest[inside]]
    with np.errstate(invalid="ignore"):
        below = inside & (remg["rem"] < local_rise)
    labels, count = ndimage.label(below)
    connected = np.zeros(count + 1, dtype=bool)
    connected[np.unique(labels[channel & below])] = True
    connected[0] = False
    wet = connected[labels]

    # Base surface of each cell as its REM has it (ground - REM: at a step of the base, the mean of its
    # DEM cells' bases), so every wet cell has its surface above its ground.
    surface = np.full(grid["shape"], np.nan, dtype="float32")
    surface[inside] = grid["mean"][inside] - remg["rem"][inside] + local_rise[inside]
    depth = np.where(wet, np.fmax(surface - grid["mean"], 0.0), np.nan).astype("float32")
    slope = _profile_slope(point_wse)
    speed = np.full(grid["shape"], np.nan, dtype="float32")
    speed[wet] = (depth[wet] ** (2.0 / 3.0)) * np.sqrt(slope[nearest[wet]]) / MANNING_N
    return {
        "wet": wet, "surface": surface, "depth": depth, "speed": speed,
        "disconnected": below & ~wet, "centreline_wse": point_wse, "slope": slope,
    }


def _area() -> dict:
    """The corridor of the REM water, for flood_grid._mesh (nearest, channel, arrow cells)."""
    remg = rem_grid()
    return {"nearest": remg["nearest"], "channel": remg["channel"], "extent": None,
            "arrow_cells": corridor(None)["arrow_cells"]}


@functools.lru_cache(maxsize=LEVEL_CACHE_SIZE)
def _level(key) -> dict:
    with _LOCK:
        computed = compute_level(key)
        return {"wet": np.packbits(computed["wet"]), "mesh": _mesh(key, computed, _area())}


def wet_mask(key) -> np.ndarray:
    """The computation-grid cells under water for a gauge level key (bool)."""
    grid = dem_grid()
    count = grid["shape"][0] * grid["shape"][1]
    return np.unpackbits(_level(key)["wet"], count=count).astype(bool).reshape(grid["shape"])


def level_mesh(key) -> dict:
    """The drawn water of a gauge level key (flood_grid mesh format)."""
    return _level(key)["mesh"]


# ============================================================
# REM VIEW
# ============================================================

def _overlay_path(interval: float) -> pathlib.Path:
    return REM_CACHE_DIR / f"overlay_{interval:g}m.tif"


def _write_overlay(interval: float) -> None:
    """The REM view for one contour interval on the computation grid: colour (RGBA) and contour mask."""
    grid = dem_grid()
    rem = _rem_grid_uncached()
    shown = np.isfinite(rem) & (rem <= OVERLAY_MAX_M)
    fraction = np.clip(np.nan_to_num(rem, nan=0.0) / OVERLAY_MAX_M, 0.0, 1.0)
    bands = np.zeros((5,) + rem.shape, dtype="uint8")
    for band in range(3):
        bands[band] = np.round(COLOUR_LOW[band] + fraction * (COLOUR_HIGH[band] - COLOUR_LOW[band])).astype("uint8")
    bands[3] = np.where(shown, OVERLAY_ALPHA, 0)
    # Contours: the cells where the REM crosses a multiple of the interval to the next cell.
    with np.errstate(invalid="ignore"):
        step = np.where(np.isfinite(rem), np.floor(rem / interval), np.nan)
    line = np.zeros(rem.shape, dtype=bool)
    line[:, :-1] |= step[:, :-1] != step[:, 1:]
    line[:-1, :] |= step[:-1, :] != step[1:, :]
    finite = np.isfinite(step)
    edge = np.zeros(rem.shape, dtype=bool)
    edge[:, :-1] |= ~(finite[:, :-1] & finite[:, 1:])
    edge[:-1, :] |= ~(finite[:-1, :] & finite[1:, :])
    bands[4] = np.where(line & ~edge & finite & (np.nan_to_num(rem, nan=np.inf) <= OVERLAY_MAX_M + interval), 255, 0)
    meta = {"driver": "GTiff", "width": rem.shape[1], "height": rem.shape[0], "count": 5, "dtype": "uint8",
            "crs": grid["crs"], "transform": grid["transform"], "compress": "deflate", "tiled": True}
    with rasterio.open(_overlay_path(interval), "w", **meta) as out:
        out.write(bands)


def _rem_grid_uncached() -> np.ndarray:
    grid = dem_grid()
    rows, cols = grid["shape"]
    with rasterio.open(REM_CACHE_DIR / "rem.tif") as source:
        factor = max(int(round(abs(grid["transform"].a) / source.res[0])), 1)
        blocks = source.read(1)[: rows * factor, : cols * factor].reshape(rows, factor, cols, factor)
    valid = np.isfinite(blocks).sum(axis=(1, 3))
    with np.errstate(invalid="ignore"):
        return np.where(valid > 0, np.nansum(blocks, axis=(1, 3)) / np.maximum(valid, 1), np.nan).astype("float32")


def parse_interval(text) -> float:
    """A contour interval in metres; one of CONTOUR_INTERVALS_M, else ValueError."""
    value = float(text)
    if value not in CONTOUR_INTERVALS_M:
        raise ValueError(f"Contour interval must be one of {', '.join(f'{v:g}' for v in CONTOUR_INTERVALS_M)} m.")
    return value


@functools.lru_cache(maxsize=len(CONTOUR_INTERVALS_M))
def overlay(interval: float) -> np.ndarray:
    """The precomputed REM view of an interval: (5, rows, cols) uint8, RGBA colour and contour mask."""
    build()
    path = _overlay_path(interval)
    if not path.is_file():
        with _BUILD_LOCK:
            _write_overlay(interval)
    with rasterio.open(path) as source:
        return source.read()


@functools.lru_cache(maxsize=1)
def bounds_wgs84() -> list:
    """[west, south, east, north] of the REM view in degrees."""
    grid = dem_grid()
    rows, cols = grid["shape"]
    transform = grid["transform"]
    left, top = transform.c, transform.f
    return list(transform_bounds(grid["crs"], "EPSG:4326", left, top + rows * transform.e, left + cols * transform.a, top))


@functools.lru_cache(maxsize=1)
def _empty_tile() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (TILE_PX, TILE_PX), (0, 0, 0, 0)).save(output, format="PNG")
    return output.getvalue()


@functools.lru_cache(maxsize=4096)
def tile_png(interval: float, z: int, x: int, y: int) -> bytes:
    """One web map tile (XYZ, EPSG:3857) of the REM view with contours every `interval` m, as PNG."""
    size = 2.0 * _HALF_WORLD_M / (2 ** z)
    left, top = -_HALF_WORLD_M + x * size, _HALF_WORLD_M - y * size
    west, south, east, north = bounds_wgs84()
    min_x, min_y, max_x, max_y = transform_bounds("EPSG:4326", WEB_MERCATOR, west, south, east, north)
    if left > max_x or left + size < min_x or top < min_y or top - size > max_y:
        return _empty_tile()
    grid = dem_grid()
    bands = overlay(interval)
    destination = np.zeros((5, TILE_PX, TILE_PX), dtype="uint8")
    dst_transform = from_origin(left, top, size / TILE_PX, size / TILE_PX)
    for band in range(5):
        reproject(
            source=bands[band], destination=destination[band],
            src_transform=grid["transform"], src_crs=grid["crs"], src_nodata=None,
            dst_transform=dst_transform, dst_crs=WEB_MERCATOR, dst_nodata=None,
            # Contours: the highest value, so the lines stay visible when zoomed out.
            resampling=Resampling.max if band == 4 and size / TILE_PX > abs(grid["transform"].a) else Resampling.nearest,
        )
    if not destination[3].any() and not destination[4].any():
        return _empty_tile()
    line = destination[4] > 0
    tile = np.moveaxis(destination[:4], 0, -1).copy()
    tile[line] = CONTOUR_RGBA
    output = io.BytesIO()
    Image.fromarray(tile, mode="RGBA").save(output, format="PNG", optimize=False)
    return output.getvalue()
