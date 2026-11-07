# -*- coding: utf-8 -*-
"""
Water surface of the Alzette as tilted, connected sections along the river, rendered in Terria by
terriajs/water-level-control.js (the default; WATER_SURFACE_METHOD=grid uses eddie_water_level.flood_grid).

Method
------
Water-surface profile
    The water-surface elevation (WSE) at every river km from the gauges: gauge zero + stage,
    interpolated linearly by river km between the gauges and never rising downstream
    (water_surface._wse_profile, as for every water level), or the profile of a flood scenario
    (water_surface.scenario_wse). The tributaries have no gauges: their water takes the level at
    their confluence, as before.
Sections
    The per-kilometre-marker cells of water_surface.water_cells (one Voronoi cell per 100 m marker,
    Alzette and tributaries), SECTION_CORRIDOR_M wide on each side of the river (wide enough for
    the HQextreme extent), inside the DEM. The boundary between two neighbouring cells is the line
    halfway between their markers.
Tilt
    Each section is a surface tilted along the flow: its downstream boundary at the WSE of the
    river km halfway to the next marker downstream, its upstream boundary at the WSE halfway to
    the next marker upstream, and linear in between (by the distances to both boundaries), so it is
    level across the river. Neighbouring sections give their shared boundary the same height and
    share its vertices: the surface is continuous, without steps or gaps.
Display
    The surface is drawn at absolute heights (plus the terrain's geoid offset,
    water_surface.terrain_height_offset) with depth testing against the terrain, so the terrain
    hides it wherever the ground is higher: islands, banks and embankments show through. No margin
    is added. Water shows wherever the ground is lower, also where that ground is not connected
    to the river (see low_areas_not_connected).
Low water
    The DEM (LiDAR, depression-filled) has no river bed: its channel lies at or above the water
    surface of the survey, above gauge zero + stage at normal and low stages (median Hesperange
    stage 56 cm: the river would be under the terrain most of the time). Inside the Alzette channel
    (water_surface.river_channel) the water is therefore at least at the low-water floor: per
    section boundary, the LOW_WATER_QUANTILE of the channel ground within LOW_WATER_WINDOW_KM of it,
    plus LOW_WATER_LIFT_M, never higher downstream than upstream, linear by river km in between.
    So the channel water stays level across the river and tilted along it, and shared boundaries
    keep identical heights. Outside the channel the floor does not apply. Higher stages are
    above the floor and unchanged. LOW_WATER_FLOOR=off turns it off.
Flooded cells
    A cell of the computation grid (flood_grid.dem_grid) is under water where the surface at its
    centre is higher than its ground: building colours and flood reports use this.
Velocity (estimate)
    Manning, as in flood_grid: v = h^(2/3) S^(1/2) / n, h = WSE - ground, S the slope of the WSE
    profile over flood_grid.SLOPE_SMOOTHING_M.
"""

import base64
import functools
import os
import threading

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio import features
from scipy import ndimage
from scipy.spatial import Delaunay
from shapely import vectorized
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.prepared import prep

from eddie_water_level.flood_grid import (
    ARROW_SPACING_M,
    LEVEL_CACHE_SIZE,
    MANNING_N,
    MIN_SLOPE,
    SLOPE_SMOOTHING_M,
    centreline,
    corridor,
    dem_grid,
    key_text,
    mesh_grid,
)
from eddie_water_level.water_surface import (
    RIVER_NAME,
    _channel_lines,
    _never_rising_downstream,
    _wse_profile,
    rendered_gauge_wse,
    scenario_wse,
    terrain_height_offset,
    water_cells,
)


# ============================================================
# CONFIGURATION
# ============================================================

# Half-width in metres of the band around the river covered by the sections. Default 700 m:
# the HQextreme extent reaches at most 651 m from the river centreline inside the DEM.
SECTION_CORRIDOR_M = float(os.environ.get("SECTION_CORRIDOR_M", "700"))

# Spacing in metres of the vertices inside each section (the surface is linear along the flow,
# so this only needs to follow the bends of the section boundaries).
SECTION_MESH_M = float(os.environ.get("SECTION_MESH_M", "10"))

# Version of the section mesh format, in the urls, so browsers do not reuse an older one.
SECTION_FORMAT = 4

# Low-water floor of the channel water (see the module docstring): on unless LOW_WATER_FLOOR is
# off; the quantile of the channel ground near each section boundary, the half-length in river
# km of that stretch, and the lift above it that keeps the water visible over flat filled ground.
LOW_WATER_FLOOR = os.environ.get("LOW_WATER_FLOOR", "on").strip().lower() not in ("0", "off", "false", "no")
LOW_WATER_QUANTILE = float(os.environ.get("LOW_WATER_QUANTILE", "0.9"))
LOW_WATER_WINDOW_KM = 0.05
LOW_WATER_LIFT_M = float(os.environ.get("LOW_WATER_LIFT", "0.05"))

# Low areas not connected to the river smaller than this are not reported.
LOW_AREA_MIN_M2 = 200.0

_LOCK = threading.Lock()


# ============================================================
# SECTIONS
# ============================================================

def _neighbours(points: list, kms: list, index: int) -> tuple:
    """Markers downstream and upstream of marker `index` (mirrored at the ends) and the boundary kms."""
    here = np.asarray(points[index].coords[0][:2])
    down = np.asarray(points[index - 1].coords[0][:2]) if index > 0 else None
    up = np.asarray(points[index + 1].coords[0][:2]) if index + 1 < len(points) else None
    if down is None:
        down = 2 * here - up
        km_down = kms[index] - (kms[index + 1] - kms[index]) / 2.0
    else:
        km_down = (kms[index - 1] + kms[index]) / 2.0
    if up is None:
        up = 2 * here - down
        km_up = kms[index] + (kms[index] - kms[index - 1]) / 2.0
    else:
        km_up = (kms[index] + kms[index + 1]) / 2.0
    return here, down, up, km_down, km_up


def _boundary_distances(xy: np.ndarray, section: dict) -> tuple:
    """Distances of points (inside the section) to its downstream and upstream boundaries."""
    here, down, up = section["here"], section["down"], section["up"]
    to_down = (here - down) / np.linalg.norm(here - down)
    to_up = (here - up) / np.linalg.norm(here - up)
    d_down = (xy - (here + down) / 2.0) @ to_down
    d_up = (xy - (here + up) / 2.0) @ to_up
    return np.maximum(d_down, 0.0), np.maximum(d_up, 0.0)


def _fraction(xy: np.ndarray, section: dict) -> np.ndarray:
    """0 on the section's downstream boundary, 1 on its upstream one, linear in the distances."""
    d_down, d_up = _boundary_distances(xy, section)
    total = d_down + d_up
    return np.where(total > 0, d_down / np.where(total > 0, total, 1.0), 0.5)


def _triangulate(polygon: Polygon, step: float) -> tuple:
    """Vertices (boundary every `step` m, interior on a `step` m grid) and triangles of a polygon."""
    rings = [polygon.exterior, *polygon.interiors]
    boundary = np.concatenate([np.asarray(LineString(ring.coords).segmentize(step).coords)[:-1, :2] for ring in rings])
    min_x, min_y, max_x, max_y = polygon.bounds
    grid_x, grid_y = np.meshgrid(
        np.arange(np.floor(min_x / step) * step, max_x + step, step),
        np.arange(np.floor(min_y / step) * step, max_y + step, step),
    )
    grid_x, grid_y = grid_x.ravel(), grid_y.ravel()
    # Interior points not too close to the boundary, so no sliver triangles.
    inner = polygon.buffer(-0.3 * step)
    keep = vectorized.contains(inner, grid_x, grid_y) if not inner.is_empty else np.zeros(grid_x.size, bool)
    points = np.concatenate([boundary, np.column_stack([grid_x[keep], grid_y[keep]])])
    if len(points) < 3:
        return points, np.zeros((0, 3), dtype=int)
    triangles = Delaunay(points).simplices
    centroids = points[triangles].mean(axis=1)
    inside = vectorized.contains(polygon, centroids[:, 0], centroids[:, 1])
    return points, triangles[inside]


@functools.lru_cache(maxsize=1)
def sections() -> dict:
    """
    The sections and their mesh (see the module docstring), computed once.

    Returns
    -------
    dict
        sections (list of dicts: river, km_down, km_up, here / down / up marker positions,
        polygon, all in the grid CRS), and the mesh: xy (vertices), section (section of each
        vertex), fraction (0 downstream boundary .. 1 upstream boundary), triangles.
    """
    grid = dem_grid()
    lines = {river: (kms, list(gpd.GeoSeries(points, crs=2169).to_crs(grid["crs"])))
             for river, _, kms, points in _channel_lines()}
    cells = water_cells(SECTION_CORRIDOR_M).to_crs(grid["crs"])
    footprint = gpd.GeoSeries([_dem_footprint()], crs=grid["crs"]).iloc[0]

    section_list, all_xy, all_section, all_fraction, all_triangles = [], [], [], [], []
    offset = 0
    for river, river_km, geometry in zip(cells["river"], cells["river_km"], cells.geometry):
        kms, points = lines[river]
        # The cell's marker (tributary cells hold the km rounded to 0.1).
        index = int(np.argmin(np.abs(np.asarray(kms) - river_km)))
        # A tributary line starts at the Alzette marker of its confluence, which has no cell of its own.
        if river != RIVER_NAME and index == 0:
            index = 1
        here, down, up, km_down, km_up = _neighbours(points, kms, index)
        part = geometry.intersection(footprint)
        for polygon in getattr(part, "geoms", [part]):
            if polygon.geom_type != "Polygon" or polygon.area < 1.0:
                continue
            section = {"river": river, "km_down": km_down, "km_up": km_up, "here": here, "down": down, "up": up,
                       "polygon": polygon}
            xy, triangles = _triangulate(polygon, SECTION_MESH_M)
            if len(triangles) == 0:
                continue
            number = len(section_list)
            section_list.append(section)
            all_xy.append(xy)
            all_section.append(np.full(len(xy), number))
            all_fraction.append(_fraction(xy, section))
            all_triangles.append(triangles + offset)
            offset += len(xy)

    xy = np.concatenate(all_xy)
    section_of = np.concatenate(all_section)
    fraction = np.concatenate(all_fraction)
    triangles = np.concatenate(all_triangles)

    # Neighbouring sections share their boundary: the same point at the same river km is one vertex,
    # so the surface has no cracks. Points on a boundary between sections of different river kms
    # (meander necks) stay separate.
    km_down = np.array([s["km_down"] for s in section_list])
    km_up = np.array([s["km_up"] for s in section_list])
    river_number = pd.factorize(np.array([s["river"] for s in section_list]))[0]
    km = km_down[section_of] + fraction * (km_up[section_of] - km_down[section_of])
    keys = pd.DataFrame({
        "x": np.round(xy[:, 0], 2), "y": np.round(xy[:, 1], 2),
        "km": np.round(km, 5), "river": river_number[section_of],
    })
    unique_id = keys.groupby(["x", "y", "km", "river"], sort=False).ngroup().to_numpy()
    first = pd.Series(np.arange(len(xy))).groupby(unique_id).first().to_numpy()
    triangles = unique_id[triangles]
    # Triangles collapsed by the merge (all corners on one shared boundary) are dropped.
    keep = (triangles[:, 0] != triangles[:, 1]) & (triangles[:, 1] != triangles[:, 2]) & (triangles[:, 0] != triangles[:, 2])
    return {
        "sections": section_list,
        "xy": xy[first], "section": section_of[first], "fraction": fraction[first],
        "triangles": triangles[keep].astype("uint32"),
    }


@functools.lru_cache(maxsize=1)
def _dem_footprint():
    """The DEM's valid area, in the grid CRS (the sections stop there: no terrain to hide the water)."""
    grid = dem_grid()
    valid = np.isfinite(grid["mean"]).astype("uint8")
    parts = [shape(geometry) for geometry, _ in features.shapes(valid, mask=valid.astype(bool), transform=grid["transform"])]
    return MultiPolygon(parts).buffer(0)


@functools.lru_cache(maxsize=1)
def section_raster() -> tuple:
    """
    Section of every computation-grid cell (-1 outside the sections) and the fraction of its centre.
    """
    grid = dem_grid()
    found = sections()
    ids = features.rasterize(
        ((s["polygon"], number + 1) for number, s in enumerate(found["sections"])),
        out_shape=grid["shape"], transform=grid["transform"], fill=0, dtype="int32",
    ) - 1
    fraction = np.full(grid["shape"], np.nan, dtype="float32")
    transform = grid["transform"]
    rows, cols = np.nonzero(ids >= 0)
    xy = np.column_stack([transform.c + (cols + 0.5) * transform.a, transform.f + (rows + 0.5) * transform.e])
    owner = ids[rows, cols]
    values = np.empty(len(owner), dtype="float64")
    for number in np.unique(owner):
        mine = owner == number
        values[mine] = _fraction(xy[mine], found["sections"][number])
    fraction[rows, cols] = values
    return ids, fraction


# ============================================================
# LEVELS
# ============================================================

def _alzette_wse(key, kms: np.ndarray) -> np.ndarray:
    """WSE (NG-L, as drawn) on the Alzette at river kms, for a level key (see flood_grid.level_key)."""
    if isinstance(key, str):
        profile = scenario_wse(key)
        chainages = np.array(sorted(profile), dtype="float64")
        return np.interp(kms * 1000.0, chainages, [profile[c] for c in chainages])
    return _never_rising_downstream(kms, rendered_gauge_wse(_wse_profile(kms, *key)))


def boundary_levels(key) -> tuple:
    """WSE at the downstream and upstream boundary of every section, for a level key."""
    found = sections()["sections"]
    rivers = np.array([s["river"] for s in found])
    km_down = np.array([s["km_down"] for s in found])
    km_up = np.array([s["km_up"] for s in found])
    alzette = rivers == RIVER_NAME
    # One profile over every Alzette boundary, so neighbouring sections get identical values.
    boundary_kms = np.unique(np.concatenate([km_down[alzette], km_up[alzette]]))
    wse = _alzette_wse(key, boundary_kms)
    h_down = np.interp(km_down, boundary_kms, wse)
    h_up = np.interp(km_up, boundary_kms, wse)
    # Tributaries: the level at their confluence (no gauges).
    confluences = {river: confluence for river, confluence, _, _ in _channel_lines() if confluence is not None}
    for river, confluence in confluences.items():
        mine = rivers == river
        level = np.interp(confluence / 1000.0, boundary_kms, wse)
        h_down[mine] = level
        h_up[mine] = level
    return h_down, h_up


def _slope_at(key, kms: np.ndarray, rivers: np.ndarray) -> np.ndarray:
    """WSE slope along the river over SLOPE_SMOOTHING_M at river kms (MIN_SLOPE on the flat tributaries)."""
    half = SLOPE_SMOOTHING_M / 2000.0
    slope = np.full(len(kms), MIN_SLOPE)
    alzette = rivers == RIVER_NAME
    if alzette.any():
        fall = _alzette_wse(key, kms[alzette] + half) - _alzette_wse(key, kms[alzette] - half)
        slope[alzette] = np.maximum(fall / SLOPE_SMOOTHING_M, MIN_SLOPE)
    return slope


@functools.lru_cache(maxsize=1)
def low_water_floor() -> dict:
    """
    The low-water floor of the Alzette channel water (see the module docstring), computed once.

    Returns
    -------
    dict
        boundary_kms and floor (NG-L, -inf where the DEM has no channel ground), cell (floor of
        every computation-grid cell, -inf outside the Alzette channel) and vertex (floor of every
        section mesh vertex, -inf outside the Alzette channel).
    """
    found = sections()
    grid = dem_grid()
    ids, fraction = section_raster()
    channel = corridor(None)["channel"]
    rivers = np.array([s["river"] for s in found["sections"]])
    km_down = np.array([s["km_down"] for s in found["sections"]])
    km_up = np.array([s["km_up"] for s in found["sections"]])
    alzette = rivers == RIVER_NAME
    boundary_kms = np.unique(np.concatenate([km_down[alzette], km_up[alzette]]))

    owner = np.where(ids >= 0, ids, 0)
    in_channel = channel & (ids >= 0) & alzette[owner] & np.isfinite(grid["mean"])
    cell_km = km_down[owner[in_channel]] + fraction[in_channel] * (km_up[owner[in_channel]] - km_down[owner[in_channel]])
    ground = grid["mean"][in_channel]
    floor = np.full(len(boundary_kms), np.nan)
    for index, km in enumerate(boundary_kms):
        near = np.abs(cell_km - km) <= LOW_WATER_WINDOW_KM
        if near.sum() >= 5:
            floor[index] = np.quantile(ground[near], LOW_WATER_QUANTILE) + LOW_WATER_LIFT_M
    # Never higher downstream than upstream (river km grows upstream), so a bridge or a covered
    # stretch does not raise the water upstream of it.
    floor = np.fmin.accumulate(floor[::-1])[::-1]
    known = np.isfinite(floor)
    if not known.any():
        return {"boundary_kms": boundary_kms, "floor": np.full(len(boundary_kms), -np.inf),
                "cell": np.full(grid["shape"], -np.inf, dtype="float32"),
                "vertex": np.full(len(found["xy"]), -np.inf)}

    def floor_at(kms):
        values = np.interp(kms, boundary_kms[known], floor[known])
        # No floor beyond the stretch with channel ground.
        values[(kms < boundary_kms[known][0]) | (kms > boundary_kms[known][-1])] = -np.inf
        return values

    cell = np.full(grid["shape"], -np.inf, dtype="float32")
    cell[in_channel] = floor_at(cell_km)

    section = found["section"]
    vertex_km = km_down[section] + found["fraction"] * (km_up[section] - km_down[section])
    transform = grid["transform"]
    cols = np.floor((found["xy"][:, 0] - transform.c) / transform.a).astype(int)
    rows = np.floor((found["xy"][:, 1] - transform.f) / transform.e).astype(int)
    on_grid = (rows >= 0) & (rows < grid["shape"][0]) & (cols >= 0) & (cols < grid["shape"][1])
    vertex_channel = np.zeros(len(section), dtype=bool)
    vertex_channel[on_grid] = channel[rows[on_grid], cols[on_grid]]
    vertex = np.where(vertex_channel & alzette[section], floor_at(vertex_km), -np.inf)
    return {"boundary_kms": boundary_kms, "floor": np.where(known, floor, -np.inf), "cell": cell, "vertex": vertex}


def compute_level(key) -> dict:
    """
    The water of one level on the sections.

    Returns
    -------
    dict
        height (drawn WSE at every mesh vertex, NG-L), wet (computation-grid cells whose ground is
        below the surface), surface (WSE at every computation-grid cell in the sections, NaN outside).
    """
    found = sections()
    h_down, h_up = boundary_levels(key)
    section = found["section"]
    height = h_down[section] + found["fraction"] * (h_up[section] - h_down[section])

    grid = dem_grid()
    ids, fraction = section_raster()
    inside = ids >= 0
    surface = np.full(grid["shape"], np.nan, dtype="float32")
    owner = ids[inside]
    surface[inside] = h_down[owner] + fraction[inside] * (h_up[owner] - h_down[owner])
    if LOW_WATER_FLOOR:
        floor = low_water_floor()
        height = np.fmax(height, floor["vertex"])
        surface[inside] = np.fmax(surface[inside], floor["cell"][inside])
    with np.errstate(invalid="ignore"):
        wet = inside & (surface > grid["mean"])
    return {"height": height, "wet": wet, "surface": surface}


@functools.lru_cache(maxsize=LEVEL_CACHE_SIZE)
def _level(key) -> dict:
    with _LOCK:
        computed = compute_level(key)
        return {"wet": np.packbits(computed["wet"]), "mesh": _mesh(key, computed)}


def wet_mask(key) -> np.ndarray:
    """The computation-grid cells under water for a level key (bool)."""
    grid = dem_grid()
    count = grid["shape"][0] * grid["shape"][1]
    return np.unpackbits(_level(key)["wet"], count=count).astype(bool).reshape(grid["shape"])


def level_mesh(key) -> dict:
    """The drawn water of a level key (see _mesh)."""
    return _level(key)["mesh"]


# ============================================================
# MESH
# ============================================================

def _b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


@functools.lru_cache(maxsize=1)
def _static() -> dict:
    """Per vertex: ground height, river, river km, downstream direction, geoid offset; computed once."""
    found = sections()
    grid = dem_grid()
    xy = found["xy"]
    transform = grid["transform"]
    rows = np.clip(((xy[:, 1] - transform.f) / transform.e).astype(int), 0, grid["shape"][0] - 1)
    cols = np.clip(((xy[:, 0] - transform.c) / transform.a).astype(int), 0, grid["shape"][1] - 1)
    ground = grid["mean"][rows, cols]
    secs = found["sections"]
    section = found["section"]
    km_down = np.array([s["km_down"] for s in secs])[section]
    km_up = np.array([s["km_up"] for s in secs])[section]
    km = km_down + found["fraction"] * (km_up - km_down)
    direction = np.array([(s["down"] - s["up"]) / np.linalg.norm(s["down"] - s["up"]) for s in secs])[section]
    rivers = mesh_grid()["rivers"]
    river_name = np.array([s["river"] for s in secs])[section]
    offset = terrain_height_offset(xy[:, 0], xy[:, 1], grid["crs"])
    return {"ground": ground, "km": km, "direction": direction, "river_name": river_name,
            "river": np.array([rivers.index(r) if r in rivers else 255 for r in river_name]), "offset": offset}


@functools.lru_cache(maxsize=1)
def geometry() -> dict:
    """The section mesh shared by every level: vertices (mesh-grid columns / rows) and triangles."""
    found = sections()
    mesh = mesh_grid()
    transform = dem_grid()["transform"]
    columns = (found["xy"][:, 0] - transform.c) / mesh["cell_m"]
    rows = (transform.f - found["xy"][:, 1]) / mesh["cell_m"]
    return {
        "format": SECTION_FORMAT,
        "vertices": len(found["xy"]),
        "points": _b64(np.column_stack([columns, rows]).astype("<f4")),
        "triangles": _b64(found["triangles"].astype("<u4")),
        "sections": len(found["sections"]),
    }


def _mesh(key, computed: dict) -> dict:
    """
    The drawn water of a level, encoded for the map: one layer on the section mesh (geometry()),
    with per vertex height (float32, m, plus the terrain's geoid offset), flow (int16 pairs, mm/s
    east and north; the estimated velocity downstream), depth (uint16, cm; 0 where the ground is
    higher), km (uint16, river km x 100) and river (uint8, index in mesh_grid rivers); arrows (per
    level of detail, [column, row, height, angle in radians from east]) and stats.
    """
    static = _static()
    height = computed["height"]
    depth = np.maximum(height - static["ground"], 0.0)
    depth = np.where(np.isfinite(depth), depth, 0.0)
    slope = _slope_at(key, static["km"], static["river_name"])
    speed = depth ** (2.0 / 3.0) * np.sqrt(slope) / MANNING_N
    flow = static["direction"] * speed[:, None] * 1000.0

    # Arrows along the centreline, on the channel cell next to it, at the surface there.
    grid = dem_grid()
    transform = grid["transform"]
    cell = mesh_grid()["cell_m"]
    points = centreline()
    arrow_cells = corridor(None)["arrow_cells"]
    surface = computed["surface"].ravel()
    arrows = []
    for spacing in ARROW_SPACING_M:
        level_arrows = []
        for river in mesh_grid()["rivers"]:
            rows_r = np.flatnonzero((points["river"] == river).to_numpy())
            s = points["s_m"].to_numpy()[rows_r]
            for target in np.arange(spacing / 2.0, s[-1], spacing):
                index = rows_r[int(np.argmin(np.abs(s - target)))]
                flat = arrow_cells[index]
                if flat < 0 or not np.isfinite(surface[flat]):
                    continue
                r, c = divmod(int(flat), grid["shape"][1])
                x = transform.c + (c + 0.5) * transform.a
                y = transform.f + (r + 0.5) * transform.e
                offset = float(terrain_height_offset(np.array([x]), np.array([y]), grid["crs"])[0])
                level_arrows.append([
                    round((x - transform.c) / cell, 3), round((transform.f - y) / cell, 3),
                    round(float(surface[flat]) + offset, 3),
                    round(float(np.arctan2(points["dy"].iat[index], points["dx"].iat[index])), 4),
                ])
        arrows.append(level_arrows)

    wet_speed = speed[depth > 0]
    return {
        "key": key_text(key),
        "layers": [{
            "geometry": "sections",
            "height": _b64((height + static["offset"]).astype("<f4")),
            "flow": _b64(np.clip(np.round(flow), -32767, 32767).astype("<i2")),
            "depth": _b64(np.clip(np.round(depth * 100.0), 0, 65535).astype("<u2")),
            "km": _b64(np.clip(np.round(static["km"] * 100.0), 0, 65535).astype("<u2")),
            "river": _b64(static["river"].astype("u1")),
        }],
        "arrows": arrows,
        "stats": {
            "wet_area_m2": round(float(computed["wet"].sum()) * abs(transform.a * transform.e), 1),
            "speed_p05_m_s": round(float(np.percentile(wet_speed, 5)), 3) if wet_speed.size else None,
            "speed_median_m_s": round(float(np.median(wet_speed)), 3) if wet_speed.size else None,
            "speed_p95_m_s": round(float(np.percentile(wet_speed, 95)), 3) if wet_speed.size else None,
            "speed_max_m_s": round(float(wet_speed.max()), 3) if wet_speed.size else None,
            "manning_n": MANNING_N,
            "slope_smoothing_m": SLOPE_SMOOTHING_M,
        },
    }


# ============================================================
# LOW AREAS NOT CONNECTED TO THE RIVER
# ============================================================

def low_areas_not_connected(key) -> gpd.GeoDataFrame:
    """
    Areas where the drawn water shows (ground below the surface) that are not connected to the
    river channel: low ground behind dikes, embankments or road and rail fills.

    Returns
    -------
    gpd.GeoDataFrame
        area_m2, river, river_km (nearest centreline point) and geometry (EPSG:2169), largest first;
        areas under LOW_AREA_MIN_M2 left out.
    """
    grid = dem_grid()
    wet = wet_mask(key)
    channel = corridor(None)["channel"]
    labels, _ = ndimage.label(wet)
    connected = np.unique(labels[channel & wet])
    isolated = (labels > 0) & ~np.isin(labels, connected)
    shapes = [shape(geometry) for geometry, _ in features.shapes(
        isolated.astype("uint8"), mask=isolated, transform=grid["transform"])]
    frame = gpd.GeoDataFrame(geometry=shapes, crs=grid["crs"]).to_crs(2169)
    if frame.empty:
        return gpd.GeoDataFrame({"area_m2": [], "river": [], "river_km": []}, geometry=[], crs=2169)
    frame = frame.dissolve().explode(index_parts=False).reset_index(drop=True)
    frame["area_m2"] = frame.geometry.area.round(0)
    frame = frame[frame["area_m2"] >= LOW_AREA_MIN_M2].sort_values("area_m2", ascending=False).reset_index(drop=True)
    points = centreline()
    from scipy.spatial import cKDTree  # pylint: disable=import-outside-toplevel
    centroids = frame.to_crs(grid["crs"]).geometry.centroid
    _, nearest = cKDTree(points[["x", "y"]].to_numpy()).query(np.column_stack([centroids.x, centroids.y]))
    frame["river"] = points["river"].to_numpy()[nearest]
    frame["river_km"] = points["river_km"].to_numpy()[nearest].round(2)
    return frame
