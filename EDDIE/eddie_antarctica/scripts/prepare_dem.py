"""
Merge DEM GeoTIFFs into the terrain's master DEM, then rebuild the Cesium terrain from it.

Usage: python scripts/prepare_dem.py <dem.tif> [<dem.tif> ...] [--new] [--height-offset METRES]
                                     [--resolution METRES] [--volume NAME]

  dem.tif          One or more DEMs. Where they overlap, later files win over earlier ones,
                   and all of them win over what is already in the master DEM.
  --new            Discard the master DEM and build the terrain from only these files.
  --height-offset  Added to every height, e.g. ~47.5 to turn Luxembourg NG-L heights into
                   ellipsoid heights so terrain lines up with other 3D data. Default 0.
  --resolution     Pixel size of the merged DEM, in the master's CRS units (metres for LUREF).
                   Defaults to the finest input. The whole mosaic is held in memory, so use e.g. 5
                   for a country-wide lidar DEM (0.5 m pixels would need ~140 GB of RAM).
  --volume         Docker volume mounted at /stored_data (passed to build_terrain.sh).

Steps:
  1. Merge the inputs (and the existing master, unless --new) into one raster in the master's CRS,
     covering the union of all extents. Pixels no input covers are NaN.
  2. Reproject the merged DEM to EPSG:4326, apply the height offset and fill the NaN gaps with
     the lowest real height: ctb-tile reads no-data as real heights, so gaps would become pits.
  3. Tile it with scripts/build_terrain.sh (Docker).
  4. Only if that succeeded: save the merged DEM as the new master and point the TerriaJS home
     camera (terriajs/catalog.json) at the terrain's extent.

The master DEM (data/terrain/dem_merged.tif) keeps the original heights, NaN gaps included, so the
offset and gap fill are re-applied from clean data on every run.
"""
import argparse
from contextlib import ExitStack
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import List, Sequence

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent
MASTER_DEM = PROJECT_DIR / "data" / "terrain" / "dem_merged.tif"
CATALOG = PROJECT_DIR / "terriajs" / "catalog.json"
BUILD_TERRAIN = PROJECT_DIR / "scripts" / "build_terrain.sh"
DEFAULT_VOLUME = "eddie_antarctica_antarctic_stored_data"

GEOTIFF_PROFILE = {"driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
                   "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512}


def sources_file(master: pathlib.Path) -> pathlib.Path:
    """Return the JSON sidecar that records which files the master DEM was merged from."""
    return master.with_suffix(".sources.json")


def merge_dems(new_files: Sequence[pathlib.Path], output: pathlib.Path,
               existing: pathlib.Path | None = None, resolution: float | None = None) -> None:
    """
    Merge DEMs into one float32 GeoTIFF covering the union of their extents.

    Parameters
    ----------
    new_files : Sequence[pathlib.Path]
        DEMs to merge. Where they overlap, later files take precedence over earlier ones.
    output : pathlib.Path
        Where to write the merged DEM. Pixels no input covers are NaN.
    existing : pathlib.Path | None
        A previously merged DEM. It has the lowest precedence, and its CRS is kept.
    resolution : float | None
        Pixel size of the output, in the units of its CRS. Defaults to the finest input resolution.
        The mosaic is held in memory at this size, so coarsen it for large inputs.
    """
    # rasterio's "first" method keeps the first valid value per pixel, so list highest precedence first.
    paths = list(reversed(new_files)) + ([existing] if existing else [])
    with rasterio.open(existing or new_files[0]) as reference:
        target_crs = reference.crs
    with ExitStack() as stack:
        aligned = []
        for path in paths:
            ds = stack.enter_context(rasterio.open(path))
            if ds.crs != target_crs:
                # merge() needs a single CRS, so inputs in another CRS are reprojected on the fly.
                ds = stack.enter_context(WarpedVRT(ds, crs=target_crs, resampling=Resampling.bilinear,
                                                   dtype="float32", nodata=np.nan))
            aligned.append(ds)
        res = resolution or min(min(ds.res) for ds in aligned)
        mosaic, transform = merge(aligned, res=res, nodata=np.nan, dtype="float32", indexes=[1],
                                  method="first", resampling=Resampling.bilinear, target_aligned_pixels=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output, "w", height=mosaic.shape[1], width=mosaic.shape[2], crs=target_crs,
                       transform=transform, **GEOTIFF_PROFILE) as dst:
        dst.write(mosaic)


def to_terrain_input(merged: pathlib.Path, output: pathlib.Path, height_offset: float) -> None:
    """
    Turn the merged DEM into what ctb-tile needs: EPSG:4326, heights offset, and no no-data pixels.

    Parameters
    ----------
    merged : pathlib.Path
        The merged DEM, in any CRS, with NaN where there is no data.
    output : pathlib.Path
        Where to write the EPSG:4326 GeoTIFF.
    height_offset : float
        Metres added to every height.
    """
    dst_crs = CRS.from_epsg(4326)
    with rasterio.open(merged) as src:
        transform, width, height = calculate_default_transform(src.crs, dst_crs, src.width, src.height,
                                                               *src.bounds)
        heights = np.full((height, width), np.nan, dtype="float32")
        reproject(rasterio.band(src, 1), heights, src_nodata=np.nan, dst_transform=transform,
                  dst_crs=dst_crs, dst_nodata=np.nan, resampling=Resampling.bilinear)
    gaps = np.isnan(heights)
    if gaps.all():
        raise ValueError(f"{merged} holds no valid heights")
    heights[gaps] = np.nanmin(heights)
    heights += height_offset
    profile = {**GEOTIFF_PROFILE, "nodata": None}
    with rasterio.open(output, "w", height=height, width=width, crs=dst_crs, transform=transform,
                       **profile) as dst:
        dst.write(heights, 1)
    print(f"Terrain input: {width} x {height} px, heights {heights.min():.1f} to {heights.max():.1f} m, "
          f"{gaps.mean():.1%} gap pixels filled with the lowest height")


def set_home_camera(dem: pathlib.Path, catalog: pathlib.Path) -> dict:
    """
    Point the TerriaJS home camera at the DEM's extent, so the map opens centred on the terrain.

    Parameters
    ----------
    dem : pathlib.Path
        The DEM whose bounds become the home camera rectangle.
    catalog : pathlib.Path
        The TerriaJS catalog.json to update in place.

    Returns
    -------
    dict
        The new homeCamera rectangle, in degrees.
    """
    with rasterio.open(dem) as src:
        west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    camera = {"west": round(west, 6), "south": round(south, 6), "east": round(east, 6), "north": round(north, 6)}
    content = json.loads(catalog.read_text(encoding="utf-8"))
    content["homeCamera"] = camera
    catalog.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return camera


def find_bash() -> str:
    """Return a bash executable, preferring Git Bash on Windows (WSL's bash cannot reach Docker Desktop paths)."""
    if os.name == "nt" and (git := shutil.which("git")):
        for parent in pathlib.Path(git).resolve().parents:
            if (candidate := parent / "bin" / "bash.exe").is_file():
                return str(candidate)
    if bash := shutil.which("bash"):
        return bash
    raise FileNotFoundError("bash not found; install Git for Windows or run scripts/build_terrain.sh yourself")


def main(argv: List[str] | None = None) -> None:
    """Parse the command line, merge, tile, then save the master DEM and home camera."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dems", nargs="+", type=pathlib.Path)
    parser.add_argument("--new", action="store_true", help="ignore the current master DEM")
    parser.add_argument("--height-offset", type=float, default=0.0)
    parser.add_argument("--resolution", type=float)
    parser.add_argument("--volume", default=DEFAULT_VOLUME)
    args = parser.parse_args(argv)

    for dem in args.dems:
        if not dem.is_file():
            parser.error(f"DEM file not found: {dem}")
    new_files = [dem.resolve() for dem in args.dems]
    existing = None if args.new or not MASTER_DEM.is_file() else MASTER_DEM
    previous_sources = []
    if existing and sources_file(MASTER_DEM).is_file():
        previous_sources = json.loads(sources_file(MASTER_DEM).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as work_dir:
        merged = pathlib.Path(work_dir) / "dem_merged.tif"
        terrain_input = pathlib.Path(work_dir) / "dem_4326.tif"
        print(f"==> Merging {len(new_files)} file(s)" + (f" into {existing}" if existing else " (new terrain)"))
        merge_dems(new_files, merged, existing, args.resolution)
        print("==> Reprojecting to EPSG:4326 and filling gaps")
        to_terrain_input(merged, terrain_input, args.height_offset)
        print("==> Tiling")
        subprocess.run([find_bash(), BUILD_TERRAIN.as_posix(), terrain_input.as_posix(), "--volume", args.volume],
                       check=True)
        MASTER_DEM.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(merged, MASTER_DEM)

    # Newest first, matching precedence.
    sources = [str(path) for path in reversed(new_files)] + previous_sources
    sources_file(MASTER_DEM).write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    camera = set_home_camera(MASTER_DEM, CATALOG)
    print(f"==> Master DEM saved to {MASTER_DEM}")
    print(f"==> Home camera set to {camera} in {CATALOG}")
    print("    Rebuild the map to pick it up: docker compose up -d --build terria_map "
          "(with docker-compose-dev.yml a hard refresh is enough)")


if __name__ == "__main__":
    main()
