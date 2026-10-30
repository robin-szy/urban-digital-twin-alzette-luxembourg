from pathlib import Path
import json
import sys
import requests

import geopandas as gpd
from shapely.geometry import box

import PipelineConfig


# ============================================================
# USER SETTINGS (from config/config.toml)
# ============================================================

CFG = PipelineConfig.load()

GPKG_PATH = Path(CFG["paths"]["tile_index_gpkg"])

# Any individual bound can be None (left out of [extent]).
# None means: use Luxembourg's dataset boundary on that side.

X_MIN = CFG["extent"].get("x_min")
X_MAX = CFG["extent"].get("x_max")
Y_MIN = CFG["extent"].get("y_min")
Y_MAX = CFG["extent"].get("y_max")

OUTPUT_MANIFEST = Path(CFG["paths"]["manifest"])

DOWNLOAD = CFG["manifest"]["download"]  # Set to False to skip downloading files
DOWNLOAD_DIR = Path(CFG["paths"]["download_dir"])


# ============================================================
# LOAD TILE INDEX
# ============================================================

def load_tile_index(gpkg_path: Path) -> gpd.GeoDataFrame:

    if not gpkg_path.exists():
        raise FileNotFoundError(
            f"Could not find:\n{gpkg_path.resolve()}"
        )

    print(f"Opening: {gpkg_path}")

    gdf = gpd.read_file(gpkg_path)

    if gdf.empty:
        raise RuntimeError("The GeoPackage contains no features.")

    print(f"Loaded {len(gdf):,} tiles.")
    print(f"CRS: {gdf.crs}")

    return gdf


# ============================================================
# DETECT COLUMNS — CASE INSENSITIVE
# ============================================================

def detect_columns(gdf: gpd.GeoDataFrame):

    candidates = {
        "tile": ["Fichier", "File", "Filename", "Tile"],
        "group": ["Dossier", "Group", "Folder"],
        "download": ["DownloadLink", "Download", "URL", "Link", "Lien"],
    }

    detected = {}

    for field_type, possible_names in candidates.items():
        detected[field_type] = None

        for column in gdf.columns:
            if column.lower() in [name.lower() for name in possible_names]:
                detected[field_type] = column  # original unlowered name
                break

    return detected

# ============================================================
# RESOLVE REQUESTED BOUNDS
# ============================================================

def resolve_bounds(gdf: gpd.GeoDataFrame):

    # Actual outer bounds of all polygons in the dataset
    dataset_xmin, dataset_ymin, dataset_xmax, dataset_ymax = (
        gdf.total_bounds
    )

    xmin = dataset_xmin if X_MIN is None else X_MIN
    xmax = dataset_xmax if X_MAX is None else X_MAX
    ymin = dataset_ymin if Y_MIN is None else Y_MIN
    ymax = dataset_ymax if Y_MAX is None else Y_MAX

    if xmin >= xmax:
        raise ValueError("X_MIN must be smaller than X_MAX.")

    if ymin >= ymax:
        raise ValueError("Y_MIN must be smaller than Y_MAX.")

    return xmin, xmax, ymin, ymax


# ============================================================
# SELECT TILES
# ============================================================

def select_tiles(gdf: gpd.GeoDataFrame):

    xmin, xmax, ymin, ymax = resolve_bounds(gdf)

    requested_area = box(
        xmin,
        ymin,
        xmax,
        ymax
    )

    selected = gdf[
        gdf.geometry.intersects(requested_area)
    ].copy()

    print("\nRequested bounding box:")
    print(f"  X: {xmin:,.2f} -> {xmax:,.2f}")
    print(f"  Y: {ymin:,.2f} -> {ymax:,.2f}")

    width = xmax - xmin
    height = ymax - ymin

    print("\nRequested dimensions:")
    print(f"  Width:  {width:,.0f} m")
    print(f"  Height: {height:,.0f} m")
    print(f"  Area:   {(width * height) / 1_000_000:.2f} km²")

    return selected, (xmin, xmax, ymin, ymax)


# ============================================================
# DISPLAY RESULTS
# ============================================================

def display_results(selected, columns):

    print("\n" + "=" * 60)
    print("LIDAR TILE SELECTION")
    print("=" * 60)

    print(f"\nIntersecting tiles: {len(selected):,}")

    tile_column = columns["tile"]
    group_column = columns["group"]
    download_column = columns["download"]

    if tile_column:

        tiles = sorted(
            selected[tile_column]
            .dropna()
            .astype(str)
            .unique()
        )

        print("\nTiles:")

        for tile in tiles:
            print(f"  {tile}.laz")

    if group_column:

        groups = sorted(
            selected[group_column]
            .dropna()
            .astype(str)
            .unique()
        )

        print(f"\nUnique LiDAR groups: {len(groups):,}")

        for group in groups:
            print(f"  {group}")

    if download_column:

        urls = (
            selected[download_column]
            .dropna()
            .astype(str)
            .unique()
        )

        print(f"\nUnique downloads required: {len(urls):,}")


# ============================================================
# DOWNLOAD
# ============================================================

def download_files(selected, columns):

    download_column = columns["download"]
    group_column = columns["group"]

    if download_column is None:
        raise RuntimeError(
            "Could not detect a download-link column."
        )

    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # One row per unique URL
    unique_downloads = selected.drop_duplicates(
        subset=[download_column]
    )

    print(
        f"\nDownloading {len(unique_downloads):,} "
        f"unique LiDAR archives..."
    )

    for number, (_, row) in enumerate(
        unique_downloads.iterrows(),
        start=1
    ):

        url = str(row[download_column])

        if group_column:
            group = str(row[group_column])
        else:
            group = f"lidar_group_{number}"

        print(
            f"\n[{number}/{len(unique_downloads)}] "
            f"{group}"
        )

        # Request the file
        with requests.get(
            url,
            stream=True,
            timeout=120,
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            # Try to determine extension from final URL
            suffix = Path(response.url).suffix

            if not suffix:
                suffix = ".zip"

            output_path = DOWNLOAD_DIR / (
                group + suffix
            )

            # Don't redownload existing files
            if output_path.exists():
                print(f"Already exists: {output_path}")
                continue

            total_bytes = int(
                response.headers.get(
                    "content-length",
                    1
                )
            )

            downloaded = 0

            with open(output_path, "wb") as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if not chunk:
                        continue

                    file.write(chunk)
                    downloaded += len(chunk)

                    if total_bytes:
                        percent = (
                            downloaded
                            / total_bytes
                            * 100
                        )

                        print(
                            f"\r  {percent:5.1f}%",
                            end="",
                            flush=True
                        )

            print(f"\nSaved: {output_path}")


# ============================================================
# CREATE MANIFEST
# ============================================================

def create_manifest(
    selected,
    columns,
    resolved_bounds
):

    xmin, xmax, ymin, ymax = resolved_bounds

    tiles = []

    for _, row in selected.iterrows():

        record = {}

        if columns["tile"]:
            record["tile"] = str(
                row[columns["tile"]]
            )

        if columns["group"]:
            record["group"] = str(
                row[columns["group"]]
            )

        if columns["download"]:
            record["download"] = str(
                row[columns["download"]]
            )

        gxmin, gymin, gxmax, gymax = (
            row.geometry.bounds
        )

        record["bounds"] = {
            "xmin": gxmin,
            "ymin": gymin,
            "xmax": gxmax,
            "ymax": gymax,
        }

        tiles.append(record)

    manifest = {
        "coordinate_system": "EPSG:2169",

        "requested_bbox": {
            "xmin": xmin,
            "xmax": xmax,
            "ymin": ymin,
            "ymax": ymax,
        },

        "number_of_tiles": len(tiles),

        "tiles": tiles,
    }

    OUTPUT_MANIFEST.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT_MANIFEST,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            manifest,
            file,
            indent=4,
            ensure_ascii=False
        )

    print(
        f"\nManifest saved to: "
        f"{OUTPUT_MANIFEST.resolve()}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("LUXEMBOURG LIDAR 2024 TILE SELECTOR")
    print("=" * 60)

    gdf = load_tile_index(GPKG_PATH)

    columns = detect_columns(gdf)

    print("\nDetected columns:")
    print(f"  Tile:     {columns['tile']}")
    print(f"  Group:    {columns['group']}")
    print(f"  Download: {columns['download']}")

    selected, bounds = select_tiles(gdf)

    if selected.empty:
        print("\nNo LiDAR tiles intersect this area.")
        sys.exit(0)

    display_results(
        selected,
        columns
    )

    create_manifest(
        selected,
        columns,
        bounds
    )

    if DOWNLOAD:
        download_files(
            selected,
            columns
        )


if __name__ == "__main__":
    main()