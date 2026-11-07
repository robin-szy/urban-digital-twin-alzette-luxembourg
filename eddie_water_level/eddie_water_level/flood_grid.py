# -*- coding: utf-8 -*-
"""
Continuous water surface of the Alzette on a grid over the LiDAR DEM, with an estimated flow
velocity and the flow direction, rendered in Terria by terriajs/water-level-control.js.

Method (height above nearest drainage)
--------------------------------------
Water-surface profile
    The water-surface elevation (WSE) at every point of the river centreline (the kilometrage
    line, sampled every CENTRELINE_STEP_M): gauge zero + stage, interpolated linearly by river km
    between the gauges and never rising downstream (water_surface.wse_for_levels), or the profile
    of a flood scenario (water_surface.scenario_wse). The tributaries have no gauges: their
    centreline takes the level at their confluence, as before.
Lateral extension
    Every grid cell of the river corridor (water_surface.water_cells) takes the WSE of the
    nearest point of its own river's centreline.
Depth
    depth = WSE - DEM. Water only where depth > 0, so islands, banks and embankments stay dry,
    and only where it is connected to the river channel (4-neighbour flood fill from the
    channel), so isolated low areas do not flood. For a flood scenario, also only inside its
    official extent.
Channel
    Inside the river channel (water_surface.river_channel) the water is always there, and at
    least CHANNEL_MARGIN_M above the highest DEM point under it (the displayed terrain), so weirs,
    bridges and the river surface the LiDAR recorded as ground do not hide it. Only there.
Velocity (estimate)
    Manning: v = h^(2/3) * S^(1/2) / n, with h the depth of the cell (wide channel: hydraulic
    radius = depth; in the channel at least CHANNEL_MARGIN_M), S the water-surface slope along the
    river at its nearest centreline point, taken over SLOPE_SMOOTHING_M, and n = MANNING_N.
    S is the slope of the WSE profile on the Alzette; on the tributaries, whose WSE is held at the
    confluence level, that of the water drawn in their channel. The DEM has no river bed (the
    LiDAR stops at the water surface), so in the channel the depth, and the velocity, are low.
Flow direction
    Downstream along the centreline (decreasing river km).

Gauge stages are rounded to WATER_LEVEL_STEP_CM. Each rounded level is computed once and kept
(LEVEL_CACHE_SIZE levels), so the map switches between precomputed levels.

The grid covers the DEM only (TERRAIN_DEM): outside it there is no terrain to compare the
water with, and the map terrain is flat there.
"""

import base64
import functools
import os
import threading

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from affine import Affine
from rasterio import features
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiPolygon, Point, shape
from shapely.ops import unary_union

from eddie_water_level.water_surface import (
    CHANNEL_MARGIN_M,
    DEFAULT_CORRIDOR_M,
    RIVER_NAME,
    SCENARIOS,
    TERRAIN_DEM,
    TERRAIN_HEIGHT_OFFSET_M,
    _channel_lines,
    _never_rising_downstream,
    _wse_profile,
    flood_extent,
    rendered_gauge_wse,
    river_channel,
    scenario_wse,
    terrain_height_offset,
    water_cells,
)


# ============================================================
# CONFIGURATION
# ============================================================

# Cell size in metres of the grid the water is computed on (the DEM is 0.5 m).
WATER_GRID_M = float(os.environ.get("WATER_GRID_M", "1"))

# Cell size in metres of the mesh drawn in Terria (a multiple of WATER_GRID_M).
WATER_MESH_M = float(os.environ.get("WATER_MESH_M", "2"))

# Gauge stages are rounded to this step (cm); each rounded level is computed once.
WATER_LEVEL_STEP_CM = float(os.environ.get("WATER_LEVEL_STEP_CM", "10"))

# Number of computed levels kept in memory.
LEVEL_CACHE_SIZE = int(os.environ.get("WATER_LEVEL_CACHE", "64"))

# How the drawn water, building colours and flood reports are computed: "sections" (tilted sections
# along the river, eddie_water_level.flood_sections, the default) or "grid" (this module).
WATER_SURFACE_METHOD = os.environ.get("WATER_SURFACE_METHOD", "sections")

# How the water of gauge levels (live, day replay, manual) is computed: "rem" (from the Relative
# Elevation Model, eddie_water_level.rem, the default) or "sections" (WATER_SURFACE_METHOD, as the flood
# scenarios: the tilted sections by default). The flood scenarios always use WATER_SURFACE_METHOD.
LIVE_WATER_METHOD = os.environ.get("LIVE_WATER_METHOD", "rem").strip().lower()


def uses_rem(key) -> bool:
    """True when a level key is drawn from the REM (a gauge level, with LIVE_WATER_METHOD=rem)."""
    return LIVE_WATER_METHOD == "rem" and not isinstance(key, str)


# Spacing of the centreline points whose WSE the grid cells take.
CENTRELINE_STEP_M = 5.0

# Manning roughness coefficient (s/m^(1/3)) of the velocity estimate.
MANNING_N = float(os.environ.get("MANNING_N", "0.035"))

# Distance in metres along the river over which the water-surface slope is taken.
SLOPE_SMOOTHING_M = float(os.environ.get("SLOPE_SMOOTHING_M", "300"))

# Smallest slope used, so flat stretches (backwater) get a small velocity instead of none.
MIN_SLOPE = 1e-5

# Flow arrows along the river: spacing in metres of each level of detail (fewer when zoomed out).
ARROW_SPACING_M = (50.0, 200.0, 800.0)

# A centreline point gets an arrow on the channel cell closest to it, if one is this close.
ARROW_SEARCH_M = 40.0

# Version of the level mesh format, in the level urls, so browsers do not reuse an older one.
MESH_FORMAT = 2

_LOCK = threading.Lock()


# ============================================================
# STATIC GRIDS
# ============================================================

@functools.lru_cache(maxsize=1)
def dem_grid() -> dict:
    """
    The DEM on the computation grid.

    Returns
    -------
    dict
        transform, crs, shape, mean (mean DEM height per cell, the ground) and top (highest DEM
        height per cell, the displayed terrain), float32 with NaN outside the DEM.
    """
    if not TERRAIN_DEM.is_file():
        raise FileNotFoundError(f"Terrain DEM not found: {TERRAIN_DEM}")
    with rasterio.open(TERRAIN_DEM) as dem:
        factor = max(int(round(WATER_GRID_M / dem.res[0])), 1)
        rows, cols = dem.height // factor, dem.width // factor
        grids = {"mean": np.full((rows, cols), np.nan, dtype="float32"), "top": np.full((rows, cols), np.nan, dtype="float32")}
        # A band of rows at a time, to keep the memory low.
        band = 500
        for start in range(0, rows, band):
            count = min(band, rows - start)
            window = rasterio.windows.Window(0, start * factor, cols * factor, count * factor)
            heights = dem.read(1, window=window, masked=True).astype("float32").filled(np.nan)
            blocks = heights.reshape(count, factor, cols, factor)
            valid = np.isfinite(blocks).sum(axis=(1, 3))
            with np.errstate(invalid="ignore"):
                grids["mean"][start: start + count] = np.where(valid > 0, np.nansum(blocks, axis=(1, 3)) / np.maximum(valid, 1), np.nan)
                grids["top"][start: start + count] = np.where(valid > 0, np.fmax.reduce(np.fmax.reduce(blocks, axis=3), axis=1), np.nan)
        transform = dem.transform * Affine.scale(factor, factor)
        crs = dem.crs
    return {"transform": transform, "crs": crs, "shape": (rows, cols), **grids}


def _cell_centres(rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    transform = dem_grid()["transform"]
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    return np.column_stack([x, y])


def _rasterize(geometries, fill=0, dtype="uint8", all_touched=False) -> np.ndarray:
    grid = dem_grid()
    shapes = [(geometry, value) for geometry, value in geometries if geometry is not None and not geometry.is_empty]
    if not shapes:
        return np.full(grid["shape"], fill, dtype=dtype)
    return features.rasterize(
        shapes, out_shape=grid["shape"], transform=grid["transform"], fill=fill, dtype=dtype, all_touched=all_touched,
    )


@functools.lru_cache(maxsize=1)
def centreline() -> pd.DataFrame:
    """
    Points every CENTRELINE_STEP_M along the centreline of the Alzette and of its tributaries.

    Returns
    -------
    pd.DataFrame
        river, river_km (along its own river), confluence_m (Alzette chainage of a tributary's
        confluence, NaN for the Alzette), s_m (distance along the line from its downstream end),
        x, y (grid CRS) and dx, dy (unit vector pointing downstream).
    """
    crs = dem_grid()["crs"]
    parts = []
    for river, confluence, kms, points in _channel_lines():
        line = gpd.GeoSeries([LineString(points)], crs=2169).to_crs(crs).iloc[0]
        markers = np.array([line.project(Point(point)) for point in gpd.GeoSeries(points, crs=2169).to_crs(crs)])
        count = max(int(np.ceil(line.length / CENTRELINE_STEP_M)), 1)
        distances = np.linspace(0.0, line.length, count + 1)
        xy = np.array([line.interpolate(d).coords[0][:2] for d in distances])
        # Downstream is towards the start of the line (lower river km).
        behind = np.array([line.interpolate(max(d - CENTRELINE_STEP_M, 0.0)).coords[0][:2] for d in distances])
        ahead = np.array([line.interpolate(min(d + CENTRELINE_STEP_M, line.length)).coords[0][:2] for d in distances])
        direction = behind - ahead
        direction /= np.maximum(np.hypot(direction[:, 0], direction[:, 1]), 1e-9)[:, None]
        parts.append(pd.DataFrame({
            "river": river,
            "river_km": np.interp(distances, markers, kms),
            "confluence_m": np.nan if confluence is None else float(confluence),
            "s_m": distances,
            "x": xy[:, 0], "y": xy[:, 1],
            "dx": direction[:, 0], "dy": direction[:, 1],
        }))
    return pd.concat(parts, ignore_index=True)


@functools.lru_cache(maxsize=len(SCENARIOS) + 1)
def corridor(scenario: str | None = None) -> dict:
    """
    The cells that may hold water, and the centreline point each of them takes its level from.

    Parameters
    ----------
    scenario : str | None
        Flood scenario whose official extent widens the corridor (see water_surface.water_cells)
        and bounds the water.

    Returns
    -------
    dict
        nearest (index in centreline() of the nearest point of the cell's own river, -1 outside
        the corridor or the DEM), channel (cells of the river channel), extent (cells inside the
        official extent, None without a scenario) and arrow_cells (per centreline point, the flat
        index of the closest channel cell, -1 when none is within ARROW_SEARCH_M).
    """
    grid = dem_grid()
    points = centreline()
    rivers = list(dict.fromkeys(points["river"]))
    cells = water_cells(DEFAULT_CORRIDOR_M, scenario).to_crs(grid["crs"])
    river_id = _rasterize(
        ((geometry, rivers.index(river) + 1) for river, geometry in zip(cells["river"], cells.geometry) if river in rivers),
    )
    river_id[~np.isfinite(grid["mean"])] = 0

    nearest = np.full(grid["shape"], -1, dtype="int32")
    for index, river in enumerate(rivers):
        rows, cols = np.nonzero(river_id == index + 1)
        if rows.size == 0:
            continue
        own = np.flatnonzero((points["river"] == river).to_numpy())
        _, found = cKDTree(points[["x", "y"]].to_numpy()[own]).query(_cell_centres(rows, cols))
        nearest[rows, cols] = own[found]

    channel_shape = gpd.GeoSeries([river_channel()], crs=2169).to_crs(grid["crs"]).iloc[0]
    channel = _rasterize([(channel_shape, 1)], all_touched=True).astype(bool) & (nearest >= 0)

    extent = None
    if scenario is not None:
        extent_shape = gpd.GeoSeries([flood_extent(scenario)], crs=2169).to_crs(grid["crs"]).iloc[0]
        extent = _rasterize([(extent_shape, 1)]).astype(bool)

    arrow_cells = np.full(len(points), -1, dtype="int64")
    rows, cols = np.nonzero(channel)
    if rows.size:
        flat = rows.astype("int64") * grid["shape"][1] + cols
        owner = nearest[rows, cols]
        for river in rivers:
            own_cells = np.flatnonzero(points["river"].to_numpy()[owner] == river)
            if own_cells.size == 0:
                continue
            own_points = np.flatnonzero((points["river"] == river).to_numpy())
            distance, found = cKDTree(_cell_centres(rows[own_cells], cols[own_cells])).query(
                points[["x", "y"]].to_numpy()[own_points]
            )
            close = distance <= ARROW_SEARCH_M
            arrow_cells[own_points[close]] = flat[own_cells[found[close]]]
    return {"nearest": nearest, "channel": channel, "extent": extent, "arrow_cells": arrow_cells}


# ============================================================
# LEVELS
# ============================================================

def _rounded(stage):
    if stage is None or (isinstance(stage, float) and np.isnan(stage)):
        return None
    value = round(float(stage) / WATER_LEVEL_STEP_CM) * WATER_LEVEL_STEP_CM
    return int(value) if float(value).is_integer() else round(value, 3)


def level_key(pfaffenthal_cm, hesperange_cm, livange_cm=None):
    """
    The precomputed level shown for a keyframe: the flood scenario name, or the gauge stages
    rounded to WATER_LEVEL_STEP_CM as (pfaffenthal, hesperange, livange or None).
    """
    if isinstance(pfaffenthal_cm, str):
        return pfaffenthal_cm
    return (_rounded(pfaffenthal_cm), _rounded(hesperange_cm), _rounded(livange_cm))


def key_text(key) -> str:
    """A level key as text for URLs: 'hq100' or 'g120_140_90' ('x' for a missing gauge)."""
    if isinstance(key, str):
        return key
    return "g" + "_".join("x" if value is None else f"{value:g}" for value in key)


def parse_key(text: str):
    """Inverse of key_text. Raises ValueError for an invalid key."""
    if text in SCENARIOS:
        return text
    parts = text[1:].split("_") if text.startswith("g") else []
    if len(parts) != 3:
        raise ValueError(f"Invalid water level {text!r}.")
    values = [None if part == "x" else float(part) for part in parts]
    if values[0] is None and values[1] is None and values[2] is None:
        raise ValueError(f"Invalid water level {text!r}.")
    return level_key(*values)


def centreline_wse(key) -> np.ndarray:
    """WSE (NG-L, as drawn) at every centreline() point for a level key."""
    points = centreline()
    alzette = (points["river"] == RIVER_NAME).to_numpy()
    kms = points["river_km"].to_numpy()[alzette]
    if isinstance(key, str):
        profile = scenario_wse(key)
        chainages = np.array(sorted(profile), dtype="float64")
        wse = np.interp(kms * 1000.0, chainages, [profile[c] for c in chainages])
    else:
        wse = _never_rising_downstream(kms, rendered_gauge_wse(_wse_profile(kms, *key)))
    result = np.full(len(points), np.nan)
    result[alzette] = wse
    order = np.argsort(kms)
    tributaries = ~alzette
    result[tributaries] = np.interp(
        points["confluence_m"].to_numpy()[tributaries] / 1000.0, kms[order], wse[order]
    )
    return result


def _profile_slope(levels: np.ndarray) -> np.ndarray:
    """Water-surface slope at every centreline point: fall over SLOPE_SMOOTHING_M along its river."""
    points = centreline()
    slope = np.full(len(points), MIN_SLOPE)
    half = SLOPE_SMOOTHING_M / 2.0
    for river in points["river"].unique():
        rows = np.flatnonzero((points["river"] == river).to_numpy())
        s = points["s_m"].to_numpy()[rows]
        z = levels[rows]
        known = np.isfinite(z)
        if known.sum() < 2:
            continue
        z = np.interp(s, s[known], z[known])
        # s grows upstream; so does a level that never rises downstream.
        z = np.maximum.accumulate(z)
        low, high = np.maximum(s - half, s[0]), np.minimum(s + half, s[-1])
        fall = np.interp(high, s, z) - np.interp(low, s, z)
        slope[rows] = np.maximum(fall / np.maximum(high - low, 1e-9), MIN_SLOPE)
    return slope


def compute_level(key) -> dict:
    """
    The water of one level on the computation grid (see the module docstring).

    Returns
    -------
    dict
        wet (bool), surface (drawn water height, NG-L), depth and speed (m/s, NaN where dry),
        disconnected (cells below the WSE left dry: not connected to the channel, or outside the
        scenario's official extent), centreline_wse and slope (per centreline point).
    """
    grid = dem_grid()
    scenario = key if isinstance(key, str) else None
    area = corridor(scenario)
    nearest, channel = area["nearest"], area["channel"]
    inside = nearest >= 0

    sample_wse = centreline_wse(key)
    wse = np.full(grid["shape"], np.nan, dtype="float32")
    wse[inside] = sample_wse[nearest[inside]]
    with np.errstate(invalid="ignore"):
        below = inside & (wse > grid["mean"])
    under_wse = below
    if area["extent"] is not None:
        below = below & area["extent"]
    candidate = below | channel
    labels, count = ndimage.label(candidate)
    connected = np.zeros(count + 1, dtype=bool)
    connected[np.unique(labels[channel])] = True
    connected[0] = False
    wet = connected[labels]

    surface = wse.copy()
    surface[channel] = np.fmax(wse[channel], grid["top"][channel] + CHANNEL_MARGIN_M)
    # Depth = WSE - DEM; in the channel at least the margin the water is drawn with.
    depth = np.where(wet, wse - grid["mean"], np.nan).astype("float32")
    depth[channel & wet] = np.fmax(depth[channel & wet], CHANNEL_MARGIN_M)

    # Slope along the Alzette: of its WSE profile. The tributaries are held at their confluence
    # level, so theirs is that of the water drawn in their channel (median per centreline point).
    levels = sample_wse.copy()
    tributary = (centreline()["river"] != RIVER_NAME).to_numpy()
    channel_wet = channel & wet & tributary[np.maximum(nearest, 0)]
    drawn = pd.Series(surface[channel_wet]).groupby(nearest[channel_wet]).median()
    levels[drawn.index.to_numpy()] = np.fmax(levels[drawn.index.to_numpy()], drawn.to_numpy())
    slope = _profile_slope(levels)

    speed = np.full(grid["shape"], np.nan, dtype="float32")
    speed[wet] = (np.maximum(depth[wet], 0.0) ** (2.0 / 3.0)) * np.sqrt(slope[nearest[wet]]) / MANNING_N
    return {
        "wet": wet, "surface": surface, "depth": depth, "speed": speed,
        "disconnected": under_wse & ~wet, "centreline_wse": sample_wse, "slope": slope,
    }


@functools.lru_cache(maxsize=LEVEL_CACHE_SIZE)
def _level(key) -> dict:
    with _LOCK:
        computed = compute_level(key)
        return {
            "wet": np.packbits(computed["wet"]),
            "mesh": _mesh(key, computed),
            "wet_area_m2": float(computed["wet"].sum()) * abs(dem_grid()["transform"].a * dem_grid()["transform"].e),
        }


def wet_mask(key) -> np.ndarray:
    """The cells under water for a level key (bool, computation grid), by LIVE_WATER_METHOD / WATER_SURFACE_METHOD."""
    if uses_rem(key):
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import rem

        return rem.wet_mask(key)
    if WATER_SURFACE_METHOD == "sections":
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import flood_sections

        return flood_sections.wet_mask(key)
    grid = dem_grid()
    count = grid["shape"][0] * grid["shape"][1]
    return np.unpackbits(_level(key)["wet"], count=count).astype(bool).reshape(grid["shape"])


def level_mesh(key) -> dict:
    """The drawn mesh of a level key (see _mesh, or flood_sections._mesh), by LIVE_WATER_METHOD / WATER_SURFACE_METHOD."""
    if uses_rem(key):
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import rem

        return rem.level_mesh(key)
    if WATER_SURFACE_METHOD == "sections":
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import flood_sections

        return flood_sections.level_mesh(key)
    return _level(key)["mesh"]


# ============================================================
# MESH
# ============================================================

def _b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def _blocks(array: np.ndarray, factor: int, rows: int, cols: int) -> np.ndarray:
    return array[: rows * factor, : cols * factor].reshape(rows, factor, cols, factor)


@functools.lru_cache(maxsize=1)
def mesh_grid() -> dict:
    """
    The mesh grid shared by every level: its size, and its node corners in WGS84 so the map can
    place every node by bilinear interpolation (exact to the millimetre over the DEM).
    """
    grid = dem_grid()
    factor = max(int(round(WATER_MESH_M / abs(grid["transform"].a))), 1)
    rows, cols = grid["shape"][0] // factor, grid["shape"][1] // factor
    cell = abs(grid["transform"].a) * factor
    x0, y0 = grid["transform"].c, grid["transform"].f
    corners = gpd.GeoSeries(
        [Point(x0, y0), Point(x0 + cols * cell, y0), Point(x0, y0 - rows * cell), Point(x0 + cols * cell, y0 - rows * cell)],
        crs=grid["crs"],
    ).to_crs(4326)
    return {
        "factor": factor, "rows": rows, "cols": cols, "cell_m": cell,
        # Node (0, 0), (0, cols), (rows, 0), (rows, cols): north-west, north-east, south-west, south-east.
        "corners": [[round(p.x, 9), round(p.y, 9)] for p in corners],
        "height_offset_m": TERRAIN_HEIGHT_OFFSET_M,
        "rivers": list(dict.fromkeys(centreline()["river"])),
    }


def _node_values(values: np.ndarray, factor: int, rows: int, cols: int, reduce: str) -> np.ndarray:
    """
    Per mesh node, the highest ("max") or mean ("mean") value of the computation cells of the
    (up to four) mesh cells around it, ignoring NaN. With "max", every corner of a mesh cell is at
    least as high as any point of that cell, so the drawn water never dips under it.
    """
    blocks = _blocks(values, factor, rows, cols)
    with np.errstate(invalid="ignore"):
        if reduce == "max":
            per_cell = np.fmax.reduce(np.fmax.reduce(blocks, axis=3), axis=1)
        else:
            counts = np.isfinite(blocks).sum(axis=(1, 3))
            per_cell = np.where(counts > 0, np.nansum(blocks, axis=(1, 3)) / np.maximum(counts, 1), np.nan)
    padded = np.pad(per_cell, 1, constant_values=np.nan)
    picks = [padded[dr: dr + rows + 1, dc: dc + cols + 1] for dr in (0, 1) for dc in (0, 1)]
    if reduce == "max":
        out = picks[0]
        for pick in picks[1:]:
            out = np.fmax(out, pick)
        return out
    stack = np.stack(picks)
    counts = np.isfinite(stack).sum(axis=0)
    return np.where(counts > 0, np.nansum(stack, axis=0) / np.maximum(counts, 1), np.nan)


def _mesh(key, computed: dict, area: dict | None = None) -> dict:
    """
    The drawn water of a level, encoded for the map.

    The mesh cells are WATER_MESH_M squares holding water in any of their computation cells,
    plus a ring of one cell around them (not over cells below the WSE left dry): that ring lies under
    the terrain, so the terrain clips the water along the true shoreline instead of a staircase.
    Two layers, each with its own nodes: the floodplain (cells without channel) and the channel.
    Every node carries the drawn height of its layer's computation cells in the mesh cells around it
    (channel: the highest, so the water stays above the terrain; floodplain: the mean WSE, so the
    channel's lift does not spread onto the banks), the depth, the estimated velocity (downstream), the river and the
    river km of the nearest centreline point.

    Returns
    -------
    dict
        key; layers (floodplain, channel), each with cell_index (indices of its mesh cells, uint32)
        or cells (one bit per mesh cell), both row-major from the north-west, and per used node in
        row-major order: height (float32, m), flow (int16 pairs, mm/s east and north), depth
        (uint16, cm), km (uint16, river km x 100), river (uint8, index in mesh_grid rivers);
        arrows (per level of detail, [column, row, height, angle in radians from east]) and stats.
    """
    grid, mesh = dem_grid(), mesh_grid()
    factor, rows, cols = mesh["factor"], mesh["rows"], mesh["cols"]
    wet, surface = computed["wet"], computed["surface"]
    # The corridor of the level (nearest, channel, arrow_cells); the REM water passes its own.
    area = area or corridor(key if isinstance(key, str) else None)
    nearest = area["nearest"]

    wet_cells = _blocks(wet, factor, rows, cols).any(axis=(1, 3))
    blocked = _blocks(computed["disconnected"], factor, rows, cols).any(axis=(1, 3))
    inside = _blocks(nearest >= 0, factor, rows, cols).any(axis=(1, 3))
    cells = wet_cells | (ndimage.binary_dilation(wet_cells) & ~blocked & inside)

    # Centreline point of each node: that of the computation cell south-east of it (or the nearest one inside).
    node_rows = np.minimum(np.arange(rows + 1) * factor, grid["shape"][0] - 1)
    node_cols = np.minimum(np.arange(cols + 1) * factor, grid["shape"][1] - 1)
    owner = nearest[np.ix_(node_rows, node_cols)]
    for dr, dc in ((-1, 0), (0, -1), (-1, -1)):
        missing = owner < 0
        if not missing.any():
            break
        shifted = nearest[np.ix_(np.clip(node_rows + dr, 0, None), np.clip(node_cols + dc, 0, None))]
        owner[missing] = shifted[missing]

    points = centreline()
    rivers = mesh["rivers"]
    river_index = points["river"].map({river: i for i, river in enumerate(rivers)}).to_numpy()
    channel = area["channel"]
    channel_cells = _blocks(channel, factor, rows, cols).any(axis=(1, 3))

    layers = []
    for in_channel in (False, True):
        layer_cells = cells & (channel_cells if in_channel else ~channel_cells)
        own = channel if in_channel else ~channel
        used = np.zeros((rows + 1, cols + 1), dtype=bool)
        for dr in (0, 1):
            for dc in (0, 1):
                used[dr: dr + rows, dc: dc + cols] |= layer_cells
        # Channel: the highest, so the water stays above the terrain; floodplain: the mean WSE.
        height = _node_values(np.where(own, surface, np.nan), factor, rows, cols, "max" if in_channel else "mean")
        depth = _node_values(np.where(own, computed["depth"], np.nan), factor, rows, cols, "mean")
        speed = _node_values(np.where(own, computed["speed"], np.nan), factor, rows, cols, "mean")

        node = used.ravel()
        node_owner = owner.ravel()[node]
        has_owner = node_owner >= 0
        safe_owner = np.where(has_owner, node_owner, 0)
        node_speed = np.nan_to_num(speed.ravel()[node], nan=0.0)
        dx = np.where(has_owner, points["dx"].to_numpy()[safe_owner], 0.0)
        dy = np.where(has_owner, points["dy"].to_numpy()[safe_owner], 0.0)
        flow = np.column_stack([node_speed * dx, node_speed * dy]) * 1000.0
        # Every drawn cell holds a corridor cell of its layer, so each node of it has a height.
        node_row, node_col = np.divmod(np.flatnonzero(node), cols + 1)
        node_height = height.ravel()[node] + terrain_height_offset(
            grid["transform"].c + node_col * mesh["cell_m"], grid["transform"].f - node_row * mesh["cell_m"], grid["crs"]
        )
        layers.append({
            # Few cells: their indices; many: one bit per cell (whichever is smaller).
            **({"cell_index": _b64(np.flatnonzero(layer_cells.ravel()).astype("<u4"))}
               if layer_cells.sum() * 32 < layer_cells.size else {"cells": _b64(np.packbits(layer_cells.ravel()))}),
            "height": _b64(node_height.astype("<f4")),
            "flow": _b64(np.clip(np.round(flow), -32767, 32767).astype("<i2")),
            "depth": _b64(np.clip(np.round(np.nan_to_num(depth.ravel()[node], nan=0.0) * 100.0), 0, 65535).astype("<u2")),
            "km": _b64(np.clip(np.round(np.where(has_owner, points["river_km"].to_numpy()[safe_owner], 0.0) * 100.0), 0, 65535).astype("<u2")),
            "river": _b64(np.where(has_owner, river_index[safe_owner], 255).astype("u1")),
        })

    arrows = []
    flat_surface = surface.ravel()
    flat_wet = wet.ravel()
    cell = mesh["cell_m"]
    transform = grid["transform"]
    for spacing in ARROW_SPACING_M:
        level_arrows = []
        for river in rivers:
            rows_r = np.flatnonzero((points["river"] == river).to_numpy())
            s = points["s_m"].to_numpy()[rows_r]
            for target in np.arange(spacing / 2.0, s[-1], spacing):
                index = rows_r[int(np.argmin(np.abs(s - target)))]
                flat = area["arrow_cells"][index]
                if flat < 0 or not flat_wet[flat]:
                    continue
                r, c = divmod(int(flat), grid["shape"][1])
                x = transform.c + (c + 0.5) * transform.a
                y = transform.f + (r + 0.5) * transform.e
                level_arrows.append([
                    round((x - transform.c) / cell, 3), round((transform.f - y) / cell, 3),
                    round(float(flat_surface[flat]) + float(terrain_height_offset(np.array([x]), np.array([y]), grid["crs"])[0]), 3),
                    round(float(np.arctan2(points["dy"].iat[index], points["dx"].iat[index])), 4),
                ])
        arrows.append(level_arrows)

    wet_speed = computed["speed"][wet]
    return {
        "key": key_text(key),
        "layers": layers,
        "arrows": arrows,
        "stats": {
            "wet_area_m2": round(float(wet.sum()) * abs(transform.a * transform.e), 1),
            "speed_p05_m_s": round(float(np.percentile(wet_speed, 5)), 3) if wet_speed.size else None,
            "speed_median_m_s": round(float(np.median(wet_speed)), 3) if wet_speed.size else None,
            "speed_p95_m_s": round(float(np.percentile(wet_speed, 95)), 3) if wet_speed.size else None,
            "speed_max_m_s": round(float(wet_speed.max()), 3) if wet_speed.size else None,
            "manning_n": MANNING_N,
            "slope_smoothing_m": SLOPE_SMOOTHING_M,
        },
    }


# ============================================================
# EXTENT OF A FLOOD SCENARIO
# ============================================================

@functools.lru_cache(maxsize=1)
def dem_footprint():
    """The area covered by the DEM (where the water is computed), EPSG:2169."""
    grid = dem_grid()
    valid = np.isfinite(grid["mean"]).astype("uint8")
    parts = [shape(geometry) for geometry, value in features.shapes(valid, mask=valid.astype(bool), transform=grid["transform"])]
    return gpd.GeoSeries([unary_union(parts)], crs=grid["crs"]).to_crs(2169).iloc[0]


@functools.lru_cache(maxsize=len(SCENARIOS))
def water_extent(scenario: str):
    """
    The water extent of a flood scenario, as drawn: the computed water inside the DEM, and the
    official extent where there is no DEM to compute it.

    Returns
    -------
    Polygon | MultiPolygon
        EPSG:2169.
    """
    grid = dem_grid()
    wet = wet_mask(scenario)
    computed = [
        shape(geometry)
        for geometry, value in features.shapes(wet.astype("uint8"), mask=wet, transform=grid["transform"])
    ]
    computed = gpd.GeoSeries([MultiPolygon(computed) if computed else MultiPolygon()], crs=grid["crs"]).to_crs(2169).iloc[0]
    outside = flood_extent(scenario).difference(dem_footprint())
    return unary_union([computed.buffer(0), outside])
