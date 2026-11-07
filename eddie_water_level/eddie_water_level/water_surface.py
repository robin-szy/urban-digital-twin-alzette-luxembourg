# -*- coding: utf-8 -*-
"""
3D water surface for the Alzette, rendered by TerriaJS / Cesium as CZML.

Method
------
The water-surface elevation (WSE) along the river comes from the gauges (wse_from_gauges:
gauge zero + stage, linear by river km, never rising downstream) or from a flood scenario
(scenario_wse). eddie_water_level.flood_grid extends it sideways over the LiDAR DEM (height above
nearest drainage), keeps the water where it is deeper than the ground and connected to the river
channel, and estimates its velocity; terriajs/water-level-control.js draws it as a continuous,
animated water surface from /water-surface-mesh.json.

The CZML of build_czml carries the timeline of the water levels (Terria's clock follows it);
the river corridor (water_cells) and channel (river_channel) here bound where water may be.

Flood scenarios (HQ10, HQ100, HQextreme) use the official extents of the AGE flood
hazard maps. Their water level per cell is the terrain height along the
edge of the extent: where the flooded area ends, the water meets the ground.

Heights are in the gauge datum (NG-L). TERRAIN_HEIGHT_OFFSET must equal
the --height-offset given to scripts/prepare_dem.py, so the water and
the terrain use the same vertical reference.

The LiDAR terrain records the river surface (at the stage of the survey
flight) as ground, so at normal stages the channel lies above the gauge water
level. Inside the channel the water is therefore drawn at least CHANNEL_MARGIN_M above the
terrain (flood_grid); WATER_RENDER_OFFSET can raise all gauge water (see rendered_gauge_wse).
"""

import functools
import os
import pathlib
from typing import Dict, List, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import LineString, MultiPoint
from shapely.ops import voronoi_diagram
from sqlalchemy import inspect as sa_inspect, text

from eddie.digitaltwin import setup_environment

from eddie_water_level.tasks import (
    HESPERANGE_GAUGE_ZERO_M,
    HESPERANGE_ID,
    HESPERANGE_KM,
    HISTORY_TABLE,
    PFAFFENTHAL_GAUGE_ZERO_M,
    PFAFFENTHAL_ID,
    PFAFFENTHAL_KM,
    RIVER_BEGIN_KM,
    RIVER_END_KM,
)


# ============================================================
# CONFIGURATION
# ============================================================

# data.public.lu "Kilométrage des cours d'eau", mounted into the backend
# by docker-compose.override.yml.
KILOMETRAGE_GPKG = pathlib.Path(
    os.environ.get(
        "RIVER_KILOMETRAGE_GPKG",
        "/app/data/river_kilometers/kilometrage-des-cours-deau.gpkg",
    )
)
KILOMETRAGE_LAYER = "Kilometrierung primäre Gewässer"
RIVER_NAME = "Alzette"

# Upstream gauge of the reach (AGE station sheet 01-Livange: km 54.34, gauge zero 265.80 m NG-L).
# Pfaffenthal and Hesperange are defined in eddie_water_level.tasks.
LIVANGE_ID = 35
LIVANGE_KM = 54.34
LIVANGE_GAUGE_ZERO_M = 265.80

# Tributaries: any river in this layer whose lowest marker (its mouth)
# lies this close to the Alzette reach. In km 35-50 these are the
# Peitruss (Pétrusse) and the Drosbech.
TRIBUTARY_LAYER = "Kilometrierung sekundäre Gewässer"
TRIBUTARY_MOUTH_MAX_M = 400.0

# Length of each tributary, upstream from its mouth, that may hold water.
TRIBUTARY_MAX_KM = 2.0

# Half-width of the band around each tributary that may hold water.
TRIBUTARY_CORRIDOR_M = 150.0

# Metres added to every water height; must match prepare_dem.py --height-offset.
TERRAIN_HEIGHT_OFFSET_M = float(
    os.environ.get("TERRAIN_HEIGHT_OFFSET", "0")
)

# Metres the water drawn from gauge levels (live, manual, replay) is raised. Default 0: the
# channel margin below keeps the water visible in the channel without raising it on the banks.
# Not added to the flood scenarios, whose levels are taken from the terrain itself.
WATER_RENDER_OFFSET_M = float(
    os.environ.get("WATER_RENDER_OFFSET", "0")
)

# Inside the river channel only, the drawn water stays at least this many metres above the
# displayed terrain (the highest DEM point under it, see flood_grid), so local bumps such as weirs,
# bridges and the river surface the LiDAR recorded as ground do not hide it. Outside the
# channel the water keeps the gauge level, so dry banks stay dry.
CHANNEL_MARGIN_M = float(
    os.environ.get("CHANNEL_MARGIN", "0.05")
)

# The channel: OpenStreetMap (Geofabrik extract, the BUILDINGS_GPKG alternative already in
# data/raw/Buildings) lines of each river of the reach, widened to at least
# CHANNEL_MIN_HALF_WIDTH_M, plus the OSM river areas they run through.
OSM_GPKG = pathlib.Path(
    os.environ.get("OSM_GPKG", "/app/data/buildings/luxembourg.gpkg")
)
OSM_WATERWAYS_LAYER = "gis_osm_waterways_free"
OSM_WATER_AREAS_LAYER = "gis_osm_water_a_free"
OSM_CHANNEL_CLASSES = ("river", "stream", "canal")
# Each kilometrage river takes the OSM name with the most line length this close to it
# (Alzette, Pétrusse, Drosbach); unnamed lines, mill races and side streams are left out.
CHANNEL_SEARCH_M = 30.0
OSM_RIVER_AREA_CLASSES = ("river", "riverbank")
CHANNEL_MIN_HALF_WIDTH_M = 3.0

# Half-width of the band around the river that may hold water.
# 300 m matches aoi_alzette_35_50_300m.
DEFAULT_CORRIDOR_M = 300.0

# KILO_KM carries float noise (46.299999); far below the 100 m spacing.
KM_TOLERANCE = 1e-3

WATER_RGBA = [40, 110, 170, 170]

# Master DEM written by scripts/prepare_dem.py (original heights, before the offset).
TERRAIN_DEM = pathlib.Path(
    os.environ.get("TERRAIN_DEM", "/app/data/terrain/dem_merged.tif")
)

# Geoid undulation (EGM2008) the 3D terrain tiles were built with, written by
# scripts/build_lidar_terrain.sh: the tiles hold DEM height (NG95) + N, ellipsoid heights. Everything
# drawn on them (water, buildings, arrows) adds the same N (terrain_height_offset). Without the file, N is 0.
TERRAIN_GEOID_GRID = pathlib.Path(
    os.environ.get("TERRAIN_GEOID_GRID", "/app/data/terrain/lidar_egm2008/geoid_egm2008.tif")
)

# AGE flood hazard maps 2021 (data.public.lu "Flood hazard maps (10 year flood / 100 year flood / extreme)"),
# unzipped, mounted into the backend by docker-compose.override.yml. One extent polygon per scenario.
FLOOD_HAZARD_DIR = pathlib.Path(
    os.environ.get("FLOOD_HAZARD_DIR", "/app/data/flood_hazard")
)
SCENARIOS = {"hq10": "HWGK2021_HQ10", "hq100": "HWGK2021_HQ100", "hqextreme": "HWGK2021_HQext"}

# The official extent reaches further from the river than the corridor; up to
# this distance it is added to the area that may hold water in a scenario.
SCENARIO_REACH_M = 1000.0

# Spacing of the terrain samples along the edge of the flood extent.
SCENARIO_SAMPLE_STEP_M = 5.0

# Cells averaged along the river to smooth the scenario level.
SCENARIO_SMOOTHING_CELLS = 5


@functools.lru_cache(maxsize=1)
def _geoid_grid():
    """(interpolator over (y, x), crs) of TERRAIN_GEOID_GRID, or None without the file."""
    if not TERRAIN_GEOID_GRID.is_file():
        return None
    # pylint: disable=import-outside-toplevel
    from scipy.interpolate import RegularGridInterpolator

    with rasterio.open(TERRAIN_GEOID_GRID) as grid:
        values = grid.read(1).astype("float64")
        xs = grid.transform.c + (np.arange(grid.width) + 0.5) * grid.transform.a
        ys = grid.transform.f + (np.arange(grid.height) + 0.5) * grid.transform.e
        crs = grid.crs
    # Rows run from north to south; the interpolator needs ascending coordinates.
    interpolator = RegularGridInterpolator((ys[::-1], xs), values[::-1], bounds_error=False, fill_value=None)
    return interpolator, crs


def terrain_height_offset(x, y, crs=2169) -> np.ndarray:
    """
    Metres to add to NG95 heights at these points so they sit on the 3D terrain: TERRAIN_HEIGHT_OFFSET_M
    plus the geoid undulation of the terrain tiles there (TERRAIN_GEOID_GRID, bilinear).

    Parameters
    ----------
    x, y : array-like
        Coordinates in `crs` (default EPSG:2169).
    """
    x, y = np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64")
    grid = _geoid_grid()
    if grid is None:
        return np.full(x.shape, TERRAIN_HEIGHT_OFFSET_M)
    interpolator, grid_crs = grid
    points = gpd.GeoSeries(gpd.points_from_xy(x.ravel(), y.ravel()), crs=crs).to_crs(grid_crs)
    values = interpolator(np.column_stack([points.y.to_numpy(), points.x.to_numpy()]))
    return values.reshape(x.shape) + TERRAIN_HEIGHT_OFFSET_M


# ============================================================
# GEOMETRY
# ============================================================

@functools.lru_cache(maxsize=8)
def water_cells(corridor_m: float = DEFAULT_CORRIDOR_M, scenario: str | None = None) -> gpd.GeoDataFrame:
    """
    Split the river corridor into one cell per kilometrage marker.

    Parameters
    ----------
    corridor_m : float
        Half-width in metres of the band around the river that may hold water.
    scenario : str | None
        Flood scenario ("hq10", "hq100", "hqextreme") whose official extent, up to SCENARIO_REACH_M
        from the river, is added to the corridor.

    Returns
    -------
    gpd.GeoDataFrame
        Columns river, chainage_m, river_km and geometry (EPSG:4326 polygons). chainage_m is the Alzette
        chainage whose water level the cell takes: its own for Alzette cells, the confluence for tributary cells.
    """
    if not KILOMETRAGE_GPKG.is_file():
        raise FileNotFoundError(
            f"River kilometrage file not found: {KILOMETRAGE_GPKG}. "
            "Download kilometrage-des-cours-deau.gpkg from data.public.lu "
            "into data/raw/River_kilometers/."
        )

    markers = gpd.read_file(KILOMETRAGE_GPKG, layer=KILOMETRAGE_LAYER).to_crs(2169)
    markers = (
        markers[
            (markers["TOPONYME"] == RIVER_NAME)
            & (markers["KILO_KM"] >= RIVER_BEGIN_KM - KM_TOLERANCE)
            & (markers["KILO_KM"] <= RIVER_END_KM + KM_TOLERANCE)
        ]
        .sort_values("KILO_KM")
        .reset_index(drop=True)
    )
    if len(markers) < 2:
        raise ValueError(
            f"Fewer than 2 {RIVER_NAME} markers between km {RIVER_BEGIN_KM} and {RIVER_END_KM} in {KILOMETRAGE_GPKG}"
        )

    points = list(markers.geometry)
    corridor = LineString(points).buffer(corridor_m)
    if scenario is not None:
        corridor = corridor.union(flood_extent(scenario).intersection(LineString(points).buffer(SCENARIO_REACH_M)))

    # Each Voronoi cell holds the area closest to one marker, so the cells
    # tile the corridor without overlaps and follow the river's bends.
    voronoi = voronoi_diagram(MultiPoint(points), envelope=corridor)
    cells = []
    for point in points:
        cell = next(poly for poly in voronoi.geoms if poly.intersects(point))
        cells.append(cell.intersection(corridor))

    chainage = (markers["KILO_KM"] * 1000).round().astype(int)
    result = gpd.GeoDataFrame(
        {"river": RIVER_NAME, "chainage_m": chainage, "river_km": chainage / 1000.0},
        geometry=cells,
        crs=2169,
    )
    tributaries = _tributary_cells(markers, chainage, corridor)
    if not tributaries.empty:
        result = pd.concat([result, tributaries], ignore_index=True)
    # A cell can split into parts where the corridor pinches; keep each part.
    result = result.explode(index_parts=False).reset_index(drop=True)
    # Clipping can leave slivers of zero area (lines, points) where cells touch the corridor edge.
    result = result[result.geom_type == "Polygon"].reset_index(drop=True)
    return result.to_crs(4326)


def _tributary_cells(
    alzette_markers: gpd.GeoDataFrame,
    alzette_chainage: pd.Series,
    alzette_corridor,
) -> gpd.GeoDataFrame:
    """
    Cells along the tributaries that flow into the Alzette reach, built like the Alzette cells.

    Each tributary cell takes the Alzette water level at its confluence (backwater): there are
    no gauges on the tributaries, so their water rises and falls with the Alzette and stays
    flat up the side valley until the terrain is higher.

    Parameters
    ----------
    alzette_markers : gpd.GeoDataFrame
        The Alzette markers of the reach, EPSG:2169, sorted by km.
    alzette_chainage : pd.Series
        Chainage in metres of each Alzette marker.
    alzette_corridor : Polygon
        The Alzette corridor; tributary cells stop at its edge so both surfaces join without overlapping.

    Returns
    -------
    gpd.GeoDataFrame
        Columns river, chainage_m (of the confluence), river_km (along the tributary) and geometry, EPSG:2169.
    """
    all_markers = gpd.read_file(KILOMETRAGE_GPKG, layer=TRIBUTARY_LAYER).to_crs(2169)
    parts = []
    for name, river in all_markers.groupby("TOPONYME"):
        mouth = river.loc[river["KILO_KM"].idxmin()]
        distances = alzette_markers.distance(mouth.geometry)
        if distances.min() > TRIBUTARY_MOUTH_MAX_M:
            continue
        confluence = distances.idxmin()

        river = river[river["KILO_KM"] <= TRIBUTARY_MAX_KM + KM_TOLERANCE].sort_values("KILO_KM")
        # Start the line at the Alzette so the tributary corridor reaches the main river.
        points = [alzette_markers.geometry[confluence]] + list(river.geometry)
        corridor = LineString(points).buffer(TRIBUTARY_CORRIDOR_M).difference(alzette_corridor)
        if corridor.is_empty:
            continue

        voronoi = voronoi_diagram(MultiPoint(list(river.geometry)), envelope=corridor)
        cells = []
        for point in river.geometry:
            cell = next(poly for poly in voronoi.geoms if poly.intersects(point))
            cells.append(cell.intersection(corridor))

        parts.append(gpd.GeoDataFrame(
            {
                "river": name,
                "chainage_m": int(alzette_chainage[confluence]),
                "river_km": river["KILO_KM"].round(1).to_list(),
            },
            geometry=cells,
            crs=2169,
        ))

    if not parts:
        return gpd.GeoDataFrame({"river": [], "chainage_m": [], "river_km": []}, geometry=[], crs=2169)
    return pd.concat(parts, ignore_index=True)


# ============================================================
# RIVER CHANNEL
# ============================================================

def _channel_lines() -> list:
    """
    Kilometrage lines of the Alzette reach and of its tributaries (as in water_cells).

    Returns
    -------
    list
        (river, confluence chainage in m or None for the Alzette, kms, points) per river, EPSG:2169,
        ordered from downstream to upstream; a tributary line starts at its Alzette confluence.
    """
    markers = gpd.read_file(KILOMETRAGE_GPKG, layer=KILOMETRAGE_LAYER).to_crs(2169)
    markers = (
        markers[
            (markers["TOPONYME"] == RIVER_NAME)
            & (markers["KILO_KM"] >= RIVER_BEGIN_KM - KM_TOLERANCE)
            & (markers["KILO_KM"] <= RIVER_END_KM + KM_TOLERANCE)
        ]
        .sort_values("KILO_KM")
        .reset_index(drop=True)
    )
    lines = [(RIVER_NAME, None, markers["KILO_KM"].to_list(), list(markers.geometry))]
    chainage = (markers["KILO_KM"] * 1000).round().astype(int)
    for name, river in gpd.read_file(KILOMETRAGE_GPKG, layer=TRIBUTARY_LAYER).to_crs(2169).groupby("TOPONYME"):
        mouth = river.loc[river["KILO_KM"].idxmin()]
        distances = markers.distance(mouth.geometry)
        if distances.min() > TRIBUTARY_MOUTH_MAX_M:
            continue
        confluence = distances.idxmin()
        river = river[river["KILO_KM"] <= TRIBUTARY_MAX_KM + KM_TOLERANCE].sort_values("KILO_KM")
        lines.append((
            name, int(chainage[confluence]), [0.0] + river["KILO_KM"].to_list(),
            [markers.geometry[confluence]] + list(river.geometry),
        ))
    return lines


@functools.lru_cache(maxsize=1)
def river_channel():
    """
    The river channel: the part of the water area where the drawn water is kept above the terrain.

    The OpenStreetMap lines (OSM_CHANNEL_CLASSES) of the Alzette and its tributaries, widened to
    CHANNEL_MIN_HALF_WIDTH_M on each side, plus the OSM river areas they run through. Each
    kilometrage river is matched to the OSM name with the most line length within CHANNEL_SEARCH_M
    of it. Without the OSM file (or a match), the kilometrage line widened the same way.

    Returns
    -------
    Polygon | MultiPolygon
        EPSG:2169, inside water_cells(DEFAULT_CORRIDOR_M).
    """
    cells = water_cells(DEFAULT_CORRIDOR_M).to_crs(2169)
    area = cells.unary_union
    lines = [(river, LineString(points)) for river, _, _, points in _channel_lines()]
    if not OSM_GPKG.is_file():
        return gpd.GeoSeries([line for _, line in lines], crs=2169).buffer(CHANNEL_MIN_HALF_WIDTH_M).unary_union.intersection(area)

    bbox = tuple(gpd.GeoSeries([area], crs=2169).to_crs(4326).total_bounds)
    ways = gpd.read_file(OSM_GPKG, layer=OSM_WATERWAYS_LAYER, bbox=bbox).to_crs(2169)
    ways = ways[ways["fclass"].isin(OSM_CHANNEL_CLASSES) & ways["name"].notna()]
    waters = gpd.read_file(OSM_GPKG, layer=OSM_WATER_AREAS_LAYER, bbox=bbox).to_crs(2169)
    waters = waters[waters["fclass"].isin(OSM_RIVER_AREA_CLASSES)]
    parts = []
    for river, line in lines:
        own_area = cells[cells["river"] == river].unary_union
        near = ways.geometry.intersection(line.buffer(CHANNEL_SEARCH_M))
        lengths = near.length.groupby(ways["name"]).sum()
        if lengths.empty or lengths.max() == 0:
            parts.append(line.buffer(CHANNEL_MIN_HALF_WIDTH_M).intersection(own_area))
            continue
        own = ways[ways["name"] == lengths.idxmax()].geometry.intersection(own_area)
        own = own[~own.is_empty]
        parts += list(own.buffer(CHANNEL_MIN_HALF_WIDTH_M).intersection(own_area))
        parts += list(waters[waters.intersects(own.unary_union)].geometry.intersection(own_area))
    return gpd.GeoSeries(parts, crs=2169).unary_union.intersection(area)


# ============================================================
# FLOOD SCENARIOS
# ============================================================

@functools.lru_cache(maxsize=len(SCENARIOS))
def flood_extent(scenario: str):
    """
    Official flood extent of a scenario around the Alzette reach.

    Parameters
    ----------
    scenario : str
        "hq10", "hq100" or "hqextreme".

    Returns
    -------
    Polygon | MultiPolygon
        The extent in EPSG:2169, clipped to the surroundings of km 35-50 and its tributaries.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown flood scenario {scenario!r}; use one of {', '.join(SCENARIOS)}.")
    path = FLOOD_HAZARD_DIR / SCENARIOS[scenario] / f"{SCENARIOS[scenario]}.shp"
    if not path.is_file():
        raise FileNotFoundError(
            f"Flood hazard map not found: {path}. Download {SCENARIOS[scenario].lower().replace('_', '-')}.zip "
            "from data.public.lu and unzip it into data/raw/Flood_hazard/."
        )

    # The file covers the whole country; keep only the area around the reach.
    markers = gpd.read_file(KILOMETRAGE_GPKG, layer=KILOMETRAGE_LAYER).to_crs(2169)
    markers = markers[
        (markers["TOPONYME"] == RIVER_NAME)
        & (markers["KILO_KM"] >= RIVER_BEGIN_KM - KM_TOLERANCE)
        & (markers["KILO_KM"] <= RIVER_END_KM + KM_TOLERANCE)
    ]
    margin = SCENARIO_REACH_M + TRIBUTARY_MAX_KM * 1000.0
    min_x, min_y, max_x, max_y = markers.total_bounds
    bbox = (min_x - margin, min_y - margin, max_x + margin, max_y + margin)

    extent = gpd.read_file(path).to_crs(2169).clip(bbox)
    return extent.unary_union


def scenario_wse(scenario: str) -> Dict[int, float]:
    """
    Water-surface elevation per Alzette chainage for a flood scenario.

    Where the official flood extent ends, the water meets the ground, so the terrain height
    along the edge of the extent inside each cell is that cell's water level (median of the samples).
    The profile is smoothed along the river and may not fall going upstream. Cells without
    terrain samples (outside the DEM) keep the offset of their nearest sampled neighbours
    from the gauge profile, so the river slope carries on.

    Parameters
    ----------
    scenario : str
        "hq10", "hq100" or "hqextreme".

    Returns
    -------
    Dict[int, float]
        For each Alzette chainage, the elevation in metres (NG-L). Tributary cells
        use the value at their confluence, as for the gauge levels.
    """
    return dict(_scenario_profile(scenario))


@functools.lru_cache(maxsize=len(SCENARIOS))
def _scenario_profile(scenario: str) -> tuple:
    if not TERRAIN_DEM.is_file():
        raise FileNotFoundError(f"Terrain DEM not found: {TERRAIN_DEM}")

    cells = water_cells(DEFAULT_CORRIDOR_M, scenario).to_crs(2169)
    cells = cells[cells["river"] == RIVER_NAME].dissolve(by="chainage_m")
    edge = flood_extent(scenario).boundary

    with rasterio.open(TERRAIN_DEM) as dem:
        levels = {}
        for chainage, cell in cells.geometry.items():
            lines = edge.intersection(cell)
            parts = getattr(lines, "geoms", [lines])
            points = [
                part.interpolate(distance)
                for part in parts if part.geom_type == "LineString"
                for distance in np.arange(0.0, part.length, SCENARIO_SAMPLE_STEP_M)
            ]
            if not points:
                levels[chainage] = np.nan
                continue
            coords = gpd.GeoSeries(points, crs=2169).to_crs(dem.crs)
            heights = np.array([value[0] for value in dem.sample([(p.x, p.y) for p in coords])], dtype="float64")
            if dem.nodata is not None:
                heights[heights == dem.nodata] = np.nan
            levels[chainage] = np.nanmedian(heights) if np.isfinite(heights).any() else np.nan

    chainages = np.array(sorted(levels))
    kms = chainages / 1000.0
    baseline = np.array([wse_from_gauges(km, 0.0, 0.0) for km in kms])
    offset = pd.Series([levels[c] for c in chainages]) - baseline
    if offset.notna().sum() == 0:
        raise ValueError(f"No terrain under the {scenario.upper()} flood extent; check TERRAIN_DEM.")
    offset = offset.rolling(SCENARIO_SMOOTHING_CELLS, center=True, min_periods=1).median()
    known = offset.notna().to_numpy()
    offset = np.interp(kms, kms[known], offset.to_numpy()[known])
    # Chainage grows upstream, and so must the water level.
    wse = np.maximum.accumulate(baseline + offset)
    return tuple(zip(chainages.tolist(), wse.tolist()))


# ============================================================
# WATER-SURFACE ELEVATION
# ============================================================

def _is_stage(value) -> bool:
    return value is not None and not (isinstance(value, float) and np.isnan(value))


def wse_from_gauges(
    river_km: float,
    pfaffenthal_cm: float | None,
    hesperange_cm: float | None,
    livange_cm: float | None = None,
) -> float:
    """
    Water-surface elevation at a river km from the gauge stages.

    WSE = gauge zero + stage, each interpolated by river km between the neighbouring gauges
    (Pfaffenthal km 35.04, Hesperange km 45.35, Livange km 54.34): each gauge moves the water
    only on the sections next to it. Between Pfaffenthal and Hesperange this is the same
    interpolation as derive_water_level_interpolation. Beyond the outer gauges, and across a
    gauge without a reading (None), the stage of the nearest gauge with a reading is held.

    Parameters
    ----------
    river_km : float
        River kilometre of the location.
    pfaffenthal_cm, hesperange_cm, livange_cm : float | None
        Stage at each gauge, in cm above its gauge zero; None when unknown.

    Returns
    -------
    float
        Water-surface elevation in metres (NG-L).
    """
    return float(_wse_profile(np.array([river_km]), pfaffenthal_cm, hesperange_cm, livange_cm)[0])


_GAUGE_KMS = np.array([PFAFFENTHAL_KM, HESPERANGE_KM, LIVANGE_KM])
_GAUGE_ZEROS = np.array([PFAFFENTHAL_GAUGE_ZERO_M, HESPERANGE_GAUGE_ZERO_M, LIVANGE_GAUGE_ZERO_M])


def _wse_profile(river_kms: np.ndarray, pfaffenthal_cm, hesperange_cm, livange_cm) -> np.ndarray:
    """wse_from_gauges for many river kms at once."""
    kms, zeros = _GAUGE_KMS, _GAUGE_ZEROS
    # The gauge zeros follow the river bed; extend their slope past the outer gauges.
    zero = np.interp(river_kms, kms, zeros)
    below, above = river_kms < kms[0], river_kms > kms[-1]
    zero[below] = zeros[0] + (river_kms[below] - kms[0]) * (zeros[1] - zeros[0]) / (kms[1] - kms[0])
    zero[above] = zeros[-1] + (river_kms[above] - kms[-1]) * (zeros[-1] - zeros[-2]) / (kms[-1] - kms[-2])

    known = [(km, stage) for km, stage in zip(kms, (pfaffenthal_cm, hesperange_cm, livange_cm)) if _is_stage(stage)]
    if not known:
        raise ValueError("No gauge stage given.")
    stage = np.interp(river_kms, [km for km, _ in known], [s for _, s in known])
    return zero + stage / 100.0


def gauge_positions() -> list:
    """
    The Pfaffenthal, Hesperange and Livange gauges on the Alzette kilometrage line.

    Returns
    -------
    list
        One dict per gauge: name, river_km, lon, lat, heading_deg (downstream direction, clockwise
        from north) and height_m (gauge zero as drawn on the 3D terrain: plus terrain_height_offset).
    """
    if not KILOMETRAGE_GPKG.is_file():
        raise FileNotFoundError(f"River kilometrage file not found: {KILOMETRAGE_GPKG}")
    markers = gpd.read_file(KILOMETRAGE_GPKG, layer=KILOMETRAGE_LAYER).to_crs(2169)
    markers = markers[markers["TOPONYME"] == RIVER_NAME].sort_values("KILO_KM").reset_index(drop=True)
    kms = markers["KILO_KM"].to_numpy()
    xs, ys = markers.geometry.x.to_numpy(), markers.geometry.y.to_numpy()
    result = []
    for name, km, zero in (("Pfaffenthal", PFAFFENTHAL_KM, PFAFFENTHAL_GAUGE_ZERO_M),
                           ("Hesperange", HESPERANGE_KM, HESPERANGE_GAUGE_ZERO_M),
                           ("Livange", LIVANGE_KM, LIVANGE_GAUGE_ZERO_M)):
        x, y = np.interp(km, kms, xs), np.interp(km, kms, ys)
        # Downstream is towards lower km.
        dx = np.interp(km - 0.1, kms, xs) - np.interp(km + 0.1, kms, xs)
        dy = np.interp(km - 0.1, kms, ys) - np.interp(km + 0.1, kms, ys)
        point = gpd.GeoSeries(gpd.points_from_xy([x], [y]), crs=2169).to_crs(4326).iloc[0]
        result.append({
            "name": name, "river_km": km, "lon": round(point.x, 7), "lat": round(point.y, 7),
            "heading_deg": round(float(np.degrees(np.arctan2(dx, dy))) % 360.0, 1),
            "height_m": round(zero + float(terrain_height_offset(np.array([x]), np.array([y]))[0]), 2),
        })
    return result


def rendered_gauge_wse(wse: np.ndarray) -> np.ndarray:
    """
    Water level drawn in Terria for a gauge-derived elevation: the one place where the water render
    offset is applied.

    Every gauge level shown (live, manual, day replay) passes through here, and so does the
    level the 3D buildings are coloured by, so both stay aligned with the water on screen.
    TERRAIN_HEIGHT_OFFSET_M (the datum shift shared with the terrain) is added on top in build_czml.

    Parameters
    ----------
    wse : np.ndarray
        Water-surface elevations in metres (NG-L) from wse_from_gauges / _wse_profile.

    Returns
    -------
    np.ndarray
        The elevations plus WATER_RENDER_OFFSET_M.
    """
    return wse + WATER_RENDER_OFFSET_M


def _never_rising_downstream(river_kms: np.ndarray, wse: np.ndarray) -> np.ndarray:
    """The profile raised where needed so the level never falls going upstream (river km grows upstream)."""
    order = np.argsort(river_kms, kind="stable")
    result = wse.copy()
    result[order] = np.maximum.accumulate(wse[order])
    return result


def wse_for_levels(
    chainages: Sequence[int],
    pfaffenthal_cm: Sequence[float],
    hesperange_cm: Sequence[float],
    livange_cm: Sequence[float] | None = None,
) -> Dict[int, List[float]]:
    """
    Water-surface elevation per chainage for a sequence of gauge stages.

    Parameters
    ----------
    chainages : Sequence[int]
        River chainages in metres.
    pfaffenthal_cm, hesperange_cm, livange_cm : Sequence[float]
        Gauge stages, one per keyframe. A keyframe may instead name a flood
        scenario ("hq10", "hq100", "hqextreme") in every sequence, for that scenario's level.
        Without livange_cm, the Hesperange stage is held upstream of Hesperange.

    Returns
    -------
    Dict[int, List[float]]
        For each chainage, one elevation per keyframe, as drawn: gauge keyframes include
        WATER_RENDER_OFFSET_M (see rendered_gauge_wse), scenario keyframes do not.
    """
    if livange_cm is None:
        livange_cm = [None] * len(pfaffenthal_cm)
    chainages = [int(chainage) for chainage in chainages]
    kms = np.array(chainages) / 1000.0
    # One profile along the river per keyframe.
    keyframes = []
    for pf, he, li in zip(pfaffenthal_cm, hesperange_cm, livange_cm):
        if isinstance(pf, str) and pf in SCENARIOS:
            profile = scenario_wse(pf)
            keyframes.append([profile[chainage] for chainage in chainages])
        else:
            keyframes.append(_never_rising_downstream(kms, rendered_gauge_wse(_wse_profile(kms, pf, he, li))).tolist())
    return {chainage: [keyframe[i] for keyframe in keyframes] for i, chainage in enumerate(chainages)}


def gauge_stages(start_iso: str, end_iso: str) -> tuple:
    """
    Stages of the three gauges for a period, from water_level_history
    (filled by the gauge_collector service and by load_water_level_period).

    Each stored reading time is a keyframe. A gauge without a reading at that time is
    interpolated in time from its neighbouring readings; a gauge without any reading in
    the period gets None throughout.

    Parameters
    ----------
    start_iso, end_iso : str
        Period bounds, ISO 8601.

    Returns
    -------
    tuple
        (times, stages): the sorted timestamps (UTC), and per station id (PFAFFENTHAL_ID,
        HESPERANGE_ID, LIVANGE_ID) one stage in cm per timestamp. Both empty without readings.
    """
    engine = setup_environment.get_database()
    query = text(
        f"""
        SELECT datetime, station_id, water_level_cm
        FROM {HISTORY_TABLE}
        WHERE station_id IN (:pf_id, :he_id, :li_id)
            AND datetime BETWEEN CAST(:start AS timestamptz) AND CAST(:end AS timestamptz)
        """
    )
    params = {"pf_id": PFAFFENTHAL_ID, "he_id": HESPERANGE_ID, "li_id": LIVANGE_ID, "start": start_iso, "end": end_iso}
    with engine.connect() as conn:
        if not sa_inspect(conn).has_table(HISTORY_TABLE):
            return [], {}
        rows = pd.read_sql(query, conn, params=params)

    if rows.empty:
        return [], {}

    table = rows.pivot_table(index="datetime", columns="station_id", values="water_level_cm").sort_index()
    # Fill gaps along time so every gauge with readings has a value at every keyframe.
    table = table.interpolate(limit_direction="both")
    stages = {
        station: table[station].tolist() if station in table else [None] * len(table)
        for station in (PFAFFENTHAL_ID, HESPERANGE_ID, LIVANGE_ID)
    }
    times = [pd.Timestamp(t).tz_convert("UTC") for t in table.index]
    return times, stages


def wse_history(start_iso: str, end_iso: str) -> tuple:
    """
    Water-surface elevation for a period, from the stored gauge readings (see gauge_stages).

    A gauge without any reading in the period is left out: wse_from_gauges then holds the
    stage of the nearest other gauge.

    Parameters
    ----------
    start_iso, end_iso : str
        Period bounds, ISO 8601.

    Returns
    -------
    tuple
        (times, wse): the sorted timestamps, and for each chainage one elevation per timestamp.
        Both empty when none of the gauges has a reading in the period.
    """
    times, stages = gauge_stages(start_iso, end_iso)
    if not times:
        return [], {}
    chainages = water_cells(DEFAULT_CORRIDOR_M)["chainage_m"].unique()
    wse = wse_for_levels(chainages, stages[PFAFFENTHAL_ID], stages[HESPERANGE_ID], stages[LIVANGE_ID])
    return times, wse


def latest_gauge_levels() -> list:
    """
    Latest stored reading of every gauge in water_level_history.

    The table is filled by the gauge_collector service (and by load_water_level_period).

    Returns
    -------
    list
        One dict per station: station_id, station_name, river_name, datetime (ISO 8601),
        water_level_cm and alert_status. Empty when nothing is stored yet.
    """
    engine = setup_environment.get_database()
    query = text(
        f"""
        SELECT DISTINCT ON (station_id)
            station_id, station_name, river_name, datetime, water_level_cm, alert_status
        FROM {HISTORY_TABLE}
        ORDER BY station_id, datetime DESC
        """
    )
    with engine.connect() as conn:
        if not sa_inspect(conn).has_table(HISTORY_TABLE):
            return []
        rows = conn.execute(query).mappings().all()
    return [
        {**row, "datetime": pd.Timestamp(row["datetime"]).tz_convert("UTC").isoformat()}
        for row in rows
    ]


# ============================================================
# CZML
# ============================================================

def _iso(timestamp: pd.Timestamp) -> str:
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_czml(
    times: Sequence[pd.Timestamp],
    wse: Dict[int, List[float]],
    corridor_m: float = DEFAULT_CORRIDOR_M,
    multiplier: float = 1.0,
    hold_s: float = 0.0,
    scenario: str | None = None,
) -> list:
    """
    Build the CZML document of the water surface: its timeline.

    The water itself is drawn by terriajs/water-level-control.js from /water-surface-mesh.json
    (the same query), as a mesh Terria's CZML cannot style with an animated water material;
    this document gives Terria the clock it follows, so the map script shows the level of each
    keyframe at its time. wse, corridor_m and scenario are not needed for that.

    Parameters
    ----------
    times : Sequence[pd.Timestamp]
        Keyframe times (UTC), ascending.
    wse : Dict[int, List[float]]
        For each chainage, the water-surface elevation (NG-L) at every keyframe.
    corridor_m : float
        Half-width in metres of the band around the river that may hold water.
    multiplier : float
        Playback speed of the Terria timeline, in simulated seconds per real second.
    hold_s : float
        Seconds the last level stays on the timeline after the last keyframe. Terria loops its clock,
        so without a hold a manual level change would replay its animation every few seconds.
    scenario : str | None
        Flood scenario whose official extent widens the area that may hold water (see water_cells).

    Returns
    -------
    list
        CZML packets, ready for json serialisation.
    """
    epoch = times[0]
    end = times[-1] + pd.Timedelta(seconds=hold_s)
    if end <= epoch:
        end = epoch + pd.Timedelta(seconds=1)

    czml = [{
        "id": "document",
        "name": "Alzette 3D water surface",
        "version": "1.0",
        "clock": {
            "interval": f"{_iso(epoch)}/{_iso(end)}",
            "currentTime": _iso(epoch),
            "multiplier": multiplier,
            "range": "CLAMPED",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]
    return czml
