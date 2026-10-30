import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio


def _to_native(value):
    # numpy scalars (int64, float64, ...) aren't JSON serializable; convert
    # to plain Python types so the key round-trips identically through JSON.
    if isinstance(value, np.generic):
        return value.item()
    return value


def _cache_key(
    path: str | Path,
    layer: str | None,
    aoi: gpd.GeoDataFrame,
    crs: str,
) -> dict:
    # Everything that determines the clip result: which raw dataset/layer,
    # which CRS, and the AOI itself (geometry, not just its attributes - a
    # future aoi_method could change the shape without changing km_start/end).
    aoi_row = aoi.iloc[0]
    return {
        "source_path": str(Path(path).resolve()),
        "source_layer": layer,
        "crs": crs,
        "aoi_geometry_wkb": aoi_row.geometry.wkb_hex,
        "aoi_attrs": {col: _to_native(aoi_row[col]) for col in aoi.columns if col != "geometry"},
    }


def _cache_file(out_path: Path) -> Path:
    return out_path.parent / f"{out_path.name}.cache.json"


def _read_cache(out_path: Path) -> dict:
    cache_file = _cache_file(out_path)
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    return {}


def _write_cache(out_path: Path, cache: dict) -> None:
    with open(_cache_file(out_path), "w") as f:
        json.dump(cache, f, indent=2)


def load_clipped(
    path: str | Path,
    aoi: gpd.GeoDataFrame,
    layer: str | None = None,
    crs: str = "EPSG:2169",
    out_path: str | Path | None = None,
    out_layer: str | None = None,
    overwrite: bool = False,
) -> gpd.GeoDataFrame:
    """Read a vector dataset, reproject/fix/clip it to the AOI.

    Uses the AOI's bbox as a pre-filter so large countrywide files (e.g. the
    land cover GDB) are not fully loaded into memory. Reusable across A1/A2/A3.

    If out_path/out_layer are given, the clipped result is cached there: a
    sidecar `<out_path>.cache.json` records which source/layer/CRS/AOI
    produced it, and the next call with identical inputs reads the saved
    layer back instead of re-clipping. Set overwrite=True to force a redo
    (e.g. after the AOI method changes in a way not reflected in its geometry,
    or the raw source file itself was replaced).
    """
    if out_path is not None:
        if out_layer is None:
            raise ValueError("out_layer is required when out_path is given")
        out_path = Path(out_path)
        key: dict = _cache_key(path, layer, aoi, crs)
        cache: dict = _read_cache(out_path)
        if not overwrite and out_path.exists() and cache.get(out_layer) == key:
            print(f"[cache] {out_layer}: source/AOI/CRS unchanged, reusing cached layer")
            return pyogrio.read_dataframe(out_path, layer=out_layer)

    # Pre-filter by bounding box before doing anything else; this is what
    # keeps country-wide datasets like the land cover GDB fast to read. The
    # bbox must be in the *source's* CRS, not the AOI's - e.g. OSM data is
    # EPSG:4326 (degrees), so an EPSG:2169 (meters) bbox would silently match
    # nothing instead of erroring.
    source_crs = pyogrio.read_info(path, layer=layer)["crs"]
    aoi_in_source_crs = aoi.to_crs(source_crs) if source_crs else aoi
    bbox: tuple[float, float, float, float] = tuple(aoi_in_source_crs.total_bounds)
    gdf: gpd.GeoDataFrame = pyogrio.read_dataframe(path, layer=layer, bbox=bbox)

    if gdf.crs is None or gdf.crs.to_string() != crs:
        gdf = gdf.to_crs(crs)

    # Bbox pre-filtering only matches on feature envelopes: a single
    # countrywide row (like HQ100) still spans the whole country after this.
    # Clip to the AOI polygon *before* make_valid, so make_valid only has to
    # fix the small clipped-down geometry, not the full countrywide one.
    clipped: gpd.GeoDataFrame = gpd.clip(gdf, aoi).reset_index(drop=True)
    clipped = clipped.set_geometry(clipped.geometry.make_valid())  # GEOS's repair algorithm

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        clipped.to_file(out_path, layer=out_layer, driver="GPKG")
        cache[out_layer] = key
        _write_cache(out_path, cache)
        print(f"[cache] {out_layer}: (re)computed and cached to {out_path}")

    return clipped


def dissolve_footprint(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Merge all rows of gdf into a single-row GeoDataFrame (one footprint polygon)."""
    footprint = gdf.geometry.union_all()
    return gpd.GeoDataFrame(geometry=[footprint], crs=gdf.crs)
