from pathlib import Path
import json
import shutil
import tempfile
import zipfile

import laspy
import numpy as np
import rasterio

from rasterio.transform import from_origin
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt
from scipy.spatial import cKDTree

import PipelineConfig


# ============================================================
# SETTINGS (from config/config.toml)
# ============================================================

CFG = PipelineConfig.load()

MANIFEST_PATH = Path(CFG["paths"]["manifest"])

DOWNLOAD_DIR = Path(CFG["paths"]["download_dir"])
EXTRACT_DIR = Path(CFG["paths"]["extract_dir"])

OUTPUT_DEM = Path(CFG["paths"]["dem"])
OUTPUT_WATER = Path(CFG["paths"]["water"])

RESOLUTION = CFG["dem"]["resolution"]
GROUND_CLASSES = CFG["dem"]["ground_classes"]
WATER_CLASS = CFG["dem"]["water_class"]


# ============================================================
# PERFORMANCE SETTINGS (from config/config.toml)
# ============================================================

# DEMs with up to this many cells are built in memory exactly as
# before (~18 bytes per cell at peak). Larger DEMs are built tile by
# tile in temporary files on disk and gap-filled block by block.
MAX_IN_MEMORY_CELLS = CFG["dem"]["max_in_memory_cells"]

# Block size (cells per side) of the on-disk gap filling
FILL_BLOCK_SIZE = CFG["dem"]["fill_block_size"]

# Cells of context read around each block for the gap filling;
# gaps wider than this are filled from a KD-tree instead
FILL_HALO = CFG["dem"]["fill_halo"]

# Temporary files of the on-disk path (None = system temp folder);
# needs ~5 bytes per DEM cell of free space
TEMP_DIR = CFG["dem"]["temp_dir"] or None

# Only these LAZ fields are decompressed (LAS 1.4 point formats 6-10
# store fields in separate layers; skipping RGB, GPS time, etc. makes
# reading about 2x faster)
LAZ_FIELDS = (
    laspy.DecompressionSelection.XY_RETURNS_CHANNEL
    | laspy.DecompressionSelection.Z
    | laspy.DecompressionSelection.CLASSIFICATION
)


# ============================================================
# LOAD MANIFEST
# ============================================================

def load_manifest():

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"Manifest not found: {MANIFEST_PATH}"
        )

    with open(
        MANIFEST_PATH,
        "r",
        encoding="utf-8"
    ) as f:
        manifest = json.load(f)

    return manifest


# ============================================================
# GET REQUIRED TILE NAMES + .laz
# ============================================================

def get_required_tiles(manifest):

    required = set()

    for tile in manifest["tiles"]:
        required.add(tile["tile"] + ".laz")

    print(f"Required LAZ tiles: {len(required):,}")

    return required


# ============================================================
# EXTRACT REQUIRED LAZ FILES
# ============================================================

def extract_required_tiles(required):

    EXTRACT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    found = set()

    zip_files = list(
        DOWNLOAD_DIR.glob("*.zip")
    )

    print(
        f"Searching {len(zip_files):,} downloaded archives..."
    )

    for zip_path in zip_files:

        # Every required tile found: no need to open more archives
        if found == required:
            break

        with zipfile.ZipFile(zip_path, "r") as archive:

            for member in archive.namelist():

                filename = Path(member).name

                if filename not in required:
                    continue

                output = EXTRACT_DIR / filename

                # Already extracted
                if output.exists():
                    found.add(filename)
                    continue

                print(f"Extracting {filename}")

                # Avoid extracting directory structure; streamed
                # copy, so the tile is never fully held in memory
                with archive.open(member) as source:
                    with open(output, "wb") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)

                found.add(filename)

    missing = required - found

    if missing:

        print("\nWARNING — missing tiles:")

        for tile in sorted(missing):
            print(f"  {tile}")

        raise RuntimeError(
            "Some required LAZ files could not be found."
        )

    print(
        f"\nAll {len(required):,} required tiles are available."
    )


# ============================================================
# DEM GRID
# ============================================================

def dem_grid(manifest):

    bbox = manifest["requested_bbox"]

    xmin = bbox["xmin"]
    xmax = bbox["xmax"]
    ymin = bbox["ymin"]
    ymax = bbox["ymax"]

    width = int(
        np.ceil(
            (xmax - xmin) / RESOLUTION
        )
    )

    height = int(
        np.ceil(
            (ymax - ymin) / RESOLUTION
        )
    )

    print("\nDEM dimensions:")
    print(f"  Columns: {width:,}")
    print(f"  Rows:    {height:,}")
    print(f"  Cells:   {width * height:,}")

    return height, width, xmin, xmax, ymin, ymax


# ============================================================
# CREATE EMPTY DEM (in-memory path)
# ============================================================

def create_dem_array(height, width):

    dem = np.full(
        (height, width),
        -np.inf,
        dtype=np.float32
    )

    water_map = np.zeros(
    (height, width),
    dtype=np.uint8
    )

    return dem, water_map


# ============================================================
# READ ONE LAZ TILE INTO DEM CELLS
# ============================================================

def read_laz_cells(
    laz_path,
    shape,
    tile_bounds,
    xmin,
    xmax,
    ymin,
    ymax
):

    # Returns the raster rows/cols, Z and water flag of the tile's
    # MNT points inside the requested area (None if there are none)

    print(f"Processing {laz_path.name}")

    las = laspy.read(
        laz_path,
        decompression_selection=LAZ_FIELDS
    )

    # --------------------------------------------------------
    # Filter MNT classes
    # --------------------------------------------------------

    classification = np.asarray(
        las.classification
    )

    class_mask = np.isin(
        classification,
        GROUND_CLASSES
    )

    x = np.asarray(las.x)[class_mask]
    y = np.asarray(las.y)[class_mask]
    z = np.asarray(las.z)[class_mask]
    water_mask = classification[class_mask] == WATER_CLASS

    # --------------------------------------------------------
    # Crop to exact requested area
    # --------------------------------------------------------

    tile_xmin = tile_bounds["xmin"]
    tile_xmax = tile_bounds["xmax"]
    tile_ymin = tile_bounds["ymin"]
    tile_ymax = tile_bounds["ymax"]

    effective_xmin = max(xmin, tile_xmin)
    effective_xmax = min(xmax, tile_xmax)
    effective_ymin = max(ymin, tile_ymin)
    effective_ymax = min(ymax, tile_ymax)


    bbox_mask = (
        (x >= effective_xmin)
        & (x < effective_xmax)
        & (y >= effective_ymin)
        & (y < effective_ymax)
    )

    x = x[bbox_mask]
    y = y[bbox_mask]
    z = z[bbox_mask]
    water_mask = water_mask[bbox_mask]

    if len(x) == 0:
        #print(f"No points in requested bounds for this tile: {laz_path.name}")
        return None

    # --------------------------------------------------------
    # Convert X/Y into raster row/column
    #
    # Raster rows start at NORTH/top.
    # --------------------------------------------------------

    cols = (
        (x - xmin) / RESOLUTION
    ).astype(np.int64)

    rows = (
        (ymax - y) / RESOLUTION
    ).astype(np.int64)

    # Numerical boundary protection
    valid = (
        (rows >= 0)
        & (rows < shape[0])
        & (cols >= 0)
        & (cols < shape[1])
    )

    return (
        rows[valid],
        cols[valid],
        z[valid].astype(np.float32),
        water_mask[valid]
    )


def burn_cells(dem, water_map, cells):

    rows, cols, z, water_mask = cells

    # --------------------------------------------------------
    # Highest Z value per 0.5 m cell
    # --------------------------------------------------------

    np.maximum.at(
        dem,
        (rows, cols),
        z
    )

    # --------------------------------------------------------
    # Watermask making
    # --------------------------------------------------------

    water_map[
        rows[water_mask],
        cols[water_mask]
    ] = 1


def process_laz(
    laz_path,
    dem,
    water_map,
    tile_bounds,
    xmin,
    xmax,
    ymin,
    ymax
):

    cells = read_laz_cells(
        laz_path, dem.shape, tile_bounds, xmin, xmax, ymin, ymax
    )

    if cells is not None:
        burn_cells(dem, water_map, cells)


def process_laz_on_disk(
    laz_path,
    dem_src,
    water_src,
    tile_bounds,
    xmin,
    xmax,
    ymin,
    ymax
):

    # Same as process_laz, but the DEM and water mask live in
    # (sparse) GeoTIFFs: only the window covered by this tile is
    # read, updated with the same max rule and written back
    cells = read_laz_cells(
        laz_path, dem_src.shape, tile_bounds, xmin, xmax, ymin, ymax
    )

    if cells is None:
        return

    rows, cols, z, water_mask = cells

    r0, c0 = int(rows.min()), int(cols.min())
    window = Window(
        c0, r0,
        int(cols.max()) - c0 + 1,
        int(rows.max()) - r0 + 1
    )

    dem = dem_src.read(1, window=window)
    water_map = water_src.read(1, window=window)

    burn_cells(dem, water_map, (rows - r0, cols - c0, z, water_mask))

    dem_src.write(dem, 1, window=window)
    water_src.write(water_map, 1, window=window)


# ============================================================
# FILL EMPTY CELLS
# ============================================================

def fill_empty_cells(dem):

    missing = np.isneginf(dem)

    number_missing = np.count_nonzero(
        missing
    )

    print(
        f"\nEmpty cells before interpolation: "
        f"{number_missing:,}"
    )

    if number_missing == 0:
        return dem

    if np.all(missing):
        raise RuntimeError(
            "DEM contains no valid elevation points."
        )

    # Find nearest valid raster cell for each missing cell
    indices = distance_transform_edt(
        missing,
        return_distances=False,
        return_indices=True
    )

    filled = dem.copy()

    filled[missing] = dem[
        tuple(
            index[missing]
            for index in indices
        )
    ]

    return filled


def _iter_blocks(height, width, size=FILL_BLOCK_SIZE):

    for row in range(0, height, size):
        for col in range(0, width, size):
            yield Window(
                col, row,
                min(size, width - col),
                min(size, height - row)
            )


def _read_with_halo(src, window, halo):

    # Block grown by `halo` cells; cells outside the raster read as
    # empty (-inf), so they are never used as fill sources
    return src.read(
        1,
        window=Window(
            window.col_off - halo,
            window.row_off - halo,
            window.width + 2 * halo,
            window.height + 2 * halo
        ),
        boundless=True,
        fill_value=-np.inf
    )


def fill_empty_cells_on_disk(raw_src, dst):

    # Nearest valid cell fill (same rule as fill_empty_cells) for
    # DEMs too large for memory, block by block.
    #
    # Each block is read with FILL_HALO cells of context. A missing
    # cell whose nearest valid cell in that window is at most
    # FILL_HALO away has found its true nearest cell (anything outside
    # the window is farther). Cells farther than FILL_HALO from any
    # data (large gaps) use a KD-tree instead. Its points only need
    # to be the valid cells on the rim of such gaps: the nearest
    # valid cell v of a cell in a large gap is also, within a cell,
    # at distance r = FILL_HALO / 4 of a missing cell whose own
    # nearest distance is r. So the KD-tree holds the valid cells
    # lying r cells from "rim" cells with nearest distance about r.
    #
    # The distance found is always exact; with several equally near
    # valid cells another one may be picked than by the global
    # transform, so filled values can differ at such ties only.
    halo = FILL_HALO
    rim = FILL_HALO / 4
    height, width = raw_src.shape
    inner = (slice(halo, -halo), slice(halo, -halo))

    # --------------------------------------------------------
    # Pass 1: count gaps, collect large-gap rim cells
    # --------------------------------------------------------

    number_missing = 0
    any_valid = False
    large_gap_blocks = 0
    rim_rows, rim_cols, rim_values = [], [], []

    for window in _iter_blocks(height, width):

        arr = _read_with_halo(raw_src, window, halo)
        missing = np.isneginf(arr)

        block_missing = int(missing[inner].sum())
        number_missing += block_missing
        any_valid |= bool((~missing[inner]).any())

        if missing.all():
            large_gap_blocks += 1
            continue

        if not missing.any():
            continue

        dist = distance_transform_edt(missing)

        large_gap_blocks += bool((dist[inner] > halo).any())

        # A block may hold rim cells of a large gap whose far cells
        # lie in another block, so rims are collected wherever a
        # gap is at least r wide
        if not (dist >= rim - 1.5).any():
            continue

        # Valid cells within r + 1.5 cells of a rim cell (the extra
        # 1.5 covers rounding to cell centers; a few more points in
        # the tree cost nothing)
        rim_cells = missing & (np.abs(dist - rim) <= 1.5)
        near_rim = distance_transform_edt(~rim_cells) <= rim + 1.5

        sel = np.zeros(arr.shape, dtype=bool)
        sel[inner] = (near_rim & ~missing)[inner]

        rows, cols = np.nonzero(sel)
        rim_rows.append(rows - halo + window.row_off)
        rim_cols.append(cols - halo + window.col_off)
        rim_values.append(arr[rows, cols])

    print(
        f"\nEmpty cells before interpolation: "
        f"{number_missing:,}"
    )

    if not any_valid:
        raise RuntimeError(
            "DEM contains no valid elevation points."
        )

    tree = None

    if large_gap_blocks:

        rim_rows = np.concatenate(rim_rows)
        rim_cols = np.concatenate(rim_cols)
        rim_values = np.concatenate(rim_values)

        print(f"  Large gaps in {large_gap_blocks:,} blocks, "
              f"{rim_rows.size:,} rim cells in the KD-tree")

        tree = cKDTree(np.column_stack([rim_rows, rim_cols]))

    # --------------------------------------------------------
    # Pass 2: fill and write every block
    # --------------------------------------------------------

    for window in _iter_blocks(height, width):

        arr = _read_with_halo(raw_src, window, halo)
        missing = np.isneginf(arr)

        block = arr[inner].copy()
        block_missing = missing[inner]

        if block_missing.any() and not missing.all():

            dist, indices = distance_transform_edt(missing, return_indices=True)

            local = block_missing & (dist[inner] <= halo)
            block[local] = arr[indices[0][inner][local], indices[1][inner][local]]

        far = np.isneginf(block)

        if far.any():
            rows, cols = np.nonzero(far)
            _, k = tree.query(
                np.column_stack([rows + window.row_off, cols + window.col_off]),
                workers=-1
            )
            block[far] = rim_values[k]

        dst.write(block, 1, window=window)


# ============================================================
# SAVE GEOTIFF
# ============================================================

def dem_profile(height, width, xmin, ymax):

    transform = from_origin(
        xmin,
        ymax,
        RESOLUTION,
        RESOLUTION
    )

    return dict(

        driver="GTiff",

        height=height,
        width=width,

        count=1,

        dtype="float32",

        crs="EPSG:2169",

        transform=transform,

        compress="deflate",

        tiled=True,

        nodata=np.nan,

        BIGTIFF="IF_SAFER"

    )


def water_profile(height, width, xmin, ymax):

    profile = dem_profile(height, width, xmin, ymax)

    profile.update(
        dtype="uint8",
        nodata=0
    )

    return profile


def save_dem(
    dem,
    xmin,
    ymax
):

    print(f"\nSaving DEM: {OUTPUT_DEM}")

    with rasterio.open(
        OUTPUT_DEM,
        "w",
        **dem_profile(*dem.shape, xmin, ymax)
    ) as dst:

        dst.write(
            dem,
            1
        )

    print("DEM saved successfully.")


# ============================================================
# SAVE WATERMASK
# ============================================================

def save_water_map(water_map, xmin, ymax):

    with rasterio.open(
        OUTPUT_WATER,
        "w",
        **water_profile(*water_map.shape, xmin, ymax)
    ) as dst:

        dst.write(water_map, 1)

    print("Water map saved successfully.")


# ============================================================
# BUILD (in memory / on disk)
# ============================================================

def build_in_memory(manifest, grid):

    height, width, xmin, xmax, ymin, ymax = grid

    dem, water_map = create_dem_array(height, width)

    for tile in manifest["tiles"]:

        filename = tile["tile"] + ".laz"
        laz_path = EXTRACT_DIR / filename

        process_laz(
            laz_path,
            dem,
            water_map,
            tile["bounds"],
            xmin,
            xmax,
            ymin,
            ymax
        )

    dem = fill_empty_cells(
        dem
    )

    for path in (OUTPUT_DEM, OUTPUT_WATER):
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

    save_dem(
        dem,
        xmin,
        ymax
    )

    save_water_map(
        water_map,
        xmin,
        ymax
    )


def build_on_disk(manifest, grid):

    # Max Z and water flags are collected in sparse, uncompressed
    # temporary GeoTIFFs (unwritten blocks read as empty, so nothing
    # has to be initialised), then gap-filled / compressed into the
    # final outputs block by block.
    height, width, xmin, xmax, ymin, ymax = grid

    print("  Large DEM: building on disk "
          f"(> MAX_IN_MEMORY_CELLS = {MAX_IN_MEMORY_CELLS:,})")

    scratch = dict(
        compress=None,
        blockxsize=256,
        blockysize=256,
        SPARSE_OK=True,
        BIGTIFF="YES"
    )

    for path in (OUTPUT_DEM, OUTPUT_WATER):
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as tmp_dir:

        raw_dem = Path(tmp_dir) / "raw_dem.tif"
        raw_water = Path(tmp_dir) / "raw_water.tif"

        dem_raw_profile = dem_profile(height, width, xmin, ymax)
        dem_raw_profile.update(scratch, nodata=-np.inf)

        water_raw_profile = water_profile(height, width, xmin, ymax)
        water_raw_profile.update(scratch)

        with rasterio.open(raw_dem, "w+", **dem_raw_profile) as dem_src, \
             rasterio.open(raw_water, "w+", **water_raw_profile) as water_src:

            for tile in manifest["tiles"]:

                filename = tile["tile"] + ".laz"
                laz_path = EXTRACT_DIR / filename

                process_laz_on_disk(
                    laz_path,
                    dem_src,
                    water_src,
                    tile["bounds"],
                    xmin,
                    xmax,
                    ymin,
                    ymax
                )

        print(f"\nSaving DEM: {OUTPUT_DEM}")

        with rasterio.open(raw_dem) as raw_src, \
             rasterio.open(
                 OUTPUT_DEM, "w", **dem_profile(height, width, xmin, ymax)
             ) as dst:

            fill_empty_cells_on_disk(raw_src, dst)

        print("DEM saved successfully.")

        with rasterio.open(raw_water) as src, \
             rasterio.open(
                 OUTPUT_WATER, "w", **water_profile(height, width, xmin, ymax)
             ) as dst:

            for window in _iter_blocks(height, width):
                dst.write(src.read(1, window=window), 1, window=window)

        print("Water map saved successfully.")


# ============================================================
# MAIN
# ============================================================

def extract_tiles():

    # Pipeline step "tiles": make sure every tile of the manifest
    # is extracted
    manifest = load_manifest()

    required = get_required_tiles(
        manifest
    )

    extract_required_tiles(
        required
    )


def make_dem():

    # Pipeline step "dem": DEM + water mask from the extracted tiles
    manifest = load_manifest()

    grid = dem_grid(
        manifest
    )

    height, width = grid[0], grid[1]

    if height * width <= MAX_IN_MEMORY_CELLS:
        build_in_memory(manifest, grid)
    else:
        build_on_disk(manifest, grid)


def main():

    print("=" * 60)
    print("LUXEMBOURG LIDAR -> DEM")
    print("=" * 60)

    extract_tiles()
    make_dem()


if __name__ == "__main__":
    main()
