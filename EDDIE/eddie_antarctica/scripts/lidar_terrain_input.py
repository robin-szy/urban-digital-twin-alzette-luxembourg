"""
Turn the master LiDAR DEM into the input of the Cesium terrain: EPSG:4326 with ellipsoid heights.

Usage (inside the backend image, which has rasterio and pyproj; scripts/build_lidar_terrain.sh runs it):
    python lidar_terrain_input.py <dem.tif> <output_dir>

The DEM heights are NG95 (Luxembourg levelling, above sea level); Cesium expects heights above the
WGS84 ellipsoid. Every pixel gets the EGM2008 geoid undulation N at its position added
(h = H + N), computed with pyproj from the EGM2008 grid (us_nga_egm08_25, downloaded by PROJ from
cdn.proj.org; PROJ_NETWORK=ON). N is computed on a coarse grid and interpolated: it varies by a few
centimetres over the DEM. Gaps are filled with the lowest height, as scripts/prepare_dem.py does
(ctb-tile reads no-data as real heights).

Writes to <output_dir>:
    dem_4326_ellipsoid.tif   the tiler input
    geoid_egm2008.tif        N (metres) over the DEM, in the DEM's CRS, for the backend to place
                             water and buildings on the same surface (TERRAIN_GEOID_GRID)
    terrain_input.json       what was done (source, geoid model, N range)

The DEM itself is only read.
"""
import json
import pathlib
import sys

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject
from scipy.interpolate import RegularGridInterpolator

GEOID_MODEL = "EGM2008 (EPSG:3855, PROJ grid us_nga_egm08_25)"
# Spacing of the points where N is computed, in pixels of the reprojected DEM.
GEOID_STEP_PX = 50
# Spacing of the geoid grid written for the backend, in metres.
GEOID_GRID_M = 50.0


def undulation(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """EGM2008 geoid undulation N (metres) at WGS84 longitudes / latitudes."""
    to_geoid = Transformer.from_crs("EPSG:4979", "EPSG:4326+3855", always_xy=True)
    _, _, geoid_height = to_geoid.transform(lon, lat, np.zeros_like(lon))
    # An ellipsoid height of 0 is -N above the geoid.
    result = -np.asarray(geoid_height, dtype="float64")
    if not np.isfinite(result).all() or np.abs(result).max() < 1.0:
        raise RuntimeError("EGM2008 grid not available to PROJ (set PROJ_NETWORK=ON or install us_nga_egm08_25.tif)")
    return result


def main(dem_path: str, output_dir: str) -> None:
    dem_path, out = pathlib.Path(dem_path), pathlib.Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    dst_crs = CRS.from_epsg(4326)
    with rasterio.open(dem_path) as src:
        transform, width, height = calculate_default_transform(src.crs, dst_crs, src.width, src.height, *src.bounds)
        heights = np.full((height, width), np.nan, dtype="float32")
        reproject(rasterio.band(src, 1), heights, src_nodata=np.nan, dst_transform=transform,
                  dst_crs=dst_crs, dst_nodata=np.nan, resampling=Resampling.bilinear)
        src_crs, src_bounds = src.crs, src.bounds

    # N on a coarse grid of pixel centres, interpolated to every pixel.
    rows = np.unique(np.append(np.arange(0, height, GEOID_STEP_PX), height - 1))
    cols = np.unique(np.append(np.arange(0, width, GEOID_STEP_PX), width - 1))
    lon = transform.c + (cols + 0.5) * transform.a
    lat = transform.f + (rows + 0.5) * transform.e
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    coarse = undulation(lon_grid.ravel(), lat_grid.ravel()).reshape(lat_grid.shape)
    interpolate = RegularGridInterpolator((rows, cols), coarse)
    geoid = np.empty((height, width), dtype="float32")
    all_cols = np.arange(width)
    for row in range(height):
        geoid[row] = interpolate(np.column_stack([np.full(width, row), all_cols]))

    gaps = np.isnan(heights)
    if gaps.all():
        raise ValueError(f"{dem_path} holds no valid heights")
    heights[gaps] = np.nanmin(heights)
    heights += geoid
    with rasterio.open(out / "dem_4326_ellipsoid.tif", "w", driver="GTiff", dtype="float32", count=1,
                       height=height, width=width, crs=dst_crs, transform=transform,
                       compress="deflate", tiled=True, blockxsize=512, blockysize=512) as dst:
        dst.write(heights, 1)

    # N over the DEM in its own CRS, for the backend.
    left, bottom, right, top = src_bounds
    grid_cols = int(np.ceil((right - left) / GEOID_GRID_M)) + 1
    grid_rows = int(np.ceil((top - bottom) / GEOID_GRID_M)) + 1
    grid_transform = from_origin(left - GEOID_GRID_M / 2, top + GEOID_GRID_M / 2, GEOID_GRID_M, GEOID_GRID_M)
    xs = left + np.arange(grid_cols) * GEOID_GRID_M
    ys = top - np.arange(grid_rows) * GEOID_GRID_M
    x_grid, y_grid = np.meshgrid(xs, ys)
    to_wgs84 = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
    grid_lon, grid_lat = to_wgs84.transform(x_grid.ravel(), y_grid.ravel())
    grid_n = undulation(np.asarray(grid_lon), np.asarray(grid_lat)).reshape(x_grid.shape).astype("float32")
    with rasterio.open(out / "geoid_egm2008.tif", "w", driver="GTiff", dtype="float32", count=1,
                       height=grid_rows, width=grid_cols, crs=src_crs, transform=grid_transform) as dst:
        dst.write(grid_n, 1)

    report = {
        "source_dem": str(dem_path.name),
        "geoid_model": GEOID_MODEL,
        "geoid_undulation_m": {"min": round(float(geoid.min()), 3), "mean": round(float(geoid.mean()), 3),
                               "max": round(float(geoid.max()), 3)},
        "size_px": [width, height],
        "ellipsoid_heights_m": [round(float(heights.min()), 2), round(float(heights.max()), 2)],
        "gap_pixels_filled": round(float(gaps.mean()), 4),
    }
    (out / "terrain_input.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
