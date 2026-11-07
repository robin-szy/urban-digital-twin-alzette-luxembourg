# -*- coding: utf-8 -*-
"""
3D buildings for the Alzette, rendered by TerriaJS / Cesium as CZML.

Method
------
Building footprints (ACT 2023 national buildings, or OpenStreetMap)
    -> keep the footprints in the river corridor
    -> height of each building, from the first source that has it:
         1. INSPIRE Building 3D (ACT 2023, measured heightAboveGround), joined by building id
         2. OpenStreetMap "height" or "building:levels" tags, when the footprint data has them
         3. the average height BUILDING_HEIGHT_M (environment variable, default 10 m,
            or ?height_m= on the endpoint)
    -> ground height from the terrain DEM under each footprint
    -> CZML polygons extruded from the ground, with every attribute as
       properties (shown by Terria when a building is clicked).

Flood status, tested in 2D on the footprint base: orange when the footprint
touches or partly overlaps the water, red when it lies fully in it. The water is
the extent drawn on the map (eddie_water_level.flood_grid): for gauge levels (live,
manual, day replay) the water cells of the level shown, tested at points along the
footprint edge and inside it (FLOOD_SAMPLE_STEP_M apart); for the HQ10 / HQ100 /
HQextreme scenarios the scenario's water extent as polygons (flood_grid.water_extent:
computed over the DEM, the official extent beyond it). The colours follow the water over time.

Heights use the same vertical reference as the terrain and the water
surface: the DEM height plus water_surface.terrain_height_offset (the geoid undulation the terrain
tiles were built with, plus TERRAIN_HEIGHT_OFFSET).
"""

import bisect
import functools
import mmap
import os
import pathlib
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import rowcol
from shapely import vectorized
from shapely.ops import transform
from shapely.prepared import prep

from eddie_water_level.flood_grid import dem_grid, wet_mask, water_extent
from eddie_water_level.water_surface import (
    SCENARIOS,
    TERRAIN_DEM,
    terrain_height_offset,
    water_cells,
)


# ============================================================
# CONFIGURATION
# ============================================================

# Average height in metres given to every building without height data.
BUILDING_HEIGHT_M = float(os.environ.get("BUILDING_HEIGHT_M", "10"))

# data.public.lu "Base de données nationale des bâtiments 3D 2023",
# ACT2023v2_Buildings3d_Footprints.gpkg (unzipped), mounted into the backend by
# docker-compose.override.yml. Reprojected below whatever its CRS.
BUILDINGS_GPKG = pathlib.Path(
    os.environ.get("BUILDINGS_GPKG", "/app/data/buildings/ACT2023v2_Buildings3d_Footprints.gpkg")
)
BUILDINGS_LAYER = os.environ.get("BUILDINGS_LAYER", "ACT2023v2_Buildings3d_Footprints")

# data.public.lu "INSPIRE - Annex III Theme Buildings - Building 3D", one
# bu.building3d-<commune>.gml per commune. Every .gml in this folder is read.
INSPIRE_BUILDING3D_DIR = pathlib.Path(
    os.environ.get("INSPIRE_BUILDING3D_DIR", "/app/data/buildings/inspire_building3d")
)

# Storey height for OpenStreetMap building:levels.
LEVEL_HEIGHT_M = 3.0

# Buildings within this distance of the Alzette (km 35-50), or inside its HQ100 extent, are shown.
BUILDINGS_CORRIDOR_M = 500.0

BUILDING_RGBA = [215, 210, 200, 255]
TOUCHED_RGBA = [255, 150, 30, 255]
FLOODED_RGBA = [220, 40, 40, 255]

# Flood status of a building: no water, footprint touched / partly in the water, fully in the water.
FLOOD_NONE, FLOOD_TOUCHED, FLOOD_FULL = 0, 1, 2
FLOOD_STATUS_TEXT = {FLOOD_NONE: "none", FLOOD_TOUCHED: "touched", FLOOD_FULL: "fully flooded"}
FLOOD_STATUS_RGBA = {FLOOD_NONE: BUILDING_RGBA, FLOOD_TOUCHED: TOUCHED_RGBA, FLOOD_FULL: FLOODED_RGBA}

# Spacing in metres of the test points along each footprint edge and inside it (the DEM is 0.5 m).
FLOOD_SAMPLE_STEP_M = 1.0

# Open ends of the colour intervals before the first and after the last water level.
_TIME_MIN = "0000-01-01T00:00:00Z"
_TIME_MAX = "9999-12-31T23:59:59Z"

HEIGHT_SOURCE_INSPIRE = "INSPIRE Building 3D 2023 (ACT, measured)"
HEIGHT_SOURCE_OSM_HEIGHT = "OpenStreetMap height tag"
HEIGHT_SOURCE_OSM_LEVELS = f"OpenStreetMap building:levels x {LEVEL_HEIGHT_M:g} m"
HEIGHT_SOURCE_AVERAGE = "Average height (BUILDING_HEIGHT_M)"

# Id of a building in the INSPIRE GML, e.g. Building3D_ACT_<uuid>, and its height value.
_GML_BUILDING = re.compile(rb'<bu-core3d:Building gml:id="Building3D_([^"]+)"')
_GML_HEIGHT = re.compile(rb'<bu-base:value uom="m">([^<]+)</bu-base:value>')


# ============================================================
# STUDY AREA
# ============================================================

def study_area():
    """
    The area whose buildings are shown and counted in the flood reports.

    The river corridor (BUILDINGS_CORRIDOR_M), widened by the HQ100 extent near the river
    when the flood hazard map is available.

    Returns
    -------
    Polygon | MultiPolygon
        EPSG:2169.
    """
    try:
        cells = water_cells(BUILDINGS_CORRIDOR_M, "hq100")
    except FileNotFoundError:
        cells = water_cells(BUILDINGS_CORRIDOR_M)
    return cells.to_crs(2169).unary_union


# ============================================================
# HEIGHTS
# ============================================================

@functools.lru_cache(maxsize=1)
def inspire_heights() -> dict:
    """
    Measured building heights from the INSPIRE Building 3D GML files.

    Only the height and id of each building are read, by a text scan: the files are
    hundreds of MB of 3D geometry that is not needed here.

    Returns
    -------
    dict
        Building id (ACT_<uuid>, the buildingid of the ACT footprints) -> height above
        ground in metres (highest point minus lowest ground point). Empty without files.
    """
    heights = {}
    for path in sorted(INSPIRE_BUILDING3D_DIR.glob("*.gml")):
        with open(path, "rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            starts, ids = [], []
            for match in _GML_BUILDING.finditer(data):
                starts.append(match.start())
                ids.append(match.group(1).decode())
            for match in _GML_HEIGHT.finditer(data):
                # The height belongs to the building whose element starts last before it.
                index = bisect.bisect_right(starts, match.start()) - 1
                if index >= 0:
                    heights.setdefault(ids[index], float(match.group(1)))
    return heights


def _intersects(geometries: gpd.GeoSeries, other) -> pd.Series:
    """geometries.intersects(other), much faster for the detailed area and flood extents (prepared once)."""
    prepared = prep(other)
    return geometries.apply(prepared.intersects).astype(bool)


def _extent_status(geometries: gpd.GeoSeries, extent) -> pd.Series:
    """Flood status of each 2D footprint against a flood extent: none, touched or fully inside."""
    prepared = prep(extent)
    return geometries.apply(
        lambda geom: FLOOD_FULL if prepared.contains(geom) else FLOOD_TOUCHED if prepared.intersects(geom) else FLOOD_NONE
    )


def _osm_height(row: pd.Series) -> tuple:
    """Height from OpenStreetMap tags, when the footprint data has them: (height_m, source) or (None, None)."""
    for column, scale, source in (("height", 1.0, HEIGHT_SOURCE_OSM_HEIGHT), ("building:levels", LEVEL_HEIGHT_M, HEIGHT_SOURCE_OSM_LEVELS)):
        value = row.get(column)
        if value is None or pd.isna(value):
            continue
        match = re.match(r"\s*([0-9]+(?:[.,][0-9]+)?)", str(value))
        if match:
            return float(match.group(1).replace(",", ".")) * scale, source
    return None, None


# ============================================================
# FOOTPRINTS
# ============================================================

@functools.lru_cache(maxsize=1)
def building_footprints() -> gpd.GeoDataFrame:
    """
    Footprints in the study area, with their height, ground height and flood status.

    Buildings outside the DEM have no ground height (NaN): they are counted in the flood
    reports but not drawn, as there is no terrain to stand them on.

    Returns
    -------
    gpd.GeoDataFrame
        Every column of the footprint data, plus measured_height_m and height_source
        (None / "average" when no height data), ground_min_m, ground_mean_m,
        flooded_hq10, flooded_hq100 and flooded_hqextreme (FLOOD_NONE / FLOOD_TOUCHED / FLOOD_FULL against the
        scenario's water extent, flood_grid.water_extent; None without the flood map), and geometry (EPSG:4326 polygons).
    """
    for path, name in [(BUILDINGS_GPKG, "Building footprint GeoPackage"), (TERRAIN_DEM, "Terrain DEM")]:
        if not path.is_file():
            raise FileNotFoundError(f"{name} not found: {path}")

    area = study_area()

    # Read only the area's bounding box, in the GeoPackage's own CRS.
    layer_crs = gpd.read_file(BUILDINGS_GPKG, layer=BUILDINGS_LAYER, rows=0).crs
    bbox = tuple(gpd.GeoSeries([area], crs=2169).to_crs(layer_crs).total_bounds)
    buildings = gpd.read_file(BUILDINGS_GPKG, layer=BUILDINGS_LAYER, bbox=bbox)
    buildings = buildings.to_crs(2169)
    buildings = buildings[_intersects(buildings.geometry, area)]
    buildings = buildings.explode(index_parts=False).reset_index(drop=True)
    buildings = buildings[buildings.geom_type == "Polygon"].reset_index(drop=True)
    # The ACT footprints are 3D (z = roof edge); only the outline is used.
    buildings["geometry"] = buildings.geometry.apply(lambda geom: transform(lambda x, y, z=None: (x, y), geom))

    # Measured heights, then OpenStreetMap tags.
    measured = inspire_heights()
    ids = buildings["buildingid"] if "buildingid" in buildings else pd.Series([None] * len(buildings))
    heights, sources = [], []
    for building_id, (_, row) in zip(ids, buildings.iterrows()):
        if building_id in measured:
            heights.append(measured[building_id])
            sources.append(HEIGHT_SOURCE_INSPIRE)
        else:
            height, source = _osm_height(row)
            heights.append(height)
            sources.append(source or HEIGHT_SOURCE_AVERAGE)
    buildings["measured_height_m"] = heights
    buildings["height_source"] = sources

    for scenario in SCENARIOS:
        try:
            buildings[f"flooded_{scenario}"] = _extent_status(buildings.geometry, water_extent(scenario))
        except FileNotFoundError:
            buildings[f"flooded_{scenario}"] = None

    with rasterio.open(TERRAIN_DEM) as dem:
        in_dem_crs = buildings.to_crs(dem.crs)
        nodata = dem.nodata
        ground_min, ground_mean = [], []
        for polygon in in_dem_crs.geometry:
            points = list(polygon.exterior.coords) + [polygon.centroid.coords[0]]
            heights = np.array([value[0] for value in dem.sample(points)], dtype="float64")
            if nodata is not None:
                heights[heights == nodata] = np.nan
            if np.isnan(heights).all():
                ground_min.append(np.nan)
                ground_mean.append(np.nan)
            else:
                ground_min.append(float(np.nanmin(heights)))
                ground_mean.append(float(np.nanmean(heights)))

    buildings["ground_min_m"] = ground_min
    buildings["ground_mean_m"] = ground_mean
    return buildings.to_crs(4326)


# ============================================================
# FLOOD STATUS FOR A WATER LEVEL
# ============================================================

def _footprint_points(polygon, step: float) -> np.ndarray:
    """2D test points of a footprint base: its vertices, its edges every `step` m, and a `step` m grid inside."""
    points = []
    for ring in [polygon.exterior, *polygon.interiors]:
        coords = np.asarray(ring.coords)[:, :2]
        for start, end in zip(coords[:-1], coords[1:]):
            count = max(int(np.ceil(np.hypot(*(end - start)) / step)), 1)
            fractions = np.arange(count)[:, None] / count
            points.append(start + fractions * (end - start))
    min_x, min_y, max_x, max_y = polygon.bounds
    grid_x, grid_y = np.meshgrid(np.arange(min_x + step / 2, max_x, step), np.arange(min_y + step / 2, max_y, step))
    grid_x, grid_y = grid_x.ravel(), grid_y.ravel()
    inside = vectorized.contains(polygon, grid_x, grid_y)
    points.append(np.column_stack([grid_x[inside], grid_y[inside]]))
    return np.concatenate(points)


@functools.lru_cache(maxsize=1)
def _flood_samples() -> tuple:
    """
    Test points of every drawn footprint, computed once.

    Returns
    -------
    tuple
        (owner, cell): for each point, the row of building_footprints() it belongs to, and the
        flat index of the flood_grid cell holding it (-1 outside the grid, where the water is
        not computed).
    """
    buildings = building_footprints().to_crs(2169)
    drawn = buildings[buildings["ground_min_m"].notna()]

    owner, points = [], []
    for index, polygon in drawn.geometry.items():
        footprint = _footprint_points(polygon, FLOOD_SAMPLE_STEP_M)
        points.append(footprint)
        owner.append(np.full(len(footprint), index))
    owner = np.concatenate(owner) if owner else np.zeros(0, dtype=int)
    xy = np.concatenate(points) if points else np.zeros((0, 2))

    grid = dem_grid()
    sample_xy = gpd.GeoSeries(gpd.points_from_xy(xy[:, 0], xy[:, 1]), crs=2169).to_crs(grid["crs"])
    rows, cols = rowcol(grid["transform"], sample_xy.x.to_numpy(), sample_xy.y.to_numpy())
    rows, cols = np.asarray(rows), np.asarray(cols)
    valid = (rows >= 0) & (rows < grid["shape"][0]) & (cols >= 0) & (cols < grid["shape"][1])
    cell = np.where(valid, rows * grid["shape"][1] + cols, -1)
    return owner, cell


def level_status(key) -> np.ndarray:
    """
    Flood status of every building for one water level.

    A test point is under water when its grid cell holds water in the level drawn on the map
    (flood_grid.wet_mask): the 2D flooded area the water surface shows.

    Parameters
    ----------
    key
        The water level (flood_grid.level_key).

    Returns
    -------
    np.ndarray
        FLOOD_NONE / FLOOD_TOUCHED / FLOOD_FULL per row of building_footprints().
    """
    owner, cell = _flood_samples()
    count = len(building_footprints())
    wet = np.append(wet_mask(key).ravel(), False)[cell]  # cell -1 picks the trailing False: never wet
    points = np.bincount(owner, minlength=count)
    wet_points = np.bincount(owner, weights=wet, minlength=count)
    status = np.full(count, FLOOD_NONE)
    status[wet_points > 0] = FLOOD_TOUCHED
    status[(points > 0) & (wet_points == points)] = FLOOD_FULL
    return status


# ============================================================
# CZML
# ============================================================

def _degrees(ring) -> list:
    positions = []
    for lon, lat in ring.coords[:-1]:
        positions += [round(lon, 7), round(lat, 7), 0]
    return positions


def _json_value(value):
    """Footprint attribute as a JSON value (numpy and missing values included)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _flood_text(value) -> str:
    if value is None or pd.isna(value):
        return "no flood map"
    return FLOOD_STATUS_TEXT[int(value)]


def _iso(timestamp) -> str:
    return pd.Timestamp(timestamp).strftime("%Y-%m-%dT%H:%M:%SZ")


def keyframe_status(levels: dict) -> np.ndarray:
    """
    Flood status of every building at each water-level keyframe.

    Parameters
    ----------
    levels : dict
        times, keyframe_scenarios (scenario name or None per keyframe) and keyframe_levels (the
        level drawn at each keyframe), as from eddie_water_level.blueprint.water_levels.

    Returns
    -------
    np.ndarray
        Shape (keyframes, buildings): FLOOD_NONE / FLOOD_TOUCHED / FLOOD_FULL.
    """
    buildings = building_footprints()
    statuses = []
    for index, scenario in enumerate(levels["keyframe_scenarios"]):
        if scenario is not None:
            # The scenario's water extent, as in the flood report.
            statuses.append(buildings[f"flooded_{scenario}"].fillna(FLOOD_NONE).to_numpy(dtype=int))
        else:
            statuses.append(level_status(levels["keyframe_levels"][index]))
    return np.array(statuses)


def _intervals(times: list, values: list, key: str, convert) -> list | dict:
    """CZML value that changes at the keyframe times; a plain value when it never changes."""
    if all(value == values[0] for value in values):
        return {key: convert(values[0])}
    intervals, start = [], _TIME_MIN
    for index, value in enumerate(values):
        if index + 1 < len(values) and values[index + 1] == value:
            continue
        end = _iso(times[index + 1]) if index + 1 < len(values) else _TIME_MAX
        intervals.append({"interval": f"{start}/{end}", key: convert(value)})
        start = end
    return intervals


def build_czml(height_m: float = BUILDING_HEIGHT_M, levels: dict | None = None) -> list:
    """
    Build the CZML document of the extruded buildings.

    Parameters
    ----------
    height_m : float
        Height in metres of the buildings without height data, above the mean ground height under them.
    levels : dict | None
        Water levels to colour the buildings by (see keyframe_status). Each building turns orange
        when the water touches its footprint and red when its footprint is fully in the water,
        changing at the keyframe times. Without levels, no building is coloured.

    Returns
    -------
    list
        CZML packets, ready for json serialisation.
    """
    buildings = building_footprints()
    if levels is not None:
        times = levels["times"]
        status = keyframe_status(levels)
    else:
        times, status = [None], np.full((1, len(buildings)), FLOOD_NONE)

    czml = [{
        "id": "document",
        "name": "Alzette 3D buildings",
        "version": "1.0",
    }]
    # Only the colour and flood status change with the water; the rest is built once per height.
    for index, packet in _static_packets(height_m):
        building_status = status[:, index].tolist()
        flood_status = _intervals(times, building_status, "string", FLOOD_STATUS_TEXT.get)
        czml.append({
            **packet,
            # Flood status at the water level shown (changes with the timeline).
            "properties": {
                **packet["properties"],
                "flood_status": flood_status["string"] if isinstance(flood_status, dict) else flood_status,
            },
            "polygon": {
                **packet["polygon"],
                "material": {"solidColor": {"color": _intervals(times, building_status, "rgba", FLOOD_STATUS_RGBA.get)}},
            },
        })
    return czml


@functools.lru_cache(maxsize=4)
def _static_packets(height_m: float) -> tuple:
    """
    The parts of every drawn building's CZML packet that do not depend on the water.

    Returns
    -------
    tuple
        (row of building_footprints(), packet) pairs; packets without material and flood_status.
    """
    buildings = building_footprints()
    attributes = [column for column in buildings.columns if column not in (
        "geometry", "measured_height_m", "height_source", "ground_min_m", "ground_mean_m",
    ) and not column.startswith("flooded_")]
    footprint_area = buildings.to_crs(2169).geometry.area
    # The terrain tiles hold ellipsoid heights (DEM + geoid): the buildings stand on them with the same offset.
    centres = buildings.to_crs(2169).geometry.centroid
    height_offset = pd.Series(terrain_height_offset(centres.x.to_numpy(), centres.y.to_numpy()), index=buildings.index)

    packets = []
    for index, building in buildings.iterrows():
        if pd.isna(building["ground_min_m"]):
            # Outside the DEM.
            continue
        if pd.notna(building["measured_height_m"]):
            height = float(building["measured_height_m"])
            # Measured heights run from the lowest ground point to the highest point.
            top = building["ground_min_m"] + height
            source = building["height_source"]
        else:
            # Without height data, the average height sits on the mean ground height.
            height = height_m
            top = building["ground_mean_m"] + height_m
            source = HEIGHT_SOURCE_AVERAGE

        polygon = {
            "positions": {"cartographicDegrees": _degrees(building.geometry.exterior)},
            # The base sits at the lowest ground point so a building on a slope does not float.
            "height": round(building["ground_min_m"] + height_offset[index], 2),
            "extrudedHeight": round(top + height_offset[index], 2),
            "outline": False,
        }
        if building.geometry.interiors:
            polygon["holes"] = {"cartographicDegrees": [_degrees(ring) for ring in building.geometry.interiors]}

        properties = {column: _json_value(building[column]) for column in attributes}
        properties.update({
            "height_m": round(height, 2),
            "height_source": source,
            "ground_height_m": round(building["ground_min_m"], 2),
            "footprint_area_m2": round(float(footprint_area[index]), 1),
            "HQ10": _flood_text(building["flooded_hq10"]),
            "HQ100": _flood_text(building["flooded_hq100"]),
            "HQextreme": _flood_text(building["flooded_hqextreme"]),
        })

        packets.append((index, {
            "id": f"building-{index}",
            "name": f"Building {properties.get('buildingid') or properties.get('osm_id') or index}",
            "properties": properties,
            "polygon": polygon,
        }))
    return tuple(packets)
