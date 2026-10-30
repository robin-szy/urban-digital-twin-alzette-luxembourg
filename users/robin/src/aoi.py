from pathlib import Path

import geopandas as gpd
import pyogrio
from shapely.geometry import box, Polygon

from src.config import REPO_ROOT

# Alzette is a primary watercourse; the kilometrage GPKG also has a
# secondary-watercourse layer that we don't need here.
KILOMETRAGE_LAYER: str = "Kilometrierung primäre Gewässer"
DEFAULT_KILOMETRAGE_PATH: Path = REPO_ROOT / "data/raw/River_kilometers/kilometrage-des-cours-deau.gpkg"

# KILO_KM values carry float rounding noise (e.g. 46.299999 instead of 46.3)
# from the source data; tolerance is far smaller than the 100 m point spacing.
KM_TOLERANCE: float = 1e-3


def build_aoi(
    river: str,
    km_start: float,
    km_end: float,
    margin_m: float,
    crs: str = "EPSG:2169",
    method: str = "bbox",
    kilometrage_path: str | Path = DEFAULT_KILOMETRAGE_PATH,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Build a provisional AOI from river-kilometrage points.

    Returns (aoi, points): a one-row GeoDataFrame with the AOI polygon and
    its build parameters as attributes, and the river-kilometrage points
    used to build it.
    """
    if method != "bbox":
        # Only the rectangle method is implemented; other methods are a team decision for later.
        raise NotImplementedError(f"aoi_method={method!r} is not implemented yet; only 'bbox' is supported")

    points: gpd.GeoDataFrame = pyogrio.read_dataframe(kilometrage_path, layer=KILOMETRAGE_LAYER)
    if points.crs is None or points.crs.to_string() != crs:
        # Source is already EPSG:2169, but reproject defensively in case that changes upstream.
        points = points.to_crs(crs)

    available_rivers: list[str] = sorted(points["TOPONYME"].unique())
    if river not in available_rivers:
        raise ValueError(f"River {river!r} not found in {kilometrage_path}. Available: {available_rivers}")

    selected: gpd.GeoDataFrame = (
        points[
            (points["TOPONYME"] == river)
            & (points["KILO_KM"] >= km_start - KM_TOLERANCE)
            & (points["KILO_KM"] <= km_end + KM_TOLERANCE)
        ]
        .sort_values("KILO_KM")
        .reset_index(drop=True)
    )

    if selected.empty:
        raise ValueError(f"No {river!r} points found between km {km_start} and {km_end}")

    # Bounding box of the selected points, expanded by the configurable margin.
    minx, miny, maxx, maxy = selected.total_bounds
    aoi_polygon: Polygon = box(minx - margin_m, miny - margin_m, maxx + margin_m, maxy + margin_m)

    # Store the build parameters alongside the geometry so aoi.gpkg is self-describing.
    aoi: gpd.GeoDataFrame = gpd.GeoDataFrame(
        {
            "river": [river],
            "km_start": [km_start],
            "km_end": [km_end],
            "margin_m": [margin_m],
            "method": [method],
            "n_points": [len(selected)],
        },
        geometry=[aoi_polygon],
        crs=crs,
    )

    return aoi, selected


DEFAULT_AOI_PATH: Path = REPO_ROOT / "data/processed/aoi.gpkg"


def load_aoi(path: str | Path = DEFAULT_AOI_PATH) -> gpd.GeoDataFrame:
    # All downstream steps must read the AOI from disk, not rebuild it, so a
    # better AOI (e.g. a river buffer) can later replace the rectangle here
    # without touching clip/intersect code.
    return pyogrio.read_dataframe(path, layer="aoi")


def load_river_points(path: str | Path = DEFAULT_AOI_PATH) -> gpd.GeoDataFrame:
    # The river-kilometrage points used to build the AOI, saved alongside it
    # by scripts/build_aoi.py - reused for the Step 4 map (§5).
    return pyogrio.read_dataframe(path, layer="river_points")
