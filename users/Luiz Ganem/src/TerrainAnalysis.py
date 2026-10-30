import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from rasterio import features
from rasterio.transform import Affine
from rasterio.windows import Window
from scipy.spatial import cKDTree
from shapely import line_merge, segmentize
from shapely.geometry import box
from whitebox import WhiteboxTools

import PipelineConfig


# ============================================================
# SETTINGS (from config/config.toml)
# ============================================================
#
# Every create_* function takes dem_path, output_dir, water_path
# and km_gpkg; the defaults below come from the config.
# Output names inside output_dir follow the *_PATH constants.

CFG = PipelineConfig.load()

OUTPUT_DIR = Path(CFG["paths"]["output_dir"])

# All GeoTIFFs in TIF_DIR, all vector files in VECTOR_DIR; watershed
# outputs in a watersheds subfolder of each
TIF_DIR = OUTPUT_DIR / CFG["outputs"]["tif_subdir"]
VECTOR_DIR = OUTPUT_DIR / CFG["outputs"]["vector_subdir"]
WATERSHEDS_SUBDIR = CFG["outputs"]["watersheds_subdir"]

DEM_PATH = Path(CFG["paths"]["dem"])
WATER_PATH = Path(CFG["paths"]["water"])


SLOPE_PATH = TIF_DIR / "slope.tif"
ASPECT_PATH = TIF_DIR / "aspect.tif"
FILLED_DEM_PATH = TIF_DIR / "filled_dem.tif"
FLOW_DIRECTION_PATH = TIF_DIR / "flow_direction.tif"
FLOW_ACCUMULATION_PATH = TIF_DIR / "flow_accumulation.tif"
WATERSHEDS_PATH = TIF_DIR / WATERSHEDS_SUBDIR / "watersheds.tif"
DRAINAGE_WATER_PATH = TIF_DIR / "drainage_water.tif"


# ============================================================
# RIVER ANALYSIS SETTINGS (from config/config.toml)
# ============================================================
#
# All thresholds are in real units (ha / m) and are converted
# to cells at runtime from the DEM pixel size.

# Official watercourse kilometre markers (points, any number of layers)
RIVER_KM_GPKG = Path(CFG["paths"]["river_km_gpkg"])
RIVER_NAME_FIELD = CFG["rivers"]["name_field"]
RIVER_KM_FIELD = CFG["rivers"]["km_field"]

# Multi-scale watersheds: minimum upstream area of a stream link
WATERSHED_THRESHOLDS_HA = dict(CFG["watersheds"]["thresholds_ha"])

# Edge outlets draining less than this are merged into one
# "minor_edge" unit (ws_id = 0)
EDGE_BASIN_MIN_HA = CFG["watersheds"]["edge_basin_min_ha"]

# Km markers are snapped to the highest flow accumulation
# cell within this distance
MARKER_SNAP_DISTANCE_M = CFG["watersheds"]["marker_snap_distance_m"]

# River centerlines
CENTERLINE_THRESHOLD_HA = CFG["rivers"]["centerline_threshold_ha"]
NAMING_RADIUS_M = CFG["rivers"]["naming_radius_m"]
NAMING_MIN_MARKERS = CFG["rivers"]["naming_min_markers"]
VALIDATION_BUFFERS_M = tuple(CFG["rivers"]["validation_buffers_m"])

# River bed (height above nearest drainage)
HAND_PERCENTILE = CFG["river_bed"]["hand_percentile"]
HAND_CLAMP_M = tuple(CFG["river_bed"]["hand_clamp_m"])
HAND_FALLBACK_M = CFG["river_bed"]["hand_fallback_m"]  # used if no water cells are near the rivers
BED_MAX_DISTANCE_M = CFG["river_bed"]["bed_max_distance_m"]
WATER_CHECK_BUFFER_M = CFG["river_bed"]["water_check_buffer_m"]

# Centerline vs km marker check
MARKER_CHECK_DISTANCE_M = CFG["marker_check"]["search_distance_m"]
MARKER_ON_TOLERANCE_CELLS = CFG["marker_check"]["on_centerline_tolerance_cells"]

# Processing tile size (cells per side) for windowed reads/writes
BLOCK_SIZE = CFG["terrain"]["block_size"]

# Stream links whose densified samples are processed at once in
# create_river_centerlines (caps memory on large DEMs)
CENTERLINE_CHUNK_LINES = CFG["terrain"]["centerline_chunk_lines"]


# ============================================================
# RIVER ANALYSIS OUTPUTS
# ============================================================

WATERSHEDS_DIR = TIF_DIR / WATERSHEDS_SUBDIR
WATERSHEDS_VECTOR_DIR = VECTOR_DIR / WATERSHEDS_SUBDIR

RIVER_STREAMS_PATH = TIF_DIR / "river_streams.tif"
HAND_PATH = TIF_DIR / "hand.tif"
RIVER_BED_PATH = TIF_DIR / "river_bed.tif"

CENTERLINES_GPKG = VECTOR_DIR / "river_centerlines.gpkg"
RIVER_BED_GPKG = VECTOR_DIR / "river_bed.gpkg"
DRAINAGE_POINTS_GPKG = VECTOR_DIR / "drainage_points.gpkg"
MARKER_CHECK_GPKG = VECTOR_DIR / "river_marker_check.gpkg"


# ============================================================
# PATHS
# ============================================================

def _paths(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # All input/output paths of one run. Outputs keep the layout of
    # the *_PATH constants, relocated from OUTPUT_DIR to output_dir.
    out = Path(output_dir)

    def rel(path):
        return out / Path(path).relative_to(OUTPUT_DIR)

    return SimpleNamespace(
        dem=Path(dem_path),
        water=Path(water_path),
        km_gpkg=Path(km_gpkg),
        out=out,
        tif_dir=rel(TIF_DIR),
        vector_dir=rel(VECTOR_DIR),
        slope=rel(SLOPE_PATH),
        aspect=rel(ASPECT_PATH),
        filled=rel(FILLED_DEM_PATH),
        ptr=rel(FLOW_DIRECTION_PATH),
        acc=rel(FLOW_ACCUMULATION_PATH),
        basins=rel(WATERSHEDS_PATH),
        drainage=rel(DRAINAGE_WATER_PATH),
        watersheds_dir=rel(WATERSHEDS_DIR),
        watersheds_vector_dir=rel(WATERSHEDS_VECTOR_DIR),
        streams=rel(RIVER_STREAMS_PATH),
        hand=rel(HAND_PATH),
        bed=rel(RIVER_BED_PATH),
        centerlines=rel(CENTERLINES_GPKG),
        bed_gpkg=rel(RIVER_BED_GPKG),
        drainage_points=rel(DRAINAGE_POINTS_GPKG),
        marker_check=rel(MARKER_CHECK_GPKG)
    )


def _make_dirs(p):

    for folder in (p.tif_dir, p.vector_dir, p.watersheds_dir, p.watersheds_vector_dir):
        folder.mkdir(parents=True, exist_ok=True)


# ============================================================
# VALIDATION OF INPUT RASTERS
# ============================================================

def validate_rasters(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    if not p.dem.exists():
        raise FileNotFoundError(
            f"DEM not found: {p.dem}"
        )

    if not p.water.exists():
        raise FileNotFoundError(
            f"Water mask not found: {p.water}"
        )

    with rasterio.open(p.dem) as dem:
        with rasterio.open(p.water) as water:

            if dem.shape != water.shape:
                raise ValueError(
                    "DEM and water mask have different dimensions."
                )

            if dem.transform != water.transform:
                raise ValueError(
                    "DEM and water mask are not aligned."
                )

            if dem.crs != water.crs:
                raise ValueError(
                    "DEM and water mask have different CRS."
                )

    print("DEM and water mask are aligned.")


# ============================================================
# SLOPE + ASPECT
# ============================================================

def create_slope_aspect(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    print("Creating slope and aspect maps...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _make_dirs(p)

    with rasterio.open(p.dem) as src:

        profile = src.profile.copy()

        # Pixel dimensions
        dx = src.res[0]
        dy = src.res[1]

        profile.update(
            dtype="float32",
            count=1,
            compress="deflate",
            nodata=np.nan,
            BIGTIFF="IF_SAFER"
        )

        with rasterio.open(p.slope, "w", **profile) as slope_dst, \
             rasterio.open(p.aspect, "w", **profile) as aspect_dst:

            for window in _iter_windows(src):

                # Block + 1-cell halo, cut at the raster border so
                # np.gradient still takes one-sided differences on the
                # true raster edges: identical to the full-raster result
                r0 = max(window.row_off - 1, 0)
                c0 = max(window.col_off - 1, 0)
                r1 = min(window.row_off + window.height + 1, src.height)
                c1 = min(window.col_off + window.width + 1, src.width)

                dem = src.read(
                    1,
                    window=Window(c0, r0, c1 - c0, r1 - r0)
                ).astype(np.float32)

                inner = (
                    slice(window.row_off - r0, window.row_off - r0 + window.height),
                    slice(window.col_off - c0, window.col_off - c0 + window.width)
                )

                # Elevation gradients
                dz_dy, dz_dx = np.gradient(dem, dy, dx)

                dz_dy = dz_dy[inner]
                dz_dx = dz_dx[inner]

                # ------------------------------------------------
                # Slope
                # ------------------------------------------------

                slope = np.degrees(
                    np.arctan(
                        np.sqrt(dz_dx**2 + dz_dy**2)
                    )
                )

                # ------------------------------------------------
                # Aspect
                # ------------------------------------------------

                aspect = np.degrees(
                    np.arctan2(-dz_dx, dz_dy)
                )

                # Convert to compass direction:
                # 0 = North
                # 90 = East
                # 180 = South
                # 270 = West

                aspect = (aspect + 360) % 360

                slope_dst.write(slope.astype(np.float32), 1, window=window)
                aspect_dst.write(aspect.astype(np.float32), 1, window=window)

    print(f"  Created: {p.slope}")
    print(f"  Created: {p.aspect}")


# ============================================================
# HYDROLOGY
# ============================================================

def create_hydrology(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    print("\nCreating hydrological maps...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _make_dirs(p)

    wbt = _CheckedWhitebox()

    # Don't print every internal Whitebox message
    wbt.set_verbose_mode(False)

    dem = str(p.dem.resolve())
    filled = str(p.filled.resolve())
    pointer = str(p.ptr.resolve())
    accumulation = str(p.acc.resolve())
    watersheds = str(p.basins.resolve())

    # --------------------------------------------------------
    # 1. Fill depressions
    # --------------------------------------------------------

    print("Filling depressions...")

    wbt.fill_depressions(
        dem,
        filled
    )

    # --------------------------------------------------------
    # 2. D8 flow direction (NO SPLITTING)
    # --------------------------------------------------------

    print("Calculating flow direction...")

    wbt.d8_pointer(
        filled,
        pointer
    )

    # --------------------------------------------------------
    # 3. Flow accumulation
    # --------------------------------------------------------
    #
    # Computed from the D8 pointer above instead of the filled
    # DEM: same result, without deriving the D8 directions again.

    print("Calculating flow accumulation...")

    wbt.d8_flow_accumulation(
        pointer,
        accumulation,
        out_type="cells",
        pntr=True
    )

    # --------------------------------------------------------
    # 4. Watersheds
    # --------------------------------------------------------
    #
    # A true watershed needs outlet/pour points.
    #
    # Since we don't have outlet points yet, we first generate
    # watershed basins automatically from the DEM.
    # --------------------------------------------------------

    print("Calculating drainage basins...")

    wbt.basins(
        pointer,
        watersheds
    )

    print(f"  Created: {p.filled}")
    print(f"  Created: {p.ptr}")
    print(f"  Created: {p.acc}")
    print(f"  Created: {p.basins}")


# ============================================================
# DRAINAGE WATER
# ============================================================

def create_drainage_water(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    print("\nCombining flow accumulation with water mask...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _make_dirs(p)

    with rasterio.open(p.acc) as acc_src, \
         rasterio.open(p.water) as water_src:

        profile = acc_src.profile.copy()

        profile.update(
            dtype="float32",
            nodata=0,
            compress="deflate",
            BIGTIFF="IF_SAFER"
        )

        with rasterio.open(
            p.drainage,
            "w",
            **profile
        ) as dst:

            for window in _iter_windows(acc_src):

                accumulation = acc_src.read(1, window=window)
                water = water_src.read(1, window=window)

                drainage_water = np.where(
                    water == 1,
                    accumulation,
                    0
                ).astype(np.float32)

                dst.write(drainage_water, 1, window=window)

    print(f"  Created: {p.drainage}")


# ============================================================
# RIVER ANALYSIS HELPERS
# ============================================================

HA = 10_000.0

# Whitebox D8 pointer encoding (d8_pointer, esri_pntr=False)
#   64 128   1
#   32   x   2
#   16   8   4
D8_OFFSETS = {
    1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0),
    16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)
}

D8_DR = np.zeros(256, dtype=np.int64)
D8_DC = np.zeros(256, dtype=np.int64)
D8_OK = np.zeros(256, dtype=bool)

for _code, (_dr, _dc) in D8_OFFSETS.items():
    D8_DR[_code] = _dr
    D8_DC[_code] = _dc
    D8_OK[_code] = True

# When two pour point types fall on the same cell, keep the
# most specific one
POUR_POINT_PRIORITY = {
    "edge_outlet": 0,
    "link_outlet": 1,
    "marker_outlet": 2
}


class _CheckedWhitebox(WhiteboxTools):

    # WhiteboxTools.run_tool returns 0 even when the tool fails, so a
    # failure would only show up later as a missing file. The tool's
    # messages are collected and an error is raised if its output
    # file was not (re)written.
    def run_tool(self, tool_name, args, callback=None):

        messages = []
        verbose = self.verbose
        start = time.time()

        # Messages only reach the callback in verbose mode; they are
        # collected here, not printed
        self.verbose = True
        try:
            code = super().run_tool(tool_name, args, messages.append)
        finally:
            self.verbose = verbose

        outputs = [
            Path(arg.split("=", 1)[1].strip("'\""))
            for arg in args if arg.startswith("--output=")
        ]
        missing = [
            path for path in outputs
            if not path.exists() or path.stat().st_mtime < start - 1
        ]

        if code != 0 or missing:
            details = "\n  ".join(m.strip() for m in messages[-15:] if m.strip())
            raise RuntimeError(
                f"Whitebox {tool_name} failed"
                + (f" (no output: {', '.join(map(str, missing))})" if missing else "")
                + f":\n  {details}"
            )

        return code


def _new_wbt():

    wbt = _CheckedWhitebox()
    wbt.set_verbose_mode(False)

    return wbt


def _area_to_cells(area_m2, src):

    cell_area = abs(src.res[0] * src.res[1])

    return max(1, int(round(area_m2 / cell_area)))


def _check_alignment(p, *paths):

    with rasterio.open(p.dem) as dem:
        for path in paths:

            if not Path(path).exists():
                raise FileNotFoundError(
                    f"Missing input: {path}. Run create_hydrology() first."
                )

            with rasterio.open(path) as src:
                if (
                    src.shape != dem.shape
                    or src.transform != dem.transform
                    or src.crs != dem.crs
                ):
                    raise ValueError(
                        f"{path} is not aligned with {p.dem}. "
                        "Re-run create_hydrology() for the current DEM."
                    )


def _iter_windows(src, size=BLOCK_SIZE):

    for row in range(0, src.height, size):
        for col in range(0, src.width, size):
            yield Window(
                col,
                row,
                min(size, src.width - col),
                min(size, src.height - row)
            )


def _group_by_tile(rows, cols, width, size=BLOCK_SIZE):

    # {tile id: indices of the cells in that tile}, tile ids in
    # _iter_windows order. One sort instead of one mask per tile,
    # which matters with millions of cells and thousands of tiles.
    tiles_per_row = (width + size - 1) // size
    tile = (rows // size) * tiles_per_row + cols // size

    order = np.argsort(tile, kind="stable")
    tile = tile[order]
    starts = np.flatnonzero(np.diff(tile)) + 1

    return {
        int(tile[group[0]]): order[group]
        for group in np.split(np.arange(tile.size), starts)
        if group.size
    }


def _read_halo(src, window, halo, fill):

    # Window grown by `halo` cells on every side; cells outside
    # the raster are filled with `fill`
    halo_window = Window(
        window.col_off - halo,
        window.row_off - halo,
        window.width + 2 * halo,
        window.height + 2 * halo
    )

    return src.read(
        1,
        window=halo_window,
        boundless=True,
        fill_value=fill
    )


def _valid(arr, nodata):

    valid = np.ones(arr.shape, dtype=bool)

    if nodata is not None and not np.isnan(nodata):
        valid &= arr != nodata

    if np.issubdtype(arr.dtype, np.floating):
        valid &= ~np.isnan(arr)

    return valid


def _fill_value(src):

    if src.nodata is not None:
        return src.nodata

    return np.nan if np.issubdtype(np.dtype(src.dtypes[0]), np.floating) else 0


def _d8_targets(ptr):

    # Row/col of the downstream cell of every cell, inside `ptr`.
    # `ok` is False for pits, nodata and flow leaving the array.
    rows, cols = np.indices(ptr.shape)

    code = np.where((ptr > 0) & (ptr < 256), ptr, 0).astype(np.int64)

    target_rows = rows + D8_DR[code]
    target_cols = cols + D8_DC[code]

    ok = (
        D8_OK[code]
        & (target_rows >= 0) & (target_rows < ptr.shape[0])
        & (target_cols >= 0) & (target_cols < ptr.shape[1])
    )

    return (
        np.where(ok, target_rows, 0),
        np.where(ok, target_cols, 0),
        ok
    )


def _output_profile(src, dtype, nodata):

    profile = src.profile.copy()

    profile.update(
        driver="GTiff",
        dtype=dtype,
        nodata=nodata,
        count=1,
        compress="deflate",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        BIGTIFF="IF_SAFER"
    )

    return profile


def _cell_centers(transform, rows, cols):

    xs, ys = rasterio.transform.xy(
        transform,
        np.asarray(rows),
        np.asarray(cols),
        offset="center"
    )

    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def _sample_cells(path, rows, cols, size=BLOCK_SIZE):

    # Values of a raster at (row, col) cells, reading only the
    # tiles that contain points
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)

    with rasterio.open(path) as src:

        values = np.full(rows.shape, _fill_value(src), dtype=src.dtypes[0])

        if rows.size == 0:
            return values

        for sel in _group_by_tile(rows, cols, src.width, size).values():

            r0, r1 = rows[sel].min(), rows[sel].max()
            c0, c1 = cols[sel].min(), cols[sel].max()

            arr = src.read(
                1,
                window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
            )

            values[sel] = arr[rows[sel] - r0, cols[sel] - c0]

    return values


def _dissolve_pieces(geoms, values, value_field, crs):

    # Same result as gdf.dissolve(by=value_field, as_index=False).
    # Only ids cut into several pieces by the tiling go through the
    # (slow, per group) dissolve; ids with a single piece get the
    # same GEOS unary union in one vectorised call.
    gdf = gpd.GeoDataFrame(
        {value_field: np.asarray(values, dtype=np.int64)},
        geometry=geoms,
        crs=crs
    )

    if gdf.empty:
        return gdf

    split = gdf[value_field].duplicated(keep=False).to_numpy()

    single = gdf[~split]
    single = gpd.GeoDataFrame(
        {value_field: single[value_field].to_numpy()},
        geometry=shapely.union_all(
            np.asarray(single.geometry.values)[:, None], axis=1
        ),
        crs=crs
    )

    parts = [single]

    if split.any():
        parts.append(gdf[split].dissolve(by=value_field, as_index=False))

    merged = pd.concat(parts, ignore_index=True)
    merged = merged.sort_values(value_field, kind="stable").reset_index(drop=True)

    return gpd.GeoDataFrame(
        merged[[c for c in merged.columns if c != "geometry"] + ["geometry"]],
        geometry="geometry",
        crs=crs
    )


def _polygonize_raster(path, value_field):

    # Polygonize tile by tile; polygons cut at tile edges are
    # merged again by the dissolve.
    geoms = []
    values = []

    with rasterio.open(path) as src:

        crs = src.crs

        for window in _iter_windows(src):

            arr = src.read(1, window=window)
            mask = _valid(arr, src.nodata)

            if not mask.any():
                continue

            for geom, value in features.shapes(
                arr,
                mask=mask,
                transform=src.window_transform(window)
            ):
                geoms.append(shapely.geometry.shape(geom))
                values.append(int(value))

    return _dissolve_pieces(geoms, values, value_field, crs)


def _read_river_markers(p):

    # Km markers of every layer of the km gpkg that fall on valid
    # DEM cells. Layer order is used as river priority (primary
    # watercourses first).
    columns = ["river_name", "river_class", "layer_order", "km", "row", "col"]

    with rasterio.open(p.dem) as dem:
        crs = dem.crs
        transform = dem.transform
        height, width = dem.shape
        nodata = dem.nodata
        aoi = gpd.GeoSeries([box(*dem.bounds)], crs=crs)

    empty = gpd.GeoDataFrame(
        {c: [] for c in columns},
        geometry=[],
        crs=crs
    )

    if not p.km_gpkg.exists():
        print(f"  WARNING: {p.km_gpkg} not found, no river markers used.")
        return empty

    parts = []

    for layer_order, layer in enumerate(gpd.list_layers(p.km_gpkg)["name"]):

        gdf = gpd.read_file(p.km_gpkg, layer=layer, bbox=aoi)

        if gdf.empty:
            continue

        if RIVER_NAME_FIELD not in gdf or RIVER_KM_FIELD not in gdf:
            print(f"  WARNING: layer '{layer}' has no "
                  f"{RIVER_NAME_FIELD}/{RIVER_KM_FIELD} fields, skipped.")
            continue

        gdf = gdf.to_crs(crs).explode(index_parts=False)
        gdf = gdf[gdf.geom_type == "Point"]
        gdf = gdf[gdf[RIVER_NAME_FIELD].notna()]

        if gdf.empty:
            continue

        rows, cols = rasterio.transform.rowcol(
            transform,
            gdf.geometry.x.values,
            gdf.geometry.y.values
        )

        gdf = gpd.GeoDataFrame(
            {
                "river_name": gdf[RIVER_NAME_FIELD].astype(str).values,
                "river_class": layer,
                "layer_order": layer_order,
                "km": gdf[RIVER_KM_FIELD].astype(float).round(3).values,
                "row": np.asarray(rows, dtype=np.int64),
                "col": np.asarray(cols, dtype=np.int64)
            },
            geometry=gdf.geometry.values,
            crs=crs
        )

        inside = (
            (gdf["row"] >= 0) & (gdf["row"] < height)
            & (gdf["col"] >= 0) & (gdf["col"] < width)
        )

        parts.append(gdf[inside])

    if not parts:
        print("  WARNING: no river km markers inside the DEM extent.")
        return empty

    markers = pd.concat(parts, ignore_index=True)

    dem_values = _sample_cells(p.dem, markers["row"], markers["col"])

    markers = markers[_valid(dem_values, nodata)]

    markers = markers.reset_index(drop=True)

    return markers


def _snap_markers(markers, p):

    # Move every marker to the highest flow accumulation cell
    # within MARKER_SNAP_DISTANCE_M. The accumulation is read once
    # per tile of markers; each marker then searches the same
    # clipped window as before, sliced from that block.
    markers = markers.copy()

    for column in ["snap_row", "snap_col"]:
        markers[column] = pd.Series(dtype=np.int64)

    markers["snap_acc"] = pd.Series(dtype=float)
    markers["snap_dist_m"] = pd.Series(dtype=float)

    if markers.empty:
        return markers

    marker_rows = markers["row"].to_numpy(np.int64)
    marker_cols = markers["col"].to_numpy(np.int64)

    snapped = np.zeros((len(markers), 4), dtype=float)

    with rasterio.open(p.acc) as acc:

        res_x = abs(acc.res[0])
        res_y = abs(acc.res[1])

        radius_r = int(np.ceil(MARKER_SNAP_DISTANCE_M / res_y))
        radius_c = int(np.ceil(MARKER_SNAP_DISTANCE_M / res_x))

        for sel in _group_by_tile(marker_rows, marker_cols, acc.width).values():

            br0 = max(marker_rows[sel].min() - radius_r, 0)
            bc0 = max(marker_cols[sel].min() - radius_c, 0)
            br1 = min(marker_rows[sel].max() + radius_r + 1, acc.height)
            bc1 = min(marker_cols[sel].max() + radius_c + 1, acc.width)

            block = acc.read(
                1,
                window=Window(bc0, br0, bc1 - bc0, br1 - br0)
            ).astype(np.float64)

            for i in sel:

                row = marker_rows[i]
                col = marker_cols[i]

                r0 = max(row - radius_r, 0)
                c0 = max(col - radius_c, 0)
                r1 = min(row + radius_r + 1, acc.height)
                c1 = min(col + radius_c + 1, acc.width)

                arr = block[r0 - br0:r1 - br0, c0 - bc0:c1 - bc0].copy()

                rr, cc = np.indices(arr.shape)

                dist = np.hypot(
                    (rr + r0 - row) * res_y,
                    (cc + c0 - col) * res_x
                )

                arr[(dist > MARKER_SNAP_DISTANCE_M) | ~_valid(arr, acc.nodata)] = -np.inf

                k = int(np.argmax(arr))
                kr, kc = np.unravel_index(k, arr.shape)

                snapped[i] = (r0 + kr, c0 + kc, arr[kr, kc], dist[kr, kc])

    markers["snap_row"] = snapped[:, 0].astype(np.int64)
    markers["snap_col"] = snapped[:, 1].astype(np.int64)
    markers["snap_acc"] = snapped[:, 2]
    markers["snap_dist_m"] = snapped[:, 3].round(2)

    return markers


# Snapped markers of the last run, keyed by the input files, so the
# watershed and centerline steps read and snap them only once
_MARKER_CACHE = {}


def _river_markers(p):

    key = tuple(
        (str(path.resolve()), path.stat().st_mtime_ns if path.exists() else None)
        for path in (p.dem, p.acc, p.km_gpkg)
    )

    if key not in _MARKER_CACHE:
        _MARKER_CACHE.clear()
        _MARKER_CACHE[key] = _snap_markers(_read_river_markers(p), p)

    markers = _MARKER_CACHE[key]

    if not markers.empty:
        print(f"  River km markers in AOI: {len(markers)} "
              f"({markers['river_name'].nunique()} rivers)")

    return markers.copy()


def _find_outlets(p, stream_cells, edge_min_cells):

    # One windowed pass over the D8 pointer (2-cell halo) for the
    # edge outlets and the stream outlets of every threshold in
    # `stream_cells` ({scale: cells}).
    #
    # edge:        valid cells whose flow leaves the DEM (pointer 0,
    #              off-grid or into nodata) with acc >= edge_min_cells
    # link:        stream cells flowing into a confluence or out of the DEM
    # confluence:  stream cells receiving >= 2 stream inflows
    #
    # Stream cells are acc > cells, exactly what Whitebox
    # extract_streams marks, so no stream raster is written and
    # read back per scale.
    halo = 2

    found = {"edge": []}

    for scale in stream_cells:
        found[scale] = {"link": [], "confluence": []}

    with rasterio.open(p.ptr) as ptr_src, \
         rasterio.open(p.acc) as acc_src, \
         rasterio.open(p.dem) as dem_src:

        for window in _iter_windows(ptr_src):

            h, w = window.height, window.width
            inner = (slice(halo, halo + h), slice(halo, halo + w))

            ptr = _read_halo(ptr_src, window, halo, 0)
            dem = _read_halo(dem_src, window, halo, _fill_value(dem_src))
            acc = _read_halo(acc_src, window, halo, _fill_value(acc_src))

            valid = _valid(dem, dem_src.nodata)

            target_rows, target_cols, ok = _d8_targets(ptr)
            ok &= valid & valid[target_rows, target_cols]

            terminal = valid & ~ok

            edge = terminal[inner] & (acc[inner] >= edge_min_cells)

            rows, cols = np.nonzero(edge)
            found["edge"].append((rows + window.row_off, cols + window.col_off))

            acc_valid = _valid(acc, acc_src.nodata) & valid

            t_rows = target_rows[inner]
            t_cols = target_cols[inner]

            for scale, cells in stream_cells.items():

                stream = acc_valid & (acc > cells)

                # Stream inflow count of every cell (exact for all cells
                # except the outermost halo ring)
                flows = stream & ok

                inflow = np.bincount(
                    (target_rows[flows] * ptr.shape[1] + target_cols[flows]),
                    minlength=ptr.size
                ).reshape(ptr.shape)

                into_confluence = (
                    ok[inner]
                    & stream[t_rows, t_cols]
                    & (inflow[t_rows, t_cols] >= 2)
                )

                link = stream[inner] & (terminal[inner] | into_confluence)
                confluence = stream[inner] & (inflow[inner] >= 2)

                rows, cols = np.nonzero(link)
                found[scale]["link"].append(
                    (rows + window.row_off, cols + window.col_off)
                )

                rows, cols = np.nonzero(confluence)
                found[scale]["confluence"].append(
                    (rows + window.row_off, cols + window.col_off)
                )

    def join(parts):
        if parts:
            return (
                np.concatenate([q[0] for q in parts]).astype(np.int64),
                np.concatenate([q[1] for q in parts]).astype(np.int64)
            )
        return np.zeros(0, np.int64), np.zeros(0, np.int64)

    result = {"edge": join(found["edge"])}

    for scale in stream_cells:
        result[scale] = {k: join(v) for k, v in found[scale].items()}

    return result


def _delineate_watersheds(p, pour_points, out_path, tmp_dir, wbt):

    # pour_points: DataFrame with row, col, ws_id (>= 1).
    # Writes an int32 label raster: ws_id, 0 = minor edge drainage,
    # -1 = outside the DEM. Returns the cell count per ws_id and the
    # dissolved polygons (polygonized while each tile is in memory).
    pour_path = Path(tmp_dir) / f"pour_points_{out_path.stem}.tif"
    raw_path = Path(tmp_dir) / f"raw_{out_path.stem}.tif"

    rows = pour_points["row"].to_numpy()
    cols = pour_points["col"].to_numpy()
    ids = pour_points["ws_id"].to_numpy()

    with rasterio.open(p.dem) as dem:

        tiles = _group_by_tile(rows, cols, dem.width)
        tiles_per_row = (dem.width + BLOCK_SIZE - 1) // BLOCK_SIZE

        with rasterio.open(pour_path, "w", **_output_profile(dem, "int32", 0)) as dst:

            for window in _iter_windows(dem):

                arr = np.zeros((window.height, window.width), dtype=np.int32)

                tile_id = (
                    (window.row_off // BLOCK_SIZE) * tiles_per_row
                    + window.col_off // BLOCK_SIZE
                )
                sel = tiles.get(tile_id, np.zeros(0, np.int64))

                arr[rows[sel] - window.row_off, cols[sel] - window.col_off] = ids[sel]

                dst.write(arr, 1, window=window)

    wbt.watershed(
        str(p.ptr.resolve()),
        str(pour_path.resolve()),
        str(raw_path.resolve())
    )

    counts = np.zeros(int(ids.max(initial=0)) + 1, dtype=np.int64)

    geoms = []
    values = []

    with rasterio.open(p.dem) as dem, rasterio.open(raw_path) as raw:

        crs = dem.crs

        with rasterio.open(out_path, "w", **_output_profile(dem, "int32", -1)) as dst:

            for window in _iter_windows(dem):

                labels = raw.read(1, window=window)
                dem_valid = _valid(dem.read(1, window=window), dem.nodata)

                out = np.where(
                    _valid(labels, raw.nodata) & (labels > 0),
                    labels,
                    0
                ).astype(np.int32)

                out[~dem_valid] = -1

                dst.write(out, 1, window=window)

                counts += np.bincount(out[out >= 0], minlength=counts.size)[:counts.size]

                mask = out != -1

                if not mask.any():
                    continue

                for geom, value in features.shapes(
                    out,
                    mask=mask,
                    transform=dem.window_transform(window)
                ):
                    geoms.append(shapely.geometry.shape(geom))
                    values.append(int(value))

    return counts, _dissolve_pieces(geoms, values, "ws_id", crs)


def _trace_downstream(ptr_src, row, col, stop_at=None, tile_size=512):

    # Follow the D8 pointer from (row, col) until the flow leaves
    # the DEM or reaches a cell in `stop_at` (not included).
    # Pointer tiles are read on demand.
    tiles = {}
    path_rows = []
    path_cols = []

    max_steps = ptr_src.width * ptr_src.height

    while (
        0 <= row < ptr_src.height
        and 0 <= col < ptr_src.width
        and len(path_rows) < max_steps
    ):

        key = (row // tile_size, col // tile_size)

        if key not in tiles:
            r0 = key[0] * tile_size
            c0 = key[1] * tile_size
            tiles[key] = ptr_src.read(
                1,
                window=Window(
                    c0, r0,
                    min(tile_size, ptr_src.width - c0),
                    min(tile_size, ptr_src.height - r0)
                )
            )

        code = int(tiles[key][row - key[0] * tile_size, col - key[1] * tile_size])

        if ptr_src.nodata is not None and code == ptr_src.nodata:
            break

        if stop_at is not None and (row, col) in stop_at:
            break

        path_rows.append(row)
        path_cols.append(col)

        if not (0 < code < 256) or not D8_OK[code]:
            break

        row += int(D8_DR[code])
        col += int(D8_DC[code])

    return np.asarray(path_rows, np.int64), np.asarray(path_cols, np.int64)


def _trace_main_rivers(p, markers, min_cells, transform):

    # Main river paths from the DEM, named with the km markers.
    #
    # For every river, the D8 flow is traced downstream from each of
    # its markers that snaps onto the stream network (upstream first;
    # a trace stops where it joins an earlier one). A traced cell is
    # kept ("core") if a cell within NAMING_RADIUS_M of one of the
    # river's markers lies at or below it on the traced flow, so
    # paths that leave the river (DEM deviations) are cut where they
    # leave. From the core cell with the largest flow accumulation the
    # path is then extended downstream until it meets another river
    # or leaves the DEM. Overlaps go to the river with the higher
    # priority (gpkg layer order, then number of markers).
    #
    # Returns {river_name: (rows, cols, river_class, n_matched)}.
    order = (
        markers.groupby("river_name")
        .agg(layer_order=("layer_order", "min"),
             n=("km", "size"),
             river_class=("river_class", "first"))
        .sort_values(["layer_order", "n"], ascending=[True, False])
    )

    cores = []

    with rasterio.open(p.ptr) as ptr_src:

        for name, info in order.iterrows():

            group = markers[markers["river_name"] == name]
            on_network = group[group["snap_acc"] >= min_cells].sort_values("snap_acc")

            if on_network.empty:
                print(f"  WARNING: no '{name}' marker snaps onto the stream "
                      "network, river not traced.")
                continue

            marker_tree = cKDTree(np.column_stack([group.geometry.x, group.geometry.y]))

            # cell -> True if a near-marker cell lies at or below it
            near_below = {}
            core_cells = []

            for _, start in on_network.iterrows():

                key = (int(start["snap_row"]), int(start["snap_col"]))

                if key in near_below:
                    continue

                rows, cols = _trace_downstream(ptr_src, key[0], key[1], stop_at=near_below)

                if rows.size == 0:
                    continue

                # Flag of the cell this trace joins (False if it left the DEM)
                end_r = rows[-1]
                end_c = cols[-1]
                code = int(ptr_src.read(1, window=Window(int(end_c), int(end_r), 1, 1))[0, 0])
                join = (end_r + D8_DR[code], end_c + D8_DC[code]) if 0 < code < 256 else None
                flag = near_below.get((int(join[0]), int(join[1])), False) if join else False

                xs, ys = _cell_centers(transform, rows, cols)
                near, _ = marker_tree.query(np.column_stack([xs, ys]))
                near = near <= NAMING_RADIUS_M

                for i in range(rows.size - 1, -1, -1):
                    flag = flag or bool(near[i])
                    near_below[(int(rows[i]), int(cols[i]))] = flag
                    if flag:
                        core_cells.append((int(rows[i]), int(cols[i])))

            if not core_cells:
                print(f"  WARNING: no DEM flow path near the '{name}' markers, "
                      "river not named.")
                continue

            core_rows = np.array([c[0] for c in core_cells], dtype=np.int64)
            core_cols = np.array([c[1] for c in core_cells], dtype=np.int64)

            xs, ys = _cell_centers(transform, core_rows, core_cols)
            dist, _ = cKDTree(np.column_stack([xs, ys])).query(
                np.column_stack([group.geometry.x, group.geometry.y])
            )
            n_matched = int((dist <= NAMING_RADIUS_M).sum())

            if n_matched < NAMING_MIN_MARKERS:
                print(f"  WARNING: only {n_matched} '{name}' markers within "
                      f"{NAMING_RADIUS_M} m of the DEM flow path, river not named.")
                continue

            core_acc = _sample_cells(p.acc, core_rows, core_cols)
            k = int(np.argmax(core_acc))

            cores.append((name, info["river_class"], set(core_cells),
                          (int(core_rows[k]), int(core_cols[k])), n_matched))

        claimed = {}

        for name, _, cells, _, _ in cores:
            for cell in cells:
                claimed.setdefault(cell, name)

        for name, _, _, outlet, _ in cores:

            rows, cols = _trace_downstream(ptr_src, outlet[0], outlet[1])

            for r, c in zip(rows[1:], cols[1:]):
                owner = claimed.get((int(r), int(c)))
                if owner is not None and owner != name:
                    break
                claimed[(int(r), int(c))] = name

    # Cells of every river in claim order, in one pass over `claimed`
    cells_by_river = {name: [] for name, _, _, _, _ in cores}

    for cell, owner in claimed.items():
        cells_by_river[owner].append(cell)

    rivers = {}

    for name, river_class, _, _, n_matched in cores:

        cells = cells_by_river[name]

        rows = np.array([c[0] for c in cells], dtype=np.int64)
        cols = np.array([c[1] for c in cells], dtype=np.int64)

        rivers[name] = (rows, cols, river_class, n_matched)

    return rivers


def _cells_to_lines(p, transform, shape, rows, cols):

    # Cell-center lines joining every river cell to its D8
    # downstream cell when that cell belongs to the same river
    if rows.size < 2:
        return None

    height, width = shape

    codes = _sample_cells(p.ptr, rows, cols).astype(np.int64)
    codes = np.where((codes > 0) & (codes < 256), codes, 0)

    down_rows = rows + D8_DR[codes]
    down_cols = cols + D8_DC[codes]

    # Membership test on linear cell indices (rows * width + cols);
    # the bounds check keeps off-grid targets from wrapping around
    inside = (
        (down_rows >= 0) & (down_rows < height)
        & (down_cols >= 0) & (down_cols < width)
    )

    linked = (
        D8_OK[codes]
        & inside
        & np.isin(down_rows * width + down_cols, rows * width + cols)
    )

    if not linked.any():
        return None

    x0, y0 = _cell_centers(transform, rows[linked], cols[linked])
    x1, y1 = _cell_centers(transform, down_rows[linked], down_cols[linked])

    segments = shapely.multilinestrings(
        shapely.linestrings(
            np.stack([np.column_stack([x0, y0]), np.column_stack([x1, y1])], axis=1)
        )
    )

    return line_merge(segments)


# ============================================================
# MULTI-SCALE WATERSHEDS
# ============================================================

def create_multiscale_watersheds(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # Watersheds at every scale in WATERSHED_THRESHOLDS_HA.
    #
    # Pour points of a scale:
    #   - link_outlet:   outlet of each stream link (streams with
    #                    upstream area >= threshold)
    #   - edge_outlet:   cells draining off the DEM with upstream
    #                    area >= EDGE_BASIN_MIN_HA (all scales)
    #   - marker_outlet: most downstream snapped km marker of each
    #                    river (all scales)
    # plus all pour points of the coarser scales, so units nest
    # exactly (small in medium in large). Whitebox `watershed`
    # assigns every cell to its first downstream pour point; cells
    # reaching none form the "minor_edge" unit (ws_id = 0).
    #
    # Returns a dict of summary numbers (thresholds in cells and
    # units per scale).

    print("\nCreating multi-scale watersheds...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _check_alignment(p, p.ptr, p.acc)

    _make_dirs(p)

    wbt = _new_wbt()

    with rasterio.open(p.acc) as acc:
        crs = acc.crs
        transform = acc.transform
        cell_area = abs(acc.res[0] * acc.res[1])
        edge_min_cells = _area_to_cells(EDGE_BASIN_MIN_HA * HA, acc)
        scale_cells = {
            scale: _area_to_cells(ha * HA, acc)
            for scale, ha in WATERSHED_THRESHOLDS_HA.items()
        }

    markers = _river_markers(p)

    summary = {
        "edge_min_cells": edge_min_cells,
        "scale_cells": dict(scale_cells),
        "units": {}
    }

    # --------------------------------------------------------
    # Pour points shared by every scale
    # --------------------------------------------------------
    #
    # Candidates are kept as DataFrames in insertion order. Per cell
    # the highest priority wins and, on equal priority, the first one
    # added (same rule as updating a dict point by point, but
    # vectorised for millions of outlets).

    candidates = []

    def add_pour_points(rows, cols, kind, river_name=None, km=np.nan, snap_dist=np.nan):
        rows = np.asarray(rows, dtype=np.int64)
        candidates.append(pd.DataFrame({
            "row": rows,
            "col": np.asarray(cols, dtype=np.int64),
            "type": np.full(rows.size, kind, dtype=object),
            "river_name": np.full(rows.size, river_name, dtype=object),
            "km": np.full(rows.size, km, dtype=float),
            "snap_dist_m": np.full(rows.size, snap_dist, dtype=float)
        }))

    def merge_pour_points():
        pour = pd.concat(candidates, ignore_index=True)
        priority = pour["type"].map(POUR_POINT_PRIORITY)
        pour = pour.assign(_priority=-priority).sort_values(
            ["row", "col", "_priority"], kind="stable"
        )
        pour = pour.drop_duplicates(["row", "col"], keep="first")
        pour = pour.drop(columns="_priority").reset_index(drop=True)
        candidates[:] = [pour]
        return pour.copy()

    if not markers.empty:
        for name, group in markers.groupby("river_name"):
            outlet = group.loc[group["snap_acc"].idxmax()]
            add_pour_points(
                [outlet["snap_row"]], [outlet["snap_col"]],
                "marker_outlet", name, float(outlet["km"]),
                float(outlet["snap_dist_m"])
            )

    # --------------------------------------------------------
    # Scales, coarse to fine
    # --------------------------------------------------------

    scales = sorted(scale_cells, key=lambda s: scale_cells[s], reverse=True)

    # Edge outlets and the stream outlets of every scale in one pass
    print("  Finding edge and stream outlets...")

    outlets = _find_outlets(p, {s: scale_cells[s] for s in scales}, edge_min_cells)

    add_pour_points(*outlets["edge"], "edge_outlet")

    point_tables = []
    previous_path = None
    previous_scale = None

    with tempfile.TemporaryDirectory() as tmp_dir:

        for scale in scales:

            cells = scale_cells[scale]
            out_tif = p.watersheds_dir / f"watersheds_{scale}.tif"
            out_gpkg = p.watersheds_vector_dir / f"watersheds_{scale}.gpkg"

            print(f"  [{scale}] threshold {WATERSHED_THRESHOLDS_HA[scale]} ha "
                  f"({cells} cells)")

            add_pour_points(*outlets[scale]["link"], "link_outlet")

            # Snapshot for this scale (coarser points are already in)
            pour = merge_pour_points()
            pour["ws_id"] = np.arange(1, len(pour) + 1, dtype=np.int64)

            counts, polygons = _delineate_watersheds(p, pour, out_tif, tmp_dir, wbt)

            # ------------------------------------------------
            # Attributes
            # ------------------------------------------------

            units = pd.DataFrame({"ws_id": np.arange(counts.size, dtype=np.int64)})
            units["scale"] = scale
            units["area_m2"] = (counts * cell_area).round(2)

            outlet_type = dict(zip(pour["ws_id"], pour["type"]))
            units["outlet_type"] = units["ws_id"].map(outlet_type).fillna("none")
            units["kind"] = units["outlet_type"].map({
                "link_outlet": "river",
                "marker_outlet": "river",
                "edge_outlet": "edge"
            }).fillna("minor_edge")

            if previous_path is not None:
                parents = _sample_cells(previous_path, pour["row"], pour["col"])
                parent_map = dict(zip(pour["ws_id"], parents.astype(np.int64)))
                parent_map[0] = 0
                units["parent_id"] = units["ws_id"].map(parent_map).astype("Int64")
                units["parent_scale"] = previous_scale
            else:
                units["parent_id"] = pd.Series(pd.NA, index=units.index, dtype="Int64")
                units["parent_scale"] = None

            units["has_river_km"] = False
            units["river_name"] = None
            units["km_min"] = np.nan
            units["km_max"] = np.nan
            units["n_markers"] = 0

            if not markers.empty:

                marker_ws = _sample_cells(out_tif, markers["row"], markers["col"])
                tagged = markers.assign(ws_id=marker_ws.astype(np.int64))

                marker_summary = tagged.groupby("ws_id").agg(
                    river_name=("river_name", lambda s: ";".join(sorted(set(s)))),
                    km_min=("km", "min"),
                    km_max=("km", "max"),
                    n_markers=("km", "size")
                )

                units = units.set_index("ws_id")
                units.update(marker_summary)
                units = units.reset_index()
                units["n_markers"] = units["n_markers"].astype(np.int64)
                units["has_river_km"] = units["n_markers"] > 0

                point_tables.append(pd.DataFrame({
                    "type": "marker_raw",
                    "scale": scale,
                    "ws_id": tagged["ws_id"],
                    "river_name": tagged["river_name"],
                    "km": tagged["km"],
                    "snap_dist_m": tagged["snap_dist_m"],
                    "x": tagged.geometry.x,
                    "y": tagged.geometry.y
                }))

            units = units[units["area_m2"] > 0]

            polygons = polygons.merge(units, on="ws_id", how="left")

            polygons.to_file(out_gpkg, layer=f"watersheds_{scale}", driver="GPKG")

            # ------------------------------------------------
            # Drainage points of this scale
            # ------------------------------------------------

            acc_at_pour = _sample_cells(p.acc, pour["row"], pour["col"])
            xs, ys = _cell_centers(transform, pour["row"], pour["col"])

            point_tables.append(pd.DataFrame({
                "type": pour["type"],
                "scale": scale,
                "ws_id": pour["ws_id"],
                "river_name": pour["river_name"],
                "km": pour["km"].astype(float),
                "snap_dist_m": pour["snap_dist_m"].astype(float),
                "upstream_area_m2": (acc_at_pour.astype(float) * cell_area).round(2),
                "x": xs,
                "y": ys
            }))

            conf_rows, conf_cols = outlets[scale]["confluence"]

            if conf_rows.size:
                xs, ys = _cell_centers(transform, conf_rows, conf_cols)
                point_tables.append(pd.DataFrame({
                    "type": "confluence",
                    "scale": scale,
                    "ws_id": _sample_cells(out_tif, conf_rows, conf_cols).astype(np.int64),
                    "upstream_area_m2": (
                        _sample_cells(p.acc, conf_rows, conf_cols)
                        .astype(float) * cell_area
                    ).round(2),
                    "x": xs,
                    "y": ys
                }))

            n_river = int((units["kind"] == "river").sum())
            n_km = int(units["has_river_km"].sum())

            summary["units"][scale] = {
                "units": len(units),
                "river": n_river,
                "edge": len(units) - n_river,
                "with_km": n_km
            }

            print(f"    {len(units)} units ({n_river} river, "
                  f"{len(units) - n_river} edge), {n_km} with river km markers")
            print(f"    Created: {out_tif}")
            print(f"    Created: {out_gpkg}")

            previous_path = out_tif
            previous_scale = scale

    points = pd.concat(point_tables, ignore_index=True)

    points = gpd.GeoDataFrame(
        points.drop(columns=["x", "y"]),
        geometry=gpd.points_from_xy(points["x"], points["y"]),
        crs=crs
    )

    points.to_file(p.drainage_points, layer="drainage_points", driver="GPKG")

    print(f"  Created: {p.drainage_points}")

    return summary


# ============================================================
# RIVER CENTERLINES
# ============================================================

def create_river_centerlines(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # Stream network = cells with upstream area >= CENTERLINE_THRESHOLD_HA,
    # ordered (Strahler) and vectorized with Whitebox. Main rivers
    # are traced on the D8 flow path and named with the km markers;
    # the geometry always comes from the DEM.
    #
    # Returns a dict of summary numbers.

    print("\nCreating river centerlines...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _check_alignment(p, p.ptr, p.acc)

    _make_dirs(p)

    wbt = _new_wbt()

    with rasterio.open(p.acc) as acc:
        crs = acc.crs
        transform = acc.transform
        shape = acc.shape
        res = min(abs(acc.res[0]), abs(acc.res[1]))
        cell_area = abs(acc.res[0] * acc.res[1])
        min_cells = _area_to_cells(CENTERLINE_THRESHOLD_HA * HA, acc)

    print(f"  Threshold {CENTERLINE_THRESHOLD_HA} ha ({min_cells} cells)")

    with tempfile.TemporaryDirectory() as tmp_dir:

        streams = Path(tmp_dir) / "centerline_streams.tif"
        lines_shp = Path(tmp_dir) / "centerlines.shp"

        wbt.extract_streams(
            str(p.acc.resolve()),
            str(streams.resolve()),
            min_cells
        )

        wbt.strahler_stream_order(
            str(p.ptr.resolve()),
            str(streams.resolve()),
            str(p.streams.resolve())
        )

        wbt.raster_streams_to_vector(
            str(p.streams.resolve()),
            str(p.ptr.resolve()),
            str(lines_shp.resolve())
        )

        lines = gpd.read_file(lines_shp)

    lines = lines.set_crs(crs, allow_override=True)
    lines = gpd.GeoDataFrame(
        {"strahler": lines["STRM_VAL"].astype(np.int64)},
        geometry=lines.geometry,
        crs=crs
    )
    lines.insert(0, "line_id", np.arange(1, len(lines) + 1, dtype=np.int64))

    if lines.empty:
        print("  WARNING: no streams above the centerline threshold.")

    # --------------------------------------------------------
    # Main rivers from the km markers
    # --------------------------------------------------------

    markers = _river_markers(p)

    rivers = {}

    if not markers.empty:
        rivers = _trace_main_rivers(p, markers, min_cells, transform)

    # River cells as sorted linear indices (row * width + col) with
    # the river name of each, for vectorised lookups below
    river_names = np.array(list(rivers), dtype=object)

    if rivers:
        owner_keys = np.concatenate([
            rows * shape[1] + cols for rows, cols, _, _ in rivers.values()
        ])
        owner_code = np.concatenate([
            np.full(rows.size, i, dtype=np.int64)
            for i, (rows, _, _, _) in enumerate(rivers.values())
        ])
        order = np.argsort(owner_keys, kind="stable")
        owner_keys = owner_keys[order]
        owner_code = owner_code[order]
    else:
        owner_keys = np.zeros(0, np.int64)
        owner_code = np.zeros(0, np.int64)

    # --------------------------------------------------------
    # Cells under every line (densified to half a pixel),
    # processed in chunks of lines to cap memory
    # --------------------------------------------------------

    max_acc = np.zeros(len(lines))
    line_samples = np.zeros(len(lines), dtype=np.int64)
    vote_parts = []

    geometries = lines.geometry.values

    for start in range(0, len(lines), CENTERLINE_CHUNK_LINES):

        stop = min(start + CENTERLINE_CHUNK_LINES, len(lines))

        dense = segmentize(geometries[start:stop], res / 2)
        coords, line_index = shapely.get_coordinates(dense, return_index=True)
        line_index = line_index + start

        rows, cols = rasterio.transform.rowcol(transform, coords[:, 0], coords[:, 1])

        sample_rows = np.minimum(np.clip(np.asarray(rows), 0, None), shape[0] - 1)
        sample_cols = np.minimum(np.clip(np.asarray(cols), 0, None), shape[1] - 1)

        acc_values = _sample_cells(p.acc, sample_rows, sample_cols).astype(float)

        np.maximum.at(max_acc, line_index, acc_values)

        line_samples += np.bincount(line_index, minlength=len(lines))

        # River owning each sample (-1 = none)
        if owner_keys.size == 0:
            continue

        keys = sample_rows * shape[1] + sample_cols
        pos = np.minimum(np.searchsorted(owner_keys, keys), owner_keys.size - 1)
        hit = owner_keys[pos] == keys

        if hit.any():
            pairs, n = np.unique(
                np.column_stack([line_index[hit], owner_code[pos[hit]]]),
                axis=0,
                return_counts=True
            )
            vote_parts.append(pd.DataFrame({
                "line": pairs[:, 0],
                "river": river_names[pairs[:, 1]],
                "n": n
            }))

    lines["upstream_area_m2"] = (max_acc * cell_area).round(2)
    lines["length_m"] = lines.length.round(2)

    # Share of each line's samples on each river, ordered by line then
    # river name exactly like the original groupby; a line is named
    # after a river holding >= 50% of its samples
    if vote_parts:
        share = (
            pd.concat(vote_parts, ignore_index=True)
            .groupby(["line", "river"])["n"].sum()
        )
        share = share.div(
            pd.Series(line_samples, index=pd.RangeIndex(len(lines), name="line")),
            level="line"
        ).reset_index(name="share")
    else:
        share = pd.DataFrame({"line": [], "river": [], "share": []})

    share = share[share["share"] >= 0.5].sort_values("share").drop_duplicates("line", keep="last")

    lines["river_name"] = pd.Series(
        dict(zip(share["line"], share["river"])), index=lines.index, dtype=object
    )
    lines["main_river"] = lines["river_name"].notna()

    main_rivers = gpd.GeoDataFrame(
        [
            {
                "river_name": name,
                "river_class": river_class,
                "n_markers_matched": n_matched,
                "geometry": _cells_to_lines(p, transform, shape, rows, cols)
            }
            for name, (rows, cols, river_class, n_matched) in rivers.items()
        ],
        columns=["river_name", "river_class", "n_markers_matched", "geometry"],
        geometry="geometry",
        crs=crs
    )
    main_rivers = main_rivers[main_rivers.geometry.notna()].copy()
    main_rivers["length_m"] = main_rivers.length.round(2)

    # --------------------------------------------------------
    # Validation against the km markers
    # --------------------------------------------------------

    validation = markers.drop(
        columns=["row", "col", "snap_row", "snap_col", "snap_acc", "layer_order"]
    ).copy()

    validation["dist_centerline_m"] = np.nan
    validation["dist_any_centerline_m"] = np.nan

    if not validation.empty:

        for name, (rows, cols, _, _) in rivers.items():
            sel = validation["river_name"] == name
            xs, ys = _cell_centers(transform, rows, cols)
            dist, _ = cKDTree(np.column_stack([xs, ys])).query(
                np.column_stack([validation.geometry.x[sel], validation.geometry.y[sel]])
            )
            validation.loc[sel, "dist_centerline_m"] = dist.round(2)

        if not lines.empty:
            nearest = gpd.sjoin_nearest(
                validation[["geometry"]],
                lines[["geometry"]],
                distance_col="dist"
            )
            nearest = nearest[~nearest.index.duplicated()]
            validation["dist_any_centerline_m"] = nearest["dist"].round(2)

    for buffer in VALIDATION_BUFFERS_M:
        validation[f"within_{buffer:g}m"] = validation["dist_centerline_m"] <= buffer

    validation["deviation"] = ~(validation["dist_centerline_m"] <= max(VALIDATION_BUFFERS_M))

    # --------------------------------------------------------
    # Write
    # --------------------------------------------------------

    lines.to_file(p.centerlines, layer="centerlines", driver="GPKG")
    main_rivers.to_file(p.centerlines, layer="main_rivers", driver="GPKG")
    validation.to_file(p.centerlines, layer="validation_points", driver="GPKG")

    print(f"  Stream links: {len(lines)}, main rivers: {len(main_rivers)}")

    for name, group in validation.groupby("river_name"):

        dist = group["dist_centerline_m"]

        if dist.isna().all():
            print(f"  {name}: not traced ({len(group)} markers)")
            continue

        within = ", ".join(
            f"{(dist <= b).mean() * 100:.0f}% <= {b:g} m" for b in VALIDATION_BUFFERS_M
        )

        print(f"  {name}: {len(group)} markers, offset median {dist.median():.1f} m, "
              f"max {dist.max():.1f} m ({within})")

        deviating = group[group["deviation"]].sort_values("km")

        if not deviating.empty:
            print(f"    Deviation > {max(VALIDATION_BUFFERS_M):g} m at km: "
                  + ", ".join(f"{k:g}" for k in deviating["km"]))

    print(f"  Created: {p.streams}")
    print(f"  Created: {p.centerlines}")

    return {
        "min_cells": min_cells,
        "stream_links": len(lines),
        "main_rivers": len(main_rivers),
        "markers": len(markers)
    }


# ============================================================
# RIVER BED
# ============================================================

def create_river_bed(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # River bed = cells whose height above the nearest main river cell
    # (HAND, Whitebox elevation_above_stream_euclidean on the raw DEM)
    # is below a threshold, within BED_MAX_DISTANCE_M of the main river
    # centerline, and connected to the centerline (Whitebox clump).
    #
    # The raw DEM is used because depression filling raises the river
    # surface and floodplain hollows, which flattens HAND to 0 there.
    #
    # Threshold = HAND_PERCENTILE of HAND over water-mask cells within
    # WATER_CHECK_BUFFER_M of the rivers, clamped to HAND_CLAMP_M.
    #
    # Blocks with no cell near a river are skipped (they cannot hold
    # bed or check cells), which is most of a large DEM.
    #
    # Returns a dict with the threshold used (None if skipped).

    print("\nCreating river bed...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _check_alignment(p, p.water)

    if not p.centerlines.exists():
        raise FileNotFoundError(
            f"{p.centerlines} not found. Run create_river_centerlines() first."
        )

    rivers = gpd.read_file(p.centerlines, layer="main_rivers")

    if rivers.empty:
        print("  WARNING: no main rivers in the AOI, river bed skipped.")
        return {"threshold": None, "raw_threshold": None, "samples": 0, "fallback": False}

    rivers = rivers.reset_index(drop=True)
    rivers["code"] = np.arange(1, len(rivers) + 1, dtype=np.int16)

    _make_dirs(p)

    wbt = _new_wbt()

    raw_threshold = None
    near_distance = max(BED_MAX_DISTANCE_M, WATER_CHECK_BUFFER_M)

    def near_rivers(dist, dist_src, limit):
        return _valid(dist, dist_src.nodata) & (dist <= limit)

    with tempfile.TemporaryDirectory() as tmp_dir:

        river_raster = Path(tmp_dir) / "main_rivers.tif"
        distance = Path(tmp_dir) / "river_distance.tif"
        allocation = Path(tmp_dir) / "river_allocation.tif"

        # ----------------------------------------------------
        # Rasterize main rivers (code per river, 0 elsewhere)
        # ----------------------------------------------------

        with rasterio.open(p.dem) as dem:

            cell_area = abs(dem.res[0] * dem.res[1])

            with rasterio.open(
                river_raster, "w", **_output_profile(dem, "int16", -32768)
            ) as dst:

                for window in _iter_windows(dem):

                    window_box = box(*rasterio.windows.bounds(window, dem.transform))
                    hits = rivers.iloc[rivers.sindex.query(window_box)]

                    arr = np.zeros((window.height, window.width), dtype=np.int16)

                    if not hits.empty:
                        arr = features.rasterize(
                            zip(hits.geometry, hits["code"].astype(int)),
                            out_shape=arr.shape,
                            transform=dem.window_transform(window),
                            fill=0,
                            dtype="int16"
                        )

                    dst.write(arr, 1, window=window)

        wbt.elevation_above_stream_euclidean(
            str(p.dem.resolve()),
            str(river_raster.resolve()),
            str(p.hand.resolve())
        )

        wbt.euclidean_distance(str(river_raster.resolve()), str(distance.resolve()))
        wbt.euclidean_allocation(str(river_raster.resolve()), str(allocation.resolve()))

        with rasterio.open(p.hand) as hand_src, \
             rasterio.open(distance) as dist_src, \
             rasterio.open(allocation) as alloc_src, \
             rasterio.open(p.water) as water_src:

            # ------------------------------------------------
            # Pass 1: HAND threshold from water cells near rivers
            # (only water-mask samples are kept in memory)
            # ------------------------------------------------

            samples = []

            for window in _iter_windows(hand_src):

                dist = dist_src.read(1, window=window)
                near = near_rivers(dist, dist_src, WATER_CHECK_BUFFER_M)

                if not near.any():
                    continue

                hand = hand_src.read(1, window=window)
                water = water_src.read(1, window=window) == 1

                sel = (
                    water
                    & _valid(hand, hand_src.nodata)
                    & near
                )

                samples.append(hand[sel].astype(np.float64))

            samples = np.concatenate(samples) if samples else np.zeros(0)

            if samples.size:
                raw_threshold = float(np.percentile(samples, HAND_PERCENTILE))
                threshold = float(np.clip(raw_threshold, *HAND_CLAMP_M))
                print(f"  HAND P{HAND_PERCENTILE} over {samples.size} water cells "
                      f"near rivers: {raw_threshold:.2f} m "
                      f"-> threshold {threshold:.2f} m (clamp {HAND_CLAMP_M})")
            else:
                threshold = HAND_FALLBACK_M
                print(f"  WARNING: no water-mask cells near the rivers, "
                      f"using HAND_FALLBACK_M = {threshold} m")

            # ------------------------------------------------
            # Pass 2: candidate cells (low HAND, near a river)
            # ------------------------------------------------

            candidate_path = Path(tmp_dir) / "bed_candidates.tif"
            clump_path = Path(tmp_dir) / "bed_clumps.tif"

            with rasterio.open(
                candidate_path, "w", **_output_profile(hand_src, "uint8", None)
            ) as dst:

                for window in _iter_windows(hand_src):

                    dist = dist_src.read(1, window=window)
                    candidate = near_rivers(dist, dist_src, BED_MAX_DISTANCE_M)

                    if candidate.any():
                        hand = hand_src.read(1, window=window)
                        candidate &= _valid(hand, hand_src.nodata) & (hand <= threshold)

                    dst.write(candidate.astype(np.uint8), 1, window=window)

            # ------------------------------------------------
            # Pass 3: keep candidate patches touching a river
            # ------------------------------------------------

            wbt.clump(
                str(candidate_path.resolve()),
                str(clump_path.resolve()),
                diag=True,
                zero_back=True
            )

            keep_ids = set()

            with rasterio.open(clump_path) as clump_src, \
                 rasterio.open(river_raster) as river_src:

                for window in _iter_windows(clump_src):

                    river = river_src.read(1, window=window) > 0

                    if not river.any():
                        continue

                    clumps = clump_src.read(1, window=window)

                    on_river = (
                        river
                        & _valid(clumps, clump_src.nodata)
                        & (clumps > 0)
                    )

                    keep_ids.update(np.unique(clumps[on_river]).tolist())

            keep_ids = np.array(sorted(keep_ids))

            # ------------------------------------------------
            # Pass 4: bed raster (river code, 0 = not bed)
            # ------------------------------------------------

            n_codes = len(rivers) + 1
            bed_cells = np.zeros(n_codes, np.int64)
            bed_water_cells = np.zeros(n_codes, np.int64)
            water_near_cells = 0
            water_near_in_bed = 0

            with rasterio.open(clump_path) as clump_src, \
                 rasterio.open(
                     p.bed, "w", **_output_profile(hand_src, "int16", 0)
                 ) as dst:

                for window in _iter_windows(hand_src):

                    dist = dist_src.read(1, window=window)

                    if not near_rivers(dist, dist_src, near_distance).any():
                        dst.write(
                            np.zeros((window.height, window.width), np.int16),
                            1,
                            window=window
                        )
                        continue

                    clumps = clump_src.read(1, window=window)
                    code = alloc_src.read(1, window=window)
                    water = water_src.read(1, window=window) == 1

                    near_check = near_rivers(dist, dist_src, WATER_CHECK_BUFFER_M)

                    bed = (
                        _valid(clumps, clump_src.nodata)
                        & np.isin(clumps, keep_ids)
                        & _valid(code, alloc_src.nodata)
                        & (code > 0)
                    )

                    out = np.where(bed, code, 0).astype(np.int16)

                    dst.write(out, 1, window=window)

                    bed_cells += np.bincount(out[bed], minlength=n_codes)[:n_codes]
                    bed_water_cells += np.bincount(out[bed & water], minlength=n_codes)[:n_codes]
                    water_near_cells += int((water & near_check).sum())
                    water_near_in_bed += int((water & near_check & bed).sum())

    # --------------------------------------------------------
    # Polygons + cross-check with the water mask
    # --------------------------------------------------------

    polygons = _polygonize_raster(p.bed, "code")
    polygons = polygons.merge(
        rivers[["code", "river_name", "river_class"]], on="code", how="left"
    )

    polygons["area_m2"] = (polygons["code"].map(dict(enumerate(bed_cells))) * cell_area).round(2)
    polygons["water_overlap_m2"] = (
        polygons["code"].map(dict(enumerate(bed_water_cells))) * cell_area
    ).round(2)
    polygons["hand_threshold_m"] = round(threshold, 3)

    polygons.to_file(p.bed_gpkg, layer="river_bed", driver="GPKG")

    total_bed = int(bed_cells.sum())

    if water_near_cells:
        print(f"  Water-mask cells within {WATER_CHECK_BUFFER_M:g} m of rivers "
              f"inside the bed: {water_near_in_bed / water_near_cells * 100:.1f}%")

    if total_bed:
        print(f"  Bed cells that are water-mask cells: "
              f"{bed_water_cells.sum() / total_bed * 100:.1f}%")

    for _, row in polygons.iterrows():
        print(f"  {row['river_name']}: {row['area_m2']:.0f} m2 of river bed")

    print(f"  Created: {p.hand}")
    print(f"  Created: {p.bed}")
    print(f"  Created: {p.bed_gpkg}")

    return {
        "threshold": threshold,
        "raw_threshold": raw_threshold,
        "samples": int(samples.size),
        "fallback": raw_threshold is None
    }


# ============================================================
# CENTERLINES vs KM MARKERS
# ============================================================

MARKER_STATUS = ("on_centerline", "near_offset", "no_centerline_nearby")


def _stream_cell_offsets(streams_path, rows, cols, radius):

    # For every (row, col): Chebyshev distance in cells (0 = same cell,
    # 1 = adjacent) to the nearest stream cell within `radius` cells,
    # NaN if there is none. The stream raster is read once per tile of
    # markers, with a `radius` halo.
    offsets = np.full(rows.size, np.nan)

    if rows.size == 0:
        return offsets

    with rasterio.open(streams_path) as src:

        for sel in _group_by_tile(rows, cols, src.width).values():

            r0 = int(rows[sel].min()) - radius
            c0 = int(cols[sel].min()) - radius

            block = src.read(
                1,
                window=Window(
                    c0, r0,
                    int(cols[sel].max()) - c0 + radius + 1,
                    int(rows[sel].max()) - r0 + radius + 1
                ),
                boundless=True,
                fill_value=_fill_value(src)
            )

            stream = _valid(block, src.nodata) & (block > 0)

            for i in sel:

                r = rows[i] - r0
                c = cols[i] - c0

                sr, sc = np.nonzero(
                    stream[r - radius:r + radius + 1, c - radius:c + radius + 1]
                )

                if sr.size:
                    offsets[i] = np.maximum(np.abs(sr - radius), np.abs(sc - radius)).min()

    return offsets


def create_marker_check(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # Do the DEM-derived centerlines line up with the official river
    # km markers? For every marker on a valid DEM cell:
    #
    #   dist_centerline_m  distance to the nearest stream centerline
    #                      (all streams, "centerlines" layer)
    #   dist_own_river_m   distance to the centerline named after the
    #                      marker's river (NaN if that river was not named)
    #   cell_offset        cells (Chebyshev: 0 = same cell, 1 = adjacent)
    #                      between the marker's cell and the nearest cell
    #                      of the centerline raster (river_streams.tif),
    #                      only for markers within MARKER_CHECK_DISTANCE_M
    #   status             on_centerline        cell_offset <= MARKER_ON_TOLERANCE_CELLS
    #                      near_offset          a centerline within
    #                                           MARKER_CHECK_DISTANCE_M,
    #                                           but farther than the tolerance
    #                      no_centerline_nearby nothing within that distance
    #
    # CRS and grid of the markers, centerlines and centerline raster are
    # checked against the DEM first. Writes MARKER_CHECK_GPKG and
    # returns a summary dict.

    print("\nChecking centerlines against river km markers...")

    p = _paths(dem_path, output_dir, water_path, km_gpkg)

    _make_dirs(p)

    for path in (p.streams, p.centerlines):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Run create_river_centerlines() first."
            )

    with rasterio.open(p.dem) as dem:
        crs = dem.crs
        transform = dem.transform
        shape = dem.shape
        res = min(abs(dem.res[0]), abs(dem.res[1]))

    # --------------------------------------------------------
    # Same CRS and grid
    # --------------------------------------------------------

    with rasterio.open(p.streams) as streams:
        grid_ok = (
            streams.crs == crs
            and streams.transform == transform
            and streams.shape == shape
        )

    if not grid_ok:
        raise ValueError(
            f"{p.streams} is not on the DEM grid. Re-run create_river_centerlines()."
        )

    lines = gpd.read_file(p.centerlines, layer="centerlines")
    rivers = gpd.read_file(p.centerlines, layer="main_rivers")

    lines_crs_ok = lines.crs is None or lines.crs == crs

    marker_crs = {}
    if p.km_gpkg.exists():
        for layer in gpd.list_layers(p.km_gpkg)["name"]:
            marker_crs[layer] = gpd.read_file(p.km_gpkg, layer=layer, rows=0).crs

    reprojected = sorted(
        layer for layer, layer_crs in marker_crs.items()
        if layer_crs is not None and layer_crs != crs
    )

    print(f"  Centerline raster on the DEM grid: yes; centerlines in DEM CRS: "
          f"{'yes' if lines_crs_ok else 'NO'}")

    if reprojected:
        print(f"  Marker layers reprojected to the DEM CRS: {', '.join(reprojected)}")

    # --------------------------------------------------------
    # Distances
    # --------------------------------------------------------

    markers = _read_river_markers(p)

    check = gpd.GeoDataFrame(
        {
            "river_name": markers["river_name"].astype(object),
            "river_class": markers["river_class"].astype(object),
            "km": markers["km"].astype(float),
        },
        geometry=markers.geometry.values,
        crs=crs
    )

    check["dist_centerline_m"] = np.nan
    check["nearest_line_id"] = pd.Series(pd.NA, index=check.index, dtype="Int64")
    check["nearest_strahler"] = pd.Series(pd.NA, index=check.index, dtype="Int64")
    check["dist_own_river_m"] = np.nan
    check["cell_offset"] = np.nan
    check["status"] = "no_centerline_nearby"

    if not check.empty and not lines.empty:

        nearest = gpd.sjoin_nearest(
            check[["geometry"]],
            lines.set_crs(crs, allow_override=True)[["line_id", "strahler", "geometry"]],
            distance_col="dist"
        )
        nearest = nearest[~nearest.index.duplicated()]

        check["dist_centerline_m"] = nearest["dist"].round(2)
        check["nearest_line_id"] = nearest["line_id"].astype("Int64")
        check["nearest_strahler"] = nearest["strahler"].astype("Int64")

    for _, river in rivers.iterrows():
        sel = (check["river_name"] == river["river_name"]).to_numpy()
        if sel.any():
            check.loc[sel, "dist_own_river_m"] = np.round(
                shapely.distance(check.geometry.values[sel], river.geometry), 2
            )

    near = (check["dist_centerline_m"] <= MARKER_CHECK_DISTANCE_M).to_numpy()

    radius = int(np.ceil(MARKER_CHECK_DISTANCE_M / res))
    offsets = _stream_cell_offsets(
        p.streams,
        markers["row"].to_numpy(np.int64)[near],
        markers["col"].to_numpy(np.int64)[near],
        radius
    )

    check.loc[near, "cell_offset"] = offsets

    on = near & (check["cell_offset"] <= MARKER_ON_TOLERANCE_CELLS).to_numpy()
    check.loc[near, "status"] = "near_offset"
    check.loc[on, "status"] = "on_centerline"

    check.to_file(p.marker_check, layer="marker_check", driver="GPKG")

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    counts = check["status"].value_counts()

    summary = {
        "markers": len(check),
        "grid_ok": grid_ok,
        "lines_crs_ok": lines_crs_ok,
        "reprojected_layers": reprojected,
        "search_distance_m": MARKER_CHECK_DISTANCE_M,
        "tolerance_cells": MARKER_ON_TOLERANCE_CELLS,
        **{status: int(counts.get(status, 0)) for status in MARKER_STATUS},
        "median_dist_near_m": (
            float(check.loc[near, "dist_centerline_m"].median()) if near.any() else None
        ),
        "rivers": {}
    }

    for name, group in check.groupby("river_name"):
        group_counts = group["status"].value_counts()
        summary["rivers"][name] = {
            "markers": len(group),
            **{status: int(group_counts.get(status, 0)) for status in MARKER_STATUS}
        }

    if check.empty:
        print("  WARNING: no river km markers on the DEM, nothing to check.")
    else:
        print(f"  {len(check)} markers: {summary['on_centerline']} on the centerline "
              f"(<= {MARKER_ON_TOLERANCE_CELLS} cell), {summary['near_offset']} near but "
              f"offset, {summary['no_centerline_nearby']} with no centerline within "
              f"{MARKER_CHECK_DISTANCE_M:g} m")

        for name, info in summary["rivers"].items():
            print(f"    {name}: {info['on_centerline']}/{info['markers']} on, "
                  f"{info['near_offset']} offset, {info['no_centerline_nearby']} none nearby")

    print(f"  Created: {p.marker_check}")

    return summary


def run_river_analysis(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    # Multi-scale watersheds, river centerlines and river bed.
    # Reuses filled_dem, flow_direction and flow_accumulation from
    # create_hydrology(); does not overwrite any existing output.

    args = (dem_path, output_dir, water_path, km_gpkg)
    p = _paths(*args)

    validate_rasters(*args)

    print("=" * 60)
    print("RIVER ANALYSIS")
    print("=" * 60)

    create_multiscale_watersheds(*args)
    create_river_centerlines(*args)
    create_river_bed(*args)

    if CFG["steps"]["marker_check"]:
        create_marker_check(*args)

    print("\n" + "=" * 60)
    print("RIVER ANALYSIS COMPLETE")
    print("=" * 60)

    print("\nOutputs:")
    print(f"  Watersheds (tif):  {p.watersheds_dir}")
    print(f"  Vector files:      {p.vector_dir}")
    print(f"  River streams:     {p.streams}")
    print(f"  HAND:              {p.hand}")
    print(f"  River bed:         {p.bed}")
    print(f"  Marker check:      {p.marker_check}")


# ============================================================
# MAIN
# ============================================================

def main(
    dem_path=DEM_PATH,
    output_dir=OUTPUT_DIR,
    water_path=WATER_PATH,
    km_gpkg=RIVER_KM_GPKG
):

    args = (dem_path, output_dir, water_path, km_gpkg)
    p = _paths(*args)

    validate_rasters(*args)

    print("=" * 60)
    print("TERRAIN ANALYSIS")
    print("=" * 60)

    _make_dirs(p)

    create_slope_aspect(*args)
    create_hydrology(*args)
    create_drainage_water(*args)

    print("\n" + "=" * 60)
    print("TERRAIN ANALYSIS COMPLETE")
    print("=" * 60)

    print("\nOutputs:")
    print(f"  Slope:             {p.slope}")
    print(f"  Aspect:            {p.aspect}")
    print(f"  Filled DEM:        {p.filled}")
    print(f"  Flow direction:    {p.ptr}")
    print(f"  Flow accumulation: {p.acc}")
    print(f"  Watersheds:        {p.basins}")
    print(f"  Drainage water:    {p.drainage}")

if __name__ == "__main__":
    run_river_analysis()
