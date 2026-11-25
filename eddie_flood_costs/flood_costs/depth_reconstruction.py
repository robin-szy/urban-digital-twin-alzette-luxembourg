"""Provisional HQ10/HQ100 reconstruction using the validated local methodology.

Inputs and output stages are configured externally; no data is read on import.
Frozen reference functions are embedded below and compiled in the same isolated
namespace as the original script, without importing teammates' modules.
"""
import os
os.environ["PROJ_NETWORK"] = "OFF"
os.environ["GDAL_PAM_ENABLED"] = "NO"
os.environ["OGR_GEOJSON_MAX_OBJ_SIZE"] = "0"

import ast
import csv
import ctypes as C
import functools
import gzip
import hashlib
import json
import pathlib
import warnings
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import shapely
from shapely import vectorized
from shapely.geometry import LineString, MultiPoint, mapping, shape
from shapely.ops import unary_union, voronoi_diagram
from shapely.prepared import prep

from . import WorkflowPaths, gdal_library_path, SCENARIOS, LEGACY_SCENARIOS, SCENARIO_PAIRS

COMMIT = "769cfc4ed85f7f56caca951807de4e2d0d9dc863"
STEP = 1.0
TOLERANCE = 1e-6


def configure(paths=None):
    """Resolve external paths when invoked; importing this module opens no data."""
    global ROOT, OUT, DEM_PATH, BUILDINGS_PATH, MARKERS_PATH, HAZARD_PATHS, PRODUCTS
    global baseline_path, verify_baseline, baseline_provenance, portable_path
    paths = paths or WorkflowPaths()
    baseline_path, verify_baseline = paths.baseline_path, paths.verify_baseline
    baseline_provenance, portable_path = paths.baseline_provenance, paths.portable_path
    ROOT = paths.project_root
    OUT = paths.stage_path('depth')
    DEM_PATH = paths.input_path('dem')
    BUILDINGS_PATH = paths.input_path('buildings')
    MARKERS_PATH = paths.input_path('markers')
    HAZARD_PATHS = {s: paths.input_path(s) for s in SCENARIOS}
    PRODUCTS = [OUT / n for n in (
        "building_reconstructed_depths.geojson", "building_reconstructed_depths.csv",
        "building_depth_samples.csv.gz", "validation_report.json",
    )]
    return paths


def emit(label, **values):
    print(json.dumps({"stage": label, **values}, allow_nan=False), flush=True)


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


class WindowedDEM:
    """Read-only GDAL-backed subset; sample semantics match nearest-pixel DEM.sample.

    Uses the GDAL library already bundled with pyogrio. No dependency installation,
    raster mutation, display elevation offset, reprojection or resampling occurs.
    """
    def __init__(self, path, bounds):
        # EDDIE has no pyogrio: FLOOD_COSTS_GDAL_LIBRARY names the image's GDAL (docker-compose.override.yml)
        lib_path = gdal_library_path(gpd.__file__)
        self.lib = C.CDLL(str(lib_path))
        self.path = pathlib.Path(path).resolve()
        self.lib.GDALAllRegister()
        self.ds = self.fn("GDALOpen", C.c_void_p, [C.c_char_p, C.c_int])(str(path).encode(), 0)
        if not self.ds:
            raise RuntimeError(f"Cannot open DEM read-only: {path}")
        ptr = C.c_void_p
        self.band = self.fn("GDALGetRasterBand", ptr, [ptr, C.c_int])(self.ds, 1)
        self.width = self.fn("GDALGetRasterXSize", C.c_int, [ptr])(self.ds)
        self.height = self.fn("GDALGetRasterYSize", C.c_int, [ptr])(self.ds)
        gt = (C.c_double * 6)()
        assert self.fn("GDALGetGeoTransform", C.c_int, [ptr, C.POINTER(C.c_double)])(self.ds, gt) == 0
        self.gt = list(gt)
        if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
            raise RuntimeError("Verification adapter requires a north-up DEM")
        wkt = self.fn("GDALGetProjectionRef", C.c_char_p, [ptr])(self.ds).decode()
        self.crs = pyproj.CRS.from_wkt(wkt)
        if self.crs.to_epsg() != 2169:
            raise RuntimeError("Local DEM has an unexpected CRS")
        has_nodata = C.c_int()
        nd = self.fn("GDALGetRasterNoDataValue", C.c_double, [ptr, C.POINTER(C.c_int)])(self.band, C.byref(has_nodata))
        self.nodata = nd if has_nodata.value else None
        self.x0 = max(0, int(np.floor((bounds[0] - gt[0]) / gt[1])) - 1)
        self.x1 = min(self.width, int(np.ceil((bounds[2] - gt[0]) / gt[1])) + 1)
        self.y0 = max(0, int(np.floor((gt[3] - bounds[3]) / -gt[5])) - 1)
        self.y1 = min(self.height, int(np.ceil((gt[3] - bounds[1]) / -gt[5])) + 1)
        self.data = np.empty((self.y1 - self.y0, self.x1 - self.x0), dtype=np.float32)
        if self.data.size > 20_000_000:
            raise RuntimeError("Scoped DEM window exceeds the 20-million-pixel guard")
        io = self.fn("GDALRasterIO", C.c_int, [ptr, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int,
                                              ptr, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int])
        if io(self.band, 0, self.x0, self.y0, self.x1-self.x0, self.y1-self.y0,
              self.data.ctypes.data, self.data.shape[1], self.data.shape[0], 6, 0, 0) != 0:
            raise RuntimeError("GDAL window read failed")
        if self.nodata is not None and np.isfinite(self.nodata):
            self.data[self.data == self.nodata] = np.nan
        self.data[~np.isfinite(self.data)] = np.nan
        self.metadata = {
            "path": str(path), "crs": "EPSG:2169", "projection_wkt": wkt,
            "horizontal_unit": self.crs.axis_info[0].unit_name,
            "raster_size": [self.width, self.height], "pixel_size": [gt[1], -gt[5]],
            "nodata": "NaN" if self.nodata is not None and np.isnan(self.nodata) else self.nodata,
            "band_unit": self.fn("GDALGetRasterUnitType", C.c_char_p, [ptr])(self.band).decode(),
            "dataset_tags": self.tags(self.ds), "band_tags": self.tags(self.band),
            "window_indices": [self.x0, self.y0, self.x1, self.y1],
            "window_bounds_epsg2169": [gt[0]+self.x0*gt[1], gt[3]+self.y1*gt[5],
                                      gt[0]+self.x1*gt[1], gt[3]+self.y0*gt[5]],
            "window_pixels": int(self.data.size),
            "window_missing_pixels": int(np.isnan(self.data).sum()),
            "vertical_datum_verified": False, "elevation_unit_assumed": "metre",
        }

    def fn(self, name, result, args):
        f = getattr(self.lib, name)
        f.restype, f.argtypes = result, args
        return f

    def tags(self, handle):
        arr = self.fn("GDALGetMetadata", C.POINTER(C.c_char_p), [C.c_void_p, C.c_char_p])(handle, None)
        result, i = [], 0
        if arr:
            while arr[i]:
                result.append(arr[i].decode())
                i += 1
        return result

    def values(self, xy):
        xy = np.asarray(xy, dtype=float)
        if not len(xy):
            return np.empty(0)
        cols = np.floor((xy[:, 0] - self.gt[0]) / self.gt[1]).astype(int) - self.x0
        rows = np.floor((xy[:, 1] - self.gt[3]) / self.gt[5]).astype(int) - self.y0
        valid = (rows >= 0) & (rows < self.data.shape[0]) & (cols >= 0) & (cols < self.data.shape[1])
        result = np.full(len(xy), np.nan)
        result[valid] = self.data[rows[valid], cols[valid]]
        return result

    def sample(self, points):
        return (np.array([v]) for v in self.values(list(points)))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        self.fn("GDALClose", None, [C.c_void_p])(self.ds)


# Frozen reference definitions selected from the validated research snapshots.
# Only these ten functions are needed. Their bodies and decorators are unchanged;
# original module imports, deployment paths and visualization code are excluded.
_REFERENCE_SOURCES = {
    'water_surface.py': r'''
@functools.lru_cache(maxsize=8)
def water_cells(corridor_m: float = DEFAULT_CORRIDOR_M, scenario: str | None = None) -> gpd.GeoDataFrame:
    """
    Split the river corridor into one cell per kilometrage marker.

    Parameters
    ----------
    corridor_m : float
        Half-width in metres of the band around the river that may hold water.
    scenario : str | None
        Flood scenario ("hq10", "hq100") whose official extent, up to SCENARIO_REACH_M
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
        "hq10" or "hq100".

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

''',
    'buildings_3d.py': r'''
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


def _extent_status(geometries: gpd.GeoSeries, extent) -> pd.Series:
    """Flood status of each 2D footprint against a flood extent: none, touched or fully inside."""
    prepared = prep(extent)
    return geometries.apply(
        lambda geom: FLOOD_FULL if prepared.contains(geom) else FLOOD_TOUCHED if prepared.intersects(geom) else FLOOD_NONE
    )


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

''',
}
_REFERENCE_SOURCE_SHA256 = {'water_surface.py': '875b9735997c7daa960b0b81cf60a7a3bb99de99acec15c68d1fb875e013b818', 'buildings_3d.py': '0d7b8352ea098121888c826c99266e5a22feff47cd72defc5a342b48f0bcc926'}
_GAUGE_CONSTANTS = {'PFAFFENTHAL_KM': 35.04, 'PFAFFENTHAL_GAUGE_ZERO_M': 235.25, 'HESPERANGE_KM': 45.35, 'HESPERANGE_GAUGE_ZERO_M': 255.74, 'LIVANGE_KM': 54.34, 'LIVANGE_GAUGE_ZERO_M': 265.8}


def load_reference_functions():
    constants = dict(_GAUGE_CONSTANTS)
    ns = dict(gpd=gpd, np=np, pd=pd, functools=functools, Dict=Dict, List=List,
              Sequence=Sequence, LineString=LineString, MultiPoint=MultiPoint,
              voronoi_diagram=voronoi_diagram, vectorized=vectorized, prep=prep,
              KILOMETRAGE_GPKG=MARKERS_PATH, KILOMETRAGE_LAYER="Kilometrierung primäre Gewässer",
              TRIBUTARY_LAYER="Kilometrierung sekundäre Gewässer", RIVER_NAME="Alzette",
              RIVER_BEGIN_KM=35.0, RIVER_END_KM=50.0, KM_TOLERANCE=1e-3,
              TRIBUTARY_MOUTH_MAX_M=400.0, TRIBUTARY_MAX_KM=2.0, TRIBUTARY_CORRIDOR_M=150.0,
              DEFAULT_CORRIDOR_M=300.0, SCENARIO_REACH_M=1000.0,
              SCENARIO_SAMPLE_STEP_M=5.0, SCENARIO_SMOOTHING_CELLS=5,
              BUILDINGS_CORRIDOR_M=500.0, TERRAIN_DEM=DEM_PATH,
              SCENARIOS={"hq10":"HWGK2021_HQ10", "hq100":"HWGK2021_HQ100", "hqextreme":"HWGK2021_HQext"},
              FLOOD_NONE=0, FLOOD_TOUCHED=1, FLOOD_FULL=2, **constants)
    ns["_GAUGE_KMS"] = np.array([constants[k] for k in ("PFAFFENTHAL_KM", "HESPERANGE_KM", "LIVANGE_KM")])
    ns["_GAUGE_ZEROS"] = np.array([constants[k] for k in ("PFAFFENTHAL_GAUGE_ZERO_M", "HESPERANGE_GAUGE_ZERO_M", "LIVANGE_GAUGE_ZERO_M")])
    selected = {
        "water_surface.py": {"water_cells", "_tributary_cells", "scenario_wse", "_scenario_profile", "wse_from_gauges", "_wse_profile", "_is_stage"},
        "buildings_3d.py": {"study_area", "_footprint_points", "_extent_status"},
    }
    hashes = {}
    for file, names in selected.items():
        text = _REFERENCE_SOURCES[file]
        hashes[file] = _REFERENCE_SOURCE_SHA256[file]
        tree = ast.parse(text)
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        if {n.name for n in functions} != names:
            raise RuntimeError("Missing expected reference functions")
        # Compile original AST nodes without modifying their bodies or decorators.
        exec(compile(ast.Module(body=functions, type_ignores=[]), f"<embedded reference {file}>", "exec"), ns)
    return ns, hashes


def crop_around_markers(markers):
    """Luiz's 3,000 m marker-bounds crop of the scenario extents."""
    x0,y0,x1,y1 = markers.total_bounds
    return shapely.box(x0-3000, y0-3000, x1+3000, y1+3000)


def load_local_extent(scenario, markers):
    """Equivalent extent input, adapted from hardcoded SHP to existing GeoJSON.

    Preserve Luiz's 3,000 m marker-bounds crop. Before union/intersection, select
    only components whose bounding boxes overlap that crop.
    """
    path = HAZARD_PATHS[scenario]
    with path.open() as f:
        doc = json.load(f)
    if doc.get("crs", {}).get("properties", {}).get("name") != "urn:ogc:def:crs:EPSG::2169":
        raise RuntimeError(f"Unexpected hazard CRS: {path}")
    crop = crop_around_markers(markers)
    selected = []
    for feature in doc["features"]:
        components = shapely.get_parts(shape(feature["geometry"]))
        bb = shapely.bounds(components)
        cb = crop.bounds
        keep = (bb[:,0] <= cb[2]) & (bb[:,2] >= cb[0]) & (bb[:,1] <= cb[3]) & (bb[:,3] >= cb[1])
        selected.extend(shapely.intersection(components[keep], crop))
    result = unary_union(selected)
    if result.is_empty or not result.is_valid:
        raise RuntimeError(f"Invalid local scenario extent: {scenario}")
    return result


def assign_cells(points, cells):
    """Strict within first; accept a boundary only when its chainage is unique.

    Several cell fragments with the same chainage have an identical WSE. Multiple
    distinct chainages are genuinely ambiguous and remain unassigned/null.
    """
    n = len(points)
    assigned = np.full(n, -1, dtype=np.int64)
    kinds = np.full(n, "outside_water_cells", dtype=object)
    left = gpd.GeoDataFrame(geometry=gpd.points_from_xy(points[:,0], points[:,1]), crs=2169)
    def apply(joined, normal):
        matched = joined[joined.chainage_m.notna()]
        for index, rows in matched.groupby(level=0):
            chains = rows.chainage_m.unique()
            if len(chains) == 1:
                assigned[index] = int(chains[0])
                kinds[index] = normal
            else:
                kinds[index] = "ambiguous_chainage"
    apply(gpd.sjoin(left, cells[["chainage_m", "geometry"]], how="left", predicate="within"), "within")
    remaining = np.flatnonzero((assigned < 0) & (kinds != "ambiguous_chainage"))
    if len(remaining):
        apply(gpd.sjoin(left.iloc[remaining], cells[["chainage_m", "geometry"]], how="left", predicate="covered_by"), "unique_chainage_boundary")
    return assigned, kinds


def summarize_depth(depth, valid, inside, chainage, kinds, ground):
    wet = valid & (depth > 0)
    positive = depth[wet]
    nv = int(valid.sum())
    flags = []
    if np.any(~np.isfinite(ground)): flags.append("missing_ground")
    if np.any(chainage < 0): flags.append("missing_or_ambiguous_water_cell")
    if np.any(kinds == "ambiguous_chainage"): flags.append("ambiguous_chainage")
    if nv == 0: flags.append("no_valid_reconstruction_samples")
    elif not len(positive): flags.append("no_positive_reconstructed_depth")
    if nv < len(valid): flags.append("incomplete_sample_coverage")
    return {
        "min_m": float(positive.min()) if len(positive) else None,
        "mean_m": float(positive.mean()) if len(positive) else None,
        "max_m": float(positive.max()) if len(positive) else None,
        "all_valid_mean_m": float(depth[valid].mean()) if nv else None,
        "wet_sample_fraction": float(wet.sum()/nv) if nv else None,
        "samples_total": len(valid), "samples_valid": nv,
        "samples_missing": int(len(valid)-nv), "samples_wet": int(wet.sum()),
        "samples_dry": int((valid & ~wet).sum()),
        "samples_ground_missing": int((~np.isfinite(ground)).sum()),
        "samples_cell_missing": int((chainage < 0).sum()),
        "samples_inside_official_extent": int(inside.sum()),
        "sample_coverage_fraction": float(nv/len(valid)),
        "quality_flags": "|".join(flags) if flags else "complete",
    }


def distribution(values):
    a = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if not len(a): return {"count":0,"min":None,"mean":None,"median":None,"p95":None,"max":None}
    return {"count":len(a),"min":float(a.min()),"mean":float(a.mean()),
            "median":float(np.median(a)),"p95":float(np.quantile(a,.95)),"max":float(a.max())}


class FileInputs:
    """Markers, scenario extents and footprints from the configured source files."""

    def __init__(self, km_start=35.0, km_end=50.0):
        # AOI change (Robin, 2026-11-20): the km range is a parameter; defaults = the old 35-50.
        self.km_start, self.km_end = km_start, km_end

    def bind(self, namespace):
        """The frozen functions already read the marker file named in their namespace."""

    def markers(self, layer):
        # Was the fixed "KILO_KM >= 34.999 AND KILO_KM <= 50.001": the same 0.001 km tolerance
        # either side, now around the requested range (AOI change).
        return gpd.read_file(MARKERS_PATH, layer=layer,
            where=f"TOPONYME = 'Alzette' AND KILO_KM >= {self.km_start - 0.001} "
                  f"AND KILO_KM <= {self.km_end + 0.001}").sort_values("KILO_KM")

    def extent(self, scenario, markers):
        return load_local_extent(scenario, markers)

    def buildings(self, study):
        return gpd.read_file(BUILDINGS_PATH,
            layer="ACT2023v2_Buildings3d_Footprints", bbox=study.bounds)

    def provenance(self):
        return {str(p):{"bytes":p.stat().st_size,"mtime_ns":p.stat().st_mtime_ns}
                for p in [BUILDINGS_PATH,MARKERS_PATH,*HAZARD_PATHS.values()]}


def main(inputs=None, km_start=35.0, km_end=50.0):
    # AOI change (Robin, 2026-11-20): km_start / km_end select the river stretch (the reach
    # the user selected in the twin). Defaults are the old fixed 35-50, so main() alone runs
    # exactly as before.
    inputs = inputs or FileInputs(km_start, km_end)
    paths = configure()
    paths.guard_output(OUT)
    if any(p.exists() for p in PRODUCTS):
        raise RuntimeError("An output product already exists; refusing to overwrite")
    baseline_features, baseline_samples, baseline_report = {}, {}, None
    # AOI change: a comparison baseline was computed for km 35-50, so it is only reused for
    # that same stretch. For any other reach its saved water-surface profile and samples
    # would belong to the wrong river kilometres, and the "comparison cohort is missing"
    # assertion below would fail for every reach that leaves one of those buildings out.
    if paths.baseline_root and (km_start, km_end) == (35.0, 50.0):
        verify_baseline()
        baseline_geo = json.loads((baseline_path('depth')/'building_reconstructed_depths.geojson').read_text())
        baseline_features = {f['properties']['buildingid']: f for f in baseline_geo['features']}
        baseline_report = json.loads((baseline_path('depth')/'validation_report.json').read_text())
        baseline_samples = {}
        with gzip.open(baseline_path('depth')/'building_depth_samples.csv.gz', 'rt', newline='') as handle:
            for row in csv.DictReader(handle):
                baseline_samples.setdefault((row['buildingid'], row['scenario']), []).append(row)
    OUT.mkdir(parents=True,exist_ok=True)
    warnings.filterwarnings("ignore", message="Measured.*")
    ns, reference_hashes = load_reference_functions()
    # AOI change (Robin, 2026-11-20): run the frozen functions for the requested km range. They
    # read RIVER_BEGIN_KM / RIVER_END_KM from this namespace when called, so overriding the two
    # names here is all it takes; the functions themselves are untouched (SHA-256 check holds).
    # With the defaults (35.0, 50.0) this sets exactly the values they already had.
    ns["RIVER_BEGIN_KM"], ns["RIVER_END_KM"] = km_start, km_end
    inputs.bind(ns)
    markers = inputs.markers(ns["KILOMETRAGE_LAYER"])
    extents = {s: inputs.extent(s, markers) for s in SCENARIOS}
    ns["flood_extent"] = lambda scenario: extents[scenario]
    original_study = ns["study_area"]()
    # Same corridor construction and km35-50 markers, widened for the third extent.
    study = unary_union([original_study, unary_union(ns['water_cells'](500.0, 'hqextreme').to_crs(2169).geometry)])
    cells = {s:ns["water_cells"](300.0, s).to_crs(2169) for s in SCENARIOS}
    emit("study_constructed", area_km2=study.area/1e6, bounds=study.bounds)
    buildings = inputs.buildings(study)
    bbox_rows = len(buildings)
    buildings["geometry"] = shapely.force_2d(buildings.geometry)
    buildings = buildings[buildings.intersects(study)].copy()
    if buildings.buildingid.isna().any() or (buildings.buildingid.astype(str).str.len()==0).any():
        raise RuntimeError("Missing building IDs")
    if not buildings.is_valid.all() or buildings.is_empty.any():
        raise RuntimeError("Invalid building geometry; no automatic repairs permitted")
    duplicate_rows = int(buildings.buildingid.duplicated().sum())
    if duplicate_rows:
        # One physical footprint per source ID, with overlapping parts counted once.
        buildings = buildings.dissolve(by="buildingid", as_index=False, aggfunc="first")
    buildings = buildings.sort_values("buildingid").reset_index(drop=True)
    for s in SCENARIOS:
        buildings[f"exposure_{s}_code"] = ns["_extent_status"](buildings.geometry, extents[s])
    selected = buildings[buildings[[f"exposure_{s}_code" for s in SCENARIOS]].gt(0).any(axis=1)
                         | buildings.buildingid.isin(baseline_features)].copy().reset_index(drop=True)
    assert set(baseline_features) <= set(selected.buildingid), 'Original comparison cohort is missing'
    for _, building in selected[selected.buildingid.isin(baseline_features)].iterrows():
        assert abs(building.geometry.area-baseline_features[building.buildingid]['properties']['footprint_area_m2']) < 1e-7
    geom_bounds = np.array([study.bounds, *[c.total_bounds for c in cells.values()], selected.total_bounds])
    bounds = [geom_bounds[:,0].min()-5, geom_bounds[:,1].min()-5,
              geom_bounds[:,2].max()+5, geom_bounds[:,3].max()+5]
    dem = WindowedDEM(DEM_PATH, bounds)
    def open_dem(path):
        if pathlib.Path(path).resolve() != DEM_PATH.resolve():
            raise RuntimeError("Unexpected raster request")
        return dem
    ns["rasterio"] = SimpleNamespace(open=open_dem)
    profiles = {s: ({int(row['chainage_m']): row['scenario_wse_m']
                     for row in baseline_report['scenarios'][s]['profile']}
                    if baseline_report and s in LEGACY_SCENARIOS else ns['scenario_wse'](s))
                for s in SCENARIOS}
    chainages = sorted(set(profiles["hq10"]) & set(profiles["hq100"]))
    profile_bad = [c for c in chainages if profiles["hq100"][c]+TOLERANCE < profiles["hq10"][c]]
    emit("profiles_reconstructed", screened_buildings=len(buildings), output_buildings=len(selected),
         window_pixels=int(dem.data.size), profile_order_violations=len(profile_bad))
    records = []
    sample_fields = ["buildingid","part_index","sample_index","x_epsg2169","y_epsg2169",
                     "ground_elevation_m","scenario","official_extent_contains_sample",
                     "chainage_m","water_cell_assignment","scenario_wse_m",
                     "reconstruction_valid","raw_terrain_depth_m","official_masked_depth_m","wet"]
    sample_totals = {s:Counter() for s in SCENARIOS}
    comparison = Counter()
    pair_comparisons = {f"{high}_vs_{low}": Counter() for low, high in SCENARIO_PAIRS}
    sample_positive = {s:[] for s in SCENARIOS}
    with gzip.open(PRODUCTS[2], "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sample_fields)
        writer.writeheader()
        for index, building in selected.iterrows():
            polygon_parts = list(building.geometry.geoms) if building.geometry.geom_type == "MultiPolygon" else [building.geometry]
            pts, part_indices = [], []
            for part_index, polygon in enumerate(polygon_parts):
                if polygon.geom_type != "Polygon":
                    raise RuntimeError("Unexpected non-polygon building part")
                p = ns["_footprint_points"](polygon, STEP)
                pts.append(p)
                part_indices.append(np.full(len(p), part_index, dtype=int))
            xy, part_ids = np.concatenate(pts), np.concatenate(part_indices)
            ground = dem.values(xy)
            bid = str(building.buildingid)
            if bid in baseline_features:
                saved_points = baseline_samples[(bid, LEGACY_SCENARIOS[0])]
                saved_xy = np.array([[float(row['x_epsg2169']), float(row['y_epsg2169'])] for row in saved_points])
                assert xy.shape == saved_xy.shape and np.allclose(xy, saved_xy, rtol=0, atol=1e-8)
                xy = saved_xy
                part_ids = np.array([int(row['part_index']) for row in saved_points])
                ground = np.array([float(row['ground_elevation_m']) if row['ground_elevation_m'] else np.nan for row in saved_points])
            record = {"buildingid":str(building.buildingid), "gml_id":str(building.gml_id),
                      "commune":str(building.commune), "lau2":str(building.lau2),
                      "footprint_area_m2":float(building.geometry.area),
                      "footprint_part_count":len(polygon_parts),
                      "ground_min_m":float(np.nanmin(ground)) if np.isfinite(ground).any() else None,
                      "ground_mean_m":float(np.nanmean(ground)) if np.isfinite(ground).any() else None,
                      "depths_provisional":True, "vertical_datum_verified":False,
                      "sample_spacing_m":STEP, "dem_resolution_m":5.0,
                      "depth_method":"Luiz scenario WSE minus DEM; official-extent masked; positive-only summaries"}
            if bid in baseline_features:
                record.update(baseline_features[bid]['properties'])
            record['comparison_cohort'] = bid in baseline_features
            record['classification_review_required'] = bid not in baseline_features
            per_scenario = {}
            for s in SCENARIOS:
                preserved = baseline_samples.get((bid, s))
                if preserved is not None:
                    code = record[f'exposure_{s}_code']
                    assigned = np.array([int(row['chainage_m']) if row['chainage_m'] else -1 for row in preserved])
                    kinds = np.array([row['water_cell_assignment'] for row in preserved])
                    wse = np.array([float(row['scenario_wse_m']) if row['scenario_wse_m'] else np.nan for row in preserved])
                    valid = np.array([row['reconstruction_valid'] == 'True' for row in preserved])
                    inside = np.array([row['official_extent_contains_sample'] == 'True' for row in preserved])
                    raw = np.array([float(row['raw_terrain_depth_m']) if row['raw_terrain_depth_m'] else np.nan for row in preserved])
                    depth = np.array([float(row['official_masked_depth_m']) if row['official_masked_depth_m'] else np.nan for row in preserved])
                else:
                    code = int(building[f"exposure_{s}_code"])
                    record[f"exposure_{s}_code"] = code
                    record[f"exposure_{s}_status"] = {0:"none",1:"touched_or_partial",2:"fully_contained"}[code]
                    overlap = building.geometry.intersection(extents[s]).area
                    record[f"official_overlap_{s}_area_m2"] = float(overlap)
                    record[f"official_overlap_{s}_fraction"] = float(overlap/building.geometry.area)
                    assigned, kinds = assign_cells(xy, cells[s])
                    wse = np.array([profiles[s].get(int(c),np.nan) if c>=0 else np.nan for c in assigned])
                    valid = np.isfinite(ground) & np.isfinite(wse) & (assigned>=0)
                    inside = shapely.contains_xy(extents[s], xy[:,0], xy[:,1])
                    raw = np.full(len(xy),np.nan)
                    raw[valid] = np.maximum(0,wse[valid]-ground[valid])
                    depth = np.full(len(xy),np.nan)
                    depth[valid] = np.where(inside[valid],raw[valid],0.0)
                    summary = summarize_depth(depth,valid,inside,assigned,kinds,ground)
                    for key,value in summary.items():
                        record[(f"depth_{s}_{key}" if key in {"min_m","mean_m","max_m","all_valid_mean_m"} else f"{key}_{s}")] = value
                if not code and np.any(valid & (depth>0)):
                    raise RuntimeError("A non-exposed building was marked wet")
                wet = valid & (depth>0)
                if np.any(wet & ~inside):
                    raise RuntimeError("Wet sample outside official extent")
                sample_totals[s].update(total=len(xy),valid=int(valid.sum()),missing=int((~valid).sum()),
                    wet=int(wet.sum()),dry=int((valid & ~wet).sum()),ground_missing=int((~np.isfinite(ground)).sum()),
                    cell_missing=int((assigned<0).sum()),suppressed_outside_extent=int((valid & ~inside & (raw>0)).sum()))
                sample_totals[s].update({f"assignment_{k}":int(v) for k,v in Counter(kinds).items()})
                sample_positive[s].extend(depth[wet].tolist())
                per_scenario[s] = (depth, valid, wse, assigned)
                if preserved is not None:
                    writer.writerows(preserved)
                    continue
                for j in range(len(xy)):
                    writer.writerow(clean({"buildingid":record["buildingid"],"part_index":int(part_ids[j]),
                        "sample_index":j,"x_epsg2169":float(xy[j,0]),"y_epsg2169":float(xy[j,1]),
                        "ground_elevation_m":ground[j],"scenario":s,"official_extent_contains_sample":bool(inside[j]),
                        "chainage_m":int(assigned[j]) if assigned[j]>=0 else None,"water_cell_assignment":str(kinds[j]),
                        "scenario_wse_m":wse[j],"reconstruction_valid":bool(valid[j]),
                        "raw_terrain_depth_m":raw[j],"official_masked_depth_m":depth[j],
                        "wet":bool(wet[j]) if valid[j] else None}))
            for low, high in SCENARIO_PAIRS:
                dlo, vlo, wlo, clo = per_scenario[low]
                dhi, vhi, whi, chi = per_scenario[high]
                common = vlo & vhi
                bad_depth = common & (dhi+TOLERANCE<dlo)
                bad_wse = common & (whi+TOLERANCE<wlo)
                prefix = f'{high}_vs_{low}'
                record[f'{high}_depth_below_{low}_sample_count'] = int(bad_depth.sum())
                record[f'{high}_wse_below_{low}_sample_count'] = int(bad_wse.sum())
                record[f'{prefix}_changed_chainage_sample_count'] = int((common & (chi!=clo)).sum())
                for statistic in ('min_m','mean_m','max_m'):
                    a,b = record[f'depth_{low}_{statistic}'],record[f'depth_{high}_{statistic}']
                    record[f'{high}_positive_{statistic}_below_{low}'] = bool(b+TOLERANCE<a) if a is not None and b is not None else None
                pair_comparisons[prefix].update(sample_depth_decreases=int(bad_depth.sum()), sample_wse_decreases=int(bad_wse.sum()),
                    buildings_with_sample_depth_decrease=int(bad_depth.any()), buildings_with_sample_wse_decrease=int(bad_wse.any()))
            records.append(clean(record))
            if (index+1)%50==0:
                emit("buildings_sampled",completed=index+1,total=len(selected))
    dem.close()
    frame = pd.DataFrame(records)
    frame.to_csv(PRODUCTS[1],index=False,na_rep="")
    wgs = selected.to_crs(4326)
    features = [{"type":"Feature","id":r["buildingid"],"geometry":baseline_features[r["buildingid"]]["geometry"] if r["buildingid"] in baseline_features else mapping(g),"properties":r}
                for r,g in zip(records,wgs.geometry)]
    document = {"type":"FeatureCollection","name":"provisional_building_reconstructed_depths",
                "features":features}
    with PRODUCTS[0].open("w",encoding="utf-8") as f:
        json.dump(document,f,ensure_ascii=False,allow_nan=False,separators=(",",":"))
        f.write("\n")
    # Read back both summaries; verify identity, geometry preservation and numeric values.
    check_geo = json.loads(PRODUCTS[0].read_text())
    check_csv = pd.read_csv(PRODUCTS[1],dtype={"buildingid":str})
    if len(check_geo["features"])!=len(selected) or len(check_csv)!=len(selected):
        raise RuntimeError("Output record-count mismatch")
    ids = [f["properties"]["buildingid"] for f in check_geo["features"]]
    assert len(ids)==len(set(ids)) and ids==check_csv.buildingid.tolist()
    restored = gpd.GeoSeries([shape(f["geometry"]) for f in check_geo["features"]],crs=4326).to_crs(2169)
    geometry_error = shapely.hausdorff_distance(selected.geometry.to_numpy(),restored.to_numpy())
    if np.max(geometry_error)>0.02:
        raise RuntimeError("Output footprint round-trip error exceeds 2 cm")
    for name in [f"depth_{s}_{stat}_m" for s in SCENARIOS for stat in ("min","mean","max")]:
        a=np.array([np.nan if r[name] is None else r[name] for r in records])
        b=check_csv[name].to_numpy(float)
        assert np.array_equal(np.isnan(a),np.isnan(b)) and np.allclose(a,b,equal_nan=True)
    stats = {}
    for s in SCENARIOS:
        exposed=[r for r in records if r[f"exposure_{s}_code"]>0]
        stats[s]={
            "official_exposure_counts":{str(code):int((buildings[f"exposure_{s}_code"]==code).sum()) for code in (0,1,2)},
            "officially_exposed_buildings":len(exposed),
            "exposed_buildings_with_positive_depth":sum(r[f"samples_wet_{s}"]>0 for r in exposed),
            "exposed_buildings_without_positive_depth":sum(r[f"samples_wet_{s}"]==0 for r in exposed),
            "exposed_buildings_with_missing_samples":sum(r[f"samples_missing_{s}"]>0 for r in exposed),
            "samples_all_output_buildings":dict(sample_totals[s]),
            "positive_sample_depth_m":distribution(sample_positive[s]),
            "exposed_building_positive_mean_depth_m":distribution([r[f"depth_{s}_mean_m"] for r in exposed]),
            "exposed_building_positive_max_depth_m":distribution([r[f"depth_{s}_max_m"] for r in exposed]),
            "exposed_building_wet_fraction":distribution([r[f"wet_sample_fraction_{s}"] for r in exposed]),
            "null_summary_fields":{k:sum(r[k] is None for r in records) for k in (f"depth_{s}_min_m",f"depth_{s}_mean_m",f"depth_{s}_max_m")},
            "water_cells":len(cells[s]),"chainages":len(profiles[s]),
            "profile":[{"chainage_m":int(c),"scenario_wse_m":float(v)} for c,v in sorted(profiles[s].items())],
        }
    for record in records:
        if record['buildingid'] in baseline_features:
            for key, value in baseline_features[record['buildingid']]['properties'].items():
                assert record[key] == value, (record['buildingid'], key)
    outside100 = extents["hq10"].intersection(study).difference(extents["hq100"]).area
    comparison = pair_comparisons['hq100_vs_hq10']
    report = {
        "status":"completed_provisional_no_monetary_damage","created_utc":datetime.now(timezone.utc).isoformat(),
        "baseline_provenance":baseline_provenance(),
        "reference_commit":COMMIT,"reference_repository":"robin-szy/urban-digital-twin-alzette-luxembourg",
        "reference_source_sha256":reference_hashes,
        "unchanged_reference_functions":{
            "water_surface.py":["water_cells","_tributary_cells","scenario_wse","_scenario_profile","wse_from_gauges","_wse_profile","_is_stage"],
            "buildings_3d.py":["study_area","_footprint_points","_extent_status"]},
        "local_adaptations":["Read existing local GeoJSON instead of hardcoded scenario shapefiles; same 3000 m crop",
            "Use GDAL-backed scoped read-only raster sampler instead of unavailable rasterio",
            "Retain one complete 2D footprint per buildingid; aggregate duplicate IDs only if present",
            "Scenario-specific sample-cell joins; strict within, unique-chainage boundary fallback; ambiguities null",
            "Mask reconstructed depths using strict interior of official scenario extent"],
        "inputs":{**inputs.provenance(), str(DEM_PATH):{"bytes":DEM_PATH.stat().st_size,"mtime_ns":DEM_PATH.stat().st_mtime_ns}},
        "software":{"python":os.sys.version,"geopandas":gpd.__version__,
            "shapely":shapely.__version__,"pyproj":pyproj.__version__,"numpy":np.__version__},
        "dem":dem.metadata,"study_area":{"definition":"Union of original HQ100 and HQextreme water_cells(500, scenario), same km35-50 markers and tributary rules",
            "crs":"EPSG:2169","bounds":study.bounds,"area_m2":study.area,
            "building_bbox_rows_read":bbox_rows,"screened_unique_buildings":len(buildings),
            "duplicate_source_id_rows_dissolved":duplicate_rows,"output_affected_union_buildings":len(selected),
            "full_footprints_preserved_not_clipped":True},
        "sampling":{"spacing_m":STEP,"location_crs":"EPSG:2169","method":"Luiz _footprint_points: rings sampled along edges plus interior grid",
            "wet_rule":"valid DEM and uniquely assigned scenario WSE; strictly inside official extent; WSE > DEM",
            "depth_formula":"max(0, WSE - DEM); outside official extent zero only when both numeric inputs valid; unknown remains null",
            "summary_definition":"depth_{scenario}_{min,mean,max}_m use strictly positive official-masked valid depths only; no positive samples => null",
            "all_valid_mean_definition":"Includes valid dry zeros; null if no valid samples",
            "wet_fraction_denominator":"All valid reconstruction samples across full footprint, including known dry samples outside official extent",
            "unknown_csv_encoding":"Empty field; GeoJSON/report use JSON null",
            "weighting_limitation":"Edge and interior samples follow Luiz; wet-sample fraction is not exact flooded-area fraction"},
        "scenarios":stats,
        "comparison_cohort":{"baseline_buildings":len(baseline_features), "buildingids":sorted(baseline_features),
            "additional_buildings":len(selected)-len(baseline_features), "legacy_depth_records_and_samples_reused":bool(baseline_features),
            "baseline_depth_report":str(baseline_path('depth')/'validation_report.json') if baseline_features else None},
        "scenario_pair_comparisons":dict(pair_comparisons),
        "profile_order_checks":{f'{high}_vs_{low}': {
            'decrease_chainages':[c for c in sorted(set(profiles[low]) & set(profiles[high])) if profiles[high][c]+TOLERANCE<profiles[low][c]]}
            for low,high in SCENARIO_PAIRS},
        "consistency":{"tolerance_m":TOLERANCE,"common_profile_chainages":len(chainages),
            "hq100_wse_below_hq10_chainages":profile_bad,
            "minimum_profile_hq100_minus_hq10_m":min(profiles["hq100"][c]-profiles["hq10"][c] for c in chainages),
            "hq10_extent_outside_hq100_within_study_m2":outside100,**dict(comparison),
            "positive_mean_min_note":"Positive-only mean/min need not increase when HQ100 adds shallow wet samples; all decreases are reported, never adjusted"},
        "validation":{"unique_output_ids":True,"csv_geojson_counts_match":True,"csv_geojson_depth_values_match":True,
            "footprint_roundtrip_max_error_m":float(np.max(geometry_error)),
            "no_wet_samples_outside_official_extent":True,"no_nonexposed_buildings_marked_wet":True,
            "nonfinite_values_serialized_as_null":True,"national_dem_not_loaded":True,
            "outputs":[{"path":portable_path(p),"bytes":p.stat().st_size} for p in PRODUCTS[:3]]},
        "limitations":["PROVISIONAL: DEM vertical datum and elevation units are not confirmed by metadata; metre elevations assumed",
            "No DEM provenance sidecar supplied; filling history and agreement with official-map model terrain unknown",
            "No CZML display offset used; no building roof Z or zmin used as ground",
            "DEM is 5 m; 1 m sample spacing does not add terrain detail",
            "Scenario WSE reconstructed from extent-edge terrain, smoothing and monotonicity; not calibrated official depth grids",
            "No finished-floor levels, hydraulic connectivity, defence behaviour or damage calculation",
            "Missing/unassigned samples excluded from depth statistics and explicitly counted; incomplete means may be biased",
            "Buildings exposed to any supported scenario plus the original comparison cohort are exported; all expanded study-area buildings are screened",
            "Retained full building footprints can extend slightly beyond the selecting study-area boundary",
            "Building positive-depth mean/min and wet fraction depend on sampling and official masks"],
    }
    PRODUCTS[3].write_text(json.dumps(clean(report),ensure_ascii=False,allow_nan=False,indent=2)+"\n")
    emit("completed",output_buildings=len(selected),exposed={s:stats[s]["officially_exposed_buildings"] for s in SCENARIOS},
         consistency=report["consistency"],outputs=[str(p) for p in PRODUCTS])


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
