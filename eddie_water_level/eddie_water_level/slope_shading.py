# -*- coding: utf-8 -*-
"""
Shading of steep terrain, drawn over the orthophoto in Terria.

The slope of the LiDAR DEM (on the flood_grid computation grid) is computed once. Each web map
tile (/slope-shading/<style>/{z}/{x}/{y}.png, loaded by a Terria layer) colours it: transparent
below the start angle, then the shading colour getting more opaque (darker over the image) as the
slope increases, full colour from the full-colour angle. The style in the url (start-full-colour,
e.g. 35-60-5a3a1e) is set by the Terrain panel of the map; the layer's opacity scales it all.
"""

import functools
import io
import math
import os
import re

import numpy as np
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

from eddie_water_level.flood_grid import dem_grid


# Slope (degrees) where the shading starts, and where it reaches full colour. Ordinary streets
# and gentle slopes stay below the start.
SLOPE_START_DEG = float(os.environ.get("SLOPE_SHADING_START_DEG", "35"))
SLOPE_FULL_DEG = float(os.environ.get("SLOPE_SHADING_FULL_DEG", "60"))

# Shading colour (dark brown), as hex RGB.
SLOPE_COLOUR = os.environ.get("SLOPE_SHADING_COLOUR", "5a3a1e")

TILE_PX = 256
WEB_MERCATOR = CRS.from_epsg(3857)
_HALF_WORLD_M = math.pi * 6378137.0
_STYLE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)-([0-9a-fA-F]{6})$")


def default_style() -> str:
    """The style of the defaults: start-full-colour."""
    return f"{SLOPE_START_DEG:g}-{SLOPE_FULL_DEG:g}-{SLOPE_COLOUR.lower()}"


def parse_style(style: str) -> tuple:
    """(start, full, (r, g, b)) of a style 'start-full-rrggbb'. Raises ValueError for an invalid one."""
    match = _STYLE.match(style)
    if not match:
        raise ValueError(f"Invalid slope shading style {style!r}; use start-full-rrggbb, e.g. {default_style()}.")
    start, full = float(match.group(1)), float(match.group(2))
    if not 0 <= start < full <= 90:
        raise ValueError("The start angle must be below the full-colour angle, both between 0 and 90 degrees.")
    colour = match.group(3)
    return start, full, tuple(int(colour[i: i + 2], 16) for i in (0, 2, 4))


@functools.lru_cache(maxsize=1)
def slope_degrees() -> np.ndarray:
    """Slope of the DEM in degrees on the flood_grid grid (float32, NaN outside the DEM)."""
    grid = dem_grid()
    ground = grid["mean"].astype("float64")
    step_y, step_x = abs(grid["transform"].e), abs(grid["transform"].a)
    rise_y, rise_x = np.gradient(ground, step_y, step_x)
    return np.degrees(np.arctan(np.hypot(rise_x, rise_y))).astype("float32")


@functools.lru_cache(maxsize=1)
def bounds_wgs84() -> list:
    """[west, south, east, north] of the shading in degrees."""
    grid = dem_grid()
    rows, cols = grid["shape"]
    transform = grid["transform"]
    left, top = transform.c, transform.f
    right, bottom = left + cols * transform.a, top + rows * transform.e
    return list(transform_bounds(grid["crs"], "EPSG:4326", left, bottom, right, top))


@functools.lru_cache(maxsize=1)
def _bounds_mercator() -> tuple:
    west, south, east, north = bounds_wgs84()
    return transform_bounds("EPSG:4326", WEB_MERCATOR, west, south, east, north)


@functools.lru_cache(maxsize=1)
def _empty_tile() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (TILE_PX, TILE_PX), (0, 0, 0, 0)).save(output, format="PNG")
    return output.getvalue()


@functools.lru_cache(maxsize=4096)
def tile_png(z: int, x: int, y: int, style: str | None = None) -> bytes:
    """One web map tile (XYZ, EPSG:3857) of the shading in a style (default_style()), as PNG."""
    start, full, colour = parse_style(style or default_style())
    size = 2.0 * _HALF_WORLD_M / (2 ** z)
    left, top = -_HALF_WORLD_M + x * size, _HALF_WORLD_M - y * size
    min_x, min_y, max_x, max_y = _bounds_mercator()
    if left > max_x or left + size < min_x or top < min_y or top - size > max_y:
        return _empty_tile()

    grid = dem_grid()
    slope = np.full((TILE_PX, TILE_PX), np.nan, dtype="float32")
    reproject(
        source=slope_degrees(), destination=slope,
        src_transform=grid["transform"], src_crs=grid["crs"], src_nodata=np.nan,
        dst_transform=from_origin(left, top, size / TILE_PX, size / TILE_PX), dst_crs=WEB_MERCATOR,
        dst_nodata=np.nan, resampling=Resampling.bilinear,
    )
    strength = np.clip((slope - start) / (full - start), 0.0, 1.0)
    strength[~np.isfinite(strength)] = 0.0
    if not strength.any():
        return _empty_tile()
    tile = np.zeros((TILE_PX, TILE_PX, 4), dtype="uint8")
    tile[..., :3] = colour
    tile[..., 3] = np.round(255 * strength).astype("uint8")
    output = io.BytesIO()
    Image.fromarray(tile, mode="RGBA").save(output, format="PNG", optimize=False)
    return output.getvalue()
