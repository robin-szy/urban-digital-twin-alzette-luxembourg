"""
Run the full terrain analysis on another DEM and compare it with
the main DEM of the config.

    python src/TestFullDem.py [dem.tif] [--water mask.tif]
                              [--bbox XMIN YMIN XMAX YMAX] [--force]
                              [--config config/other.toml]

Without dem.tif, paths.comparison_dem of the config is used. Also run
by RunPipeline.py (step "comparison"). Run from the project root. The
DEM must be in EPSG:2169. Both runs (main DEM -> baseline/, input DEM
-> test/) write only into paths.comparison_output_dir, never into
paths.output_dir.
"""

import argparse
import shutil
import sys
import time
import warnings
from pathlib import Path

import PipelineConfig

if __name__ == "__main__":
    # --config has to be applied before the pipeline scripts are
    # imported: their constants blocks are filled from it at import
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--config", type=Path)
    _config = _pre.parse_known_args()[0].config
    if _config is not None:
        PipelineConfig.use(_config)

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from pyproj import CRS
from rasterio.windows import Window, from_bounds
from shapely.geometry import box

import RunPipeline
import TerrainAnalysis as TA
from RunPipeline import ANALYSIS_STEPS, available_ram


# ============================================================
# SETTINGS (from config/config.toml)
# ============================================================

CFG = PipelineConfig.load()
_CMP = CFG["comparison"]

# Baseline = the main DEM of the config
BASELINE_DEM = Path(CFG["paths"]["dem"])
BASELINE_WATER = Path(CFG["paths"]["water"])

# Never written to
PROTECTED_DIR = Path(CFG["paths"]["output_dir"])

TEST_ROOT = Path(CFG["paths"]["comparison_output_dir"])
BASELINE_DIR = TEST_ROOT / "baseline"
TEST_DIR = TEST_ROOT / "test"

# Cropped / generated test inputs go with the other GeoTIFFs
TEST_INPUT_DIR = TEST_DIR / CFG["outputs"]["tif_subdir"]

REPORT_PATH = TEST_ROOT / "comparison_report.md"
METRICS_CSV = TEST_ROOT / "comparison_metrics.csv"
CONFIG_COPY = TEST_ROOT / CFG["outputs"]["config_copy_name"]

# Km markers used by both runs ("" = the main river_km_gpkg)
KM_GPKG = Path(CFG["paths"]["comparison_km_gpkg"] or CFG["paths"]["river_km_gpkg"])

REQUIRED_EPSG = 2169

# Memory / disk estimate per DEM cell for the whole pipeline
# (Whitebox loads full rasters). Measured 2026-10-11 on a 64M-cell
# float64 DEM: peak 70 B/cell (river_bed step), outputs ~35 B/cell.
# Recalibrate from "peak memory per cell" in comparison_report.md.
BYTES_PER_CELL_RAM = _CMP["bytes_per_cell_ram"]
BYTES_PER_CELL_DISK = _CMP["bytes_per_cell_disk"]

# Share of free RAM / disk the estimate may use before the run is
# refused (override with --force / comparison.force)
RESOURCE_MARGIN = _CMP["resource_margin"]

# Overlap comparison
CENTERLINE_SAMPLE_M = _CMP["centerline_sample_m"]            # spacing of points compared between centerlines
IOU_GOOD = _CMP["iou_good"]                                  # watershed / bed IoU considered a match
CENTERLINE_MAX_MEAN_M = _CMP["centerline_max_mean_m"]        # mean centerline offset considered suspicious
ELEVATION_MAX_MEAN_ABS_M = _CMP["elevation_max_mean_abs_m"]  # mean |dz| in the overlap considered suspicious
SCALING_MAX_RATIO = _CMP["scaling_max_ratio"]                # time per cell test / baseline considered superlinear


# ============================================================
# INPUT CHECKS
# ============================================================

def gib(n):
    return n / 2**30


def is_epsg_2169(crs):

    # Compare the CRS definition, not only its EPSG code: the official
    # Luxembourg DEM stores EPSG:2169 as WKT with other object names
    # ("International 1924" ellipsoid), so to_epsg() and equals() both
    # fail on it although projection and parameters are identical
    if crs is None:
        return False

    crs = CRS.from_wkt(crs.to_wkt())
    target = CRS.from_epsg(REQUIRED_EPSG)

    if crs.to_epsg(min_confidence=70) == REQUIRED_EPSG:
        return True

    if crs.equals(target, ignore_axis_order=True):
        return True

    with warnings.catch_warnings():
        # to_proj4 warns that PROJ strings drop metadata; only the
        # projection, its parameters, ellipsoid and units are compared
        warnings.simplefilter("ignore", UserWarning)
        return crs.to_proj4() == target.to_proj4()


def check_output_dir(path):

    resolved = Path(path).resolve()
    protected = PROTECTED_DIR.resolve()

    if resolved == protected or protected in resolved.parents:
        raise RuntimeError(f"Refusing to write into {PROTECTED_DIR}: {path}")


def describe(path):

    with rasterio.open(path) as src:
        return {
            "path": str(path),
            "width": src.width,
            "height": src.height,
            "cells": src.width * src.height,
            "res_x": abs(src.res[0]),
            "res_y": abs(src.res[1]),
            "bounds": tuple(src.bounds),
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
            "crs_ok": is_epsg_2169(src.crs),
        }


def estimate(info):

    return (
        info["cells"] * BYTES_PER_CELL_RAM,
        info["cells"] * BYTES_PER_CELL_DISK
    )


def dem_statistics(path):

    # Nodata share and elevation min / max / mean, block by block
    with rasterio.open(path) as src:

        n_valid = 0
        total = 0.0
        zmin = np.inf
        zmax = -np.inf

        for window in TA._iter_windows(src):

            arr = src.read(1, window=window)
            valid = TA._valid(arr, src.nodata)

            if not valid.any():
                continue

            values = arr[valid].astype(np.float64)
            n_valid += values.size
            total += values.sum()
            zmin = min(zmin, values.min())
            zmax = max(zmax, values.max())

        cells = src.width * src.height
        cell_area = abs(src.res[0] * src.res[1])

    return {
        "nodata_pct": 100.0 * (cells - n_valid) / cells,
        "area_km2": cells * cell_area / 1e6,
        "valid_area_km2": n_valid * cell_area / 1e6,
        "z_min": float(zmin) if n_valid else np.nan,
        "z_max": float(zmax) if n_valid else np.nan,
        "z_mean": total / n_valid if n_valid else np.nan,
    }


# ============================================================
# TEST INPUTS
# ============================================================

def _copy_window(src_path, dst_path, window, dtype=None, nodata="keep", zeros=False):

    # Copy a window of a raster (or write zeros on its grid) block
    # by block into a compressed, tiled GeoTIFF
    with rasterio.open(src_path) as src:

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            width=int(window.width),
            height=int(window.height),
            transform=src.window_transform(window),
            count=1,
            compress="deflate",
            tiled=True,
            blockxsize=256,
            blockysize=256,
            BIGTIFF="IF_SAFER"
        )

        if dtype is not None:
            profile["dtype"] = dtype

        if nodata != "keep":
            profile["nodata"] = nodata

        with rasterio.open(dst_path, "w", **profile) as dst:

            for block in TA._iter_windows(dst):

                if zeros:
                    arr = np.zeros((block.height, block.width), dtype=profile["dtype"])
                else:
                    arr = src.read(1, window=Window(
                        window.col_off + block.col_off,
                        window.row_off + block.row_off,
                        block.width,
                        block.height
                    ))

                dst.write(arr, 1, window=block)


def prepare_test_inputs(dem_path, water_path, bbox):

    # Returns (dem, water, notes) for the test run. With --bbox the
    # DEM (and water mask) are cropped into TEST_DIR; without a water
    # mask an all-zero one is written on the DEM grid.
    notes = []

    TEST_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    with rasterio.open(dem_path) as src:

        full = Window(0, 0, src.width, src.height)

        if bbox is not None:
            window = from_bounds(*bbox, transform=src.transform)
            window = window.round_offsets().round_lengths().intersection(full)
        else:
            window = full

    if bbox is not None:
        cropped = TEST_INPUT_DIR / "input_dem.tif"
        print(f"Cropping input DEM to {bbox} -> {cropped}")
        _copy_window(dem_path, cropped, window)
        notes.append(f"Test DEM cropped from `{dem_path}` to bbox {tuple(bbox)}.")
        dem_out = cropped
    else:
        dem_out = Path(dem_path)

    if water_path is not None:
        if bbox is not None:
            water_out = TEST_INPUT_DIR / "input_water.tif"
            _copy_window(water_path, water_out, window)
        else:
            water_out = Path(water_path)
    else:
        water_out = TEST_INPUT_DIR / "input_water.tif"
        print(f"No water mask given: writing an empty one -> {water_out}")
        _copy_window(dem_out, water_out, Window(0, 0, *_shape_wh(dem_out)),
                     dtype="uint8", nodata=0, zeros=True)
        notes.append(
            "No water mask was given for the test DEM, so an empty one was used: "
            "drainage_water is empty and the river bed uses HAND_FALLBACK_M."
        )

    return dem_out, water_out, notes


def _shape_wh(path):

    with rasterio.open(path) as src:
        return src.width, src.height


# ============================================================
# RUN THE PIPELINE
# ============================================================

def run_pipeline(name, dem_path, water_path, output_dir):

    # Full terrain analysis (every step) via RunPipeline; a failing
    # step is recorded and the remaining steps skipped, so the report
    # can still be written
    check_output_dir(output_dir)

    print("\n" + "#" * 60)
    print(f"# RUN: {name}  ({dem_path} -> {output_dir})")
    print("#" * 60)

    run = RunPipeline.run_analysis(
        dem_path, water_path, output_dir, KM_GPKG, enabled=None, stop_on_error=False
    )
    run["name"] = name

    return run


# ============================================================
# RESULTS OF ONE RUN
# ============================================================

def _read_layer(path, layer, bbox=None):

    if not Path(path).exists():
        return None

    if layer not in set(gpd.list_layers(path)["name"]):
        return None

    return gpd.read_file(path, layer=layer, bbox=bbox)


def run_results(run, info):

    p = run["paths"]
    res = {}

    with rasterio.open(run["dem"]) as src:
        res_m = abs(src.res[0])
        cells = {
            f"{scale} ({ha} ha)": TA._area_to_cells(ha * TA.HA, src)
            for scale, ha in TA.WATERSHED_THRESHOLDS_HA.items()
        }
        cells[f"edge basin ({TA.EDGE_BASIN_MIN_HA} ha)"] = TA._area_to_cells(
            TA.EDGE_BASIN_MIN_HA * TA.HA, src)
        cells[f"centerline ({TA.CENTERLINE_THRESHOLD_HA} ha)"] = TA._area_to_cells(
            TA.CENTERLINE_THRESHOLD_HA * TA.HA, src)

    for label, metres in [
        ("marker snap", TA.MARKER_SNAP_DISTANCE_M),
        ("naming radius", TA.NAMING_RADIUS_M),
        ("bed max distance", TA.BED_MAX_DISTANCE_M),
        ("water check buffer", TA.WATER_CHECK_BUFFER_M),
    ]:
        cells[f"{label} ({metres:g} m)"] = metres / res_m

    res["threshold_cells"] = cells

    res["watersheds"] = {}

    for scale in TA.WATERSHED_THRESHOLDS_HA:
        gdf = _read_layer(p.watersheds_vector_dir / f"watersheds_{scale}.gpkg", f"watersheds_{scale}")
        if gdf is None:
            res["watersheds"][scale] = None
            continue
        kinds = gdf["kind"].value_counts()
        res["watersheds"][scale] = {
            "units": len(gdf),
            "river": int(kinds.get("river", 0)),
            "edge": int(kinds.get("edge", 0)) + int(kinds.get("minor_edge", 0)),
        }

    lines = _read_layer(p.centerlines, "centerlines")
    rivers = _read_layer(p.centerlines, "main_rivers")
    validation = _read_layer(p.centerlines, "validation_points")
    bed = _read_layer(p.bed_gpkg, "river_bed")

    res["stream_links"] = None if lines is None else len(lines)
    res["stream_length_km"] = None if lines is None else float(lines.length.sum()) / 1000
    res["river_length_km"] = {} if rivers is None else {
        r["river_name"]: r.geometry.length / 1000 for _, r in rivers.iterrows()
    }
    res["markers_in_dem"] = None if validation is None else len(validation)
    res["markers_matched"] = None if rivers is None else int(rivers["n_markers_matched"].sum())
    res["bed_area_m2"] = None if bed is None else float(bed["area_m2"].sum())

    bed_summary = run["summaries"].get("river_bed") or {}
    res["hand_threshold_m"] = bed_summary.get("threshold")
    res["hand_raw_m"] = bed_summary.get("raw_threshold")
    res["hand_fallback"] = bed_summary.get("fallback")

    res["marker_check"] = run["summaries"].get("marker_check")

    area_km2 = info["stats"]["valid_area_km2"] or np.nan
    res["per_km2"] = {
        "small watersheds": (res["watersheds"].get("small") or {}).get("units", np.nan) / area_km2,
        "stream km": (res["stream_length_km"] or np.nan) / area_km2,
    }

    return res


# ============================================================
# OVERLAP COMPARISON
# ============================================================

def _clip(gdf, clip_box):

    if gdf is None or gdf.empty:
        return gdf

    gdf = gpd.clip(gdf, clip_box)
    return gdf[~gdf.geometry.is_empty]


def compare_watersheds(base, test, scale, clip_box, cell):

    layer = f"watersheds_{scale}"
    a = _clip(_read_layer(base["paths"].watersheds_vector_dir / f"{layer}.gpkg", layer, clip_box.bounds), clip_box)
    b = _clip(_read_layer(test["paths"].watersheds_vector_dir / f"{layer}.gpkg", layer, clip_box.bounds), clip_box)

    if a is None or b is None or a.empty or b.empty:
        return None

    a = a[["ws_id", "geometry"]].rename(columns={"ws_id": "id_a"})
    b = b[["ws_id", "geometry"]].rename(columns={"ws_id": "id_b"})
    a["area_a"] = a.area
    b["area_b"] = b.area

    inter = gpd.overlay(a, b, how="intersection", keep_geom_type=True)
    inter["inter"] = inter.area

    # Every baseline unit is matched with the test unit it overlaps most
    best = inter.sort_values("inter").drop_duplicates("id_a", keep="last")
    best["iou"] = best["inter"] / (best["area_a"] + best["area_b"] - best["inter"])

    matched = a.merge(best[["id_a", "iou"]], on="id_a", how="left").fillna({"iou": 0.0})

    # Units cut by the overlap edge (i.e. by either DEM's extent)
    edge = clip_box.boundary.buffer(cell)
    matched["interior"] = ~matched.geometry.intersects(edge)

    def stats(df):
        if df.empty:
            return {"n": 0, "iou_weighted": np.nan, "iou_median": np.nan, "pct_good": np.nan}
        return {
            "n": len(df),
            "iou_weighted": float(np.average(df["iou"], weights=df["area_a"])),
            "iou_median": float(df["iou"].median()),
            "pct_good": float((df["iou"] >= IOU_GOOD).mean() * 100),
        }

    return {"all": stats(matched), "interior": stats(matched[matched["interior"]])}


def _line_offsets(a, b):

    # Distances from points every CENTERLINE_SAMPLE_M along `a` to `b`
    if a is None or b is None or a.is_empty or b.is_empty:
        return np.zeros(0)

    points = shapely.get_coordinates(shapely.segmentize(a, CENTERLINE_SAMPLE_M))

    return shapely.distance(shapely.points(points), b)


def compare_lines(a, b):

    # Mean of both directions, max of both directions (Hausdorff-like)
    ab = _line_offsets(a, b)
    ba = _line_offsets(b, a)

    if ab.size == 0 or ba.size == 0:
        return None

    both = np.concatenate([ab, ba])

    return {"mean_m": float(both.mean()), "max_m": float(both.max())}


def compare_centerlines(base, test, clip_box):

    out = {}

    a = _clip(_read_layer(base["paths"].centerlines, "main_rivers", clip_box.bounds), clip_box)
    b = _clip(_read_layer(test["paths"].centerlines, "main_rivers", clip_box.bounds), clip_box)

    names = sorted(set([] if a is None else a["river_name"]) | set([] if b is None else b["river_name"]))

    for name in names:
        ga = None if a is None else a[a["river_name"] == name].geometry.union_all()
        gb = None if b is None else b[b["river_name"] == name].geometry.union_all()
        out[name] = compare_lines(ga, gb)

    a = _clip(_read_layer(base["paths"].centerlines, "centerlines", clip_box.bounds), clip_box)
    b = _clip(_read_layer(test["paths"].centerlines, "centerlines", clip_box.bounds), clip_box)

    out["(whole stream network)"] = compare_lines(
        None if a is None or a.empty else a.geometry.union_all(),
        None if b is None or b.empty else b.geometry.union_all()
    )

    return out


def compare_bed(base, test, clip_box):

    a = _clip(_read_layer(base["paths"].bed_gpkg, "river_bed", clip_box.bounds), clip_box)
    b = _clip(_read_layer(test["paths"].bed_gpkg, "river_bed", clip_box.bounds), clip_box)

    if a is None or b is None or a.empty or b.empty:
        return None

    ga = a.geometry.union_all()
    gb = b.geometry.union_all()
    inter = ga.intersection(gb).area

    return {
        "iou": inter / ga.union(gb).area,
        "pct_baseline_covered": 100 * inter / ga.area,
        "pct_test_in_baseline": 100 * inter / gb.area,
    }


def compare_elevation(base_dem, test_dem, clip_box):

    # Elevation difference test - baseline in the overlap, only when
    # both grids have the same cell size and are aligned
    with rasterio.open(base_dem) as a, rasterio.open(test_dem) as b:

        if a.res != b.res:
            return None

        wa = from_bounds(*clip_box.bounds, transform=a.transform)
        wb = from_bounds(*clip_box.bounds, transform=b.transform)

        offsets = [wa.col_off, wa.row_off, wb.col_off, wb.row_off]

        if not np.allclose(offsets, np.round(offsets), atol=1e-6):
            return None

        wa = wa.round_offsets().round_lengths()
        wb = wb.round_offsets().round_lengths()

        n = min(wa.height, wb.height) * min(wa.width, wb.width)
        dz_sum = dz_abs = 0.0
        count = 0

        # Row strips keep memory bounded for large overlaps
        height = int(min(wa.height, wb.height))
        width = int(min(wa.width, wb.width))

        for r in range(0, height, TA.BLOCK_SIZE):
            h = min(TA.BLOCK_SIZE, height - r)
            za = a.read(1, window=Window(wa.col_off, wa.row_off + r, width, h)).astype(np.float64)
            zb = b.read(1, window=Window(wb.col_off, wb.row_off + r, width, h)).astype(np.float64)
            ok = TA._valid(za, a.nodata) & TA._valid(zb, b.nodata)
            dz = zb[ok] - za[ok]
            dz_sum += dz.sum()
            dz_abs += np.abs(dz).sum()
            count += dz.size

    if count == 0:
        return None

    return {"mean_dz_m": dz_sum / count, "mean_abs_dz_m": dz_abs / count,
            "cells": count, "cells_total": n}


def compare_overlap(base, test, info_base, info_test):

    ax0, ay0, ax1, ay1 = info_base["bounds"]
    bx0, by0, bx1, by1 = info_test["bounds"]

    x0, y0, x1, y1 = max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1)

    if x0 >= x1 or y0 >= y1:
        return None

    clip_box = box(x0, y0, x1, y1)
    cell = max(info_base["res_x"], info_test["res_x"])

    return {
        "bounds": (x0, y0, x1, y1),
        "area_km2": clip_box.area / 1e6,
        "elevation": compare_elevation(base["dem"], test["dem"], clip_box),
        "watersheds": {
            scale: compare_watersheds(base, test, scale, clip_box, cell)
            for scale in TA.WATERSHED_THRESHOLDS_HA
        },
        "centerlines": compare_centerlines(base, test, clip_box),
        "bed": compare_bed(base, test, clip_box),
    }


# ============================================================
# REPORT
# ============================================================

def fmt(value, digits=2):

    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "n/a"
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    if isinstance(value, (int, np.integer)):
        return f"{value:,}"
    if isinstance(value, (float, np.floating)):
        return f"{value:,.{digits}f}"
    return str(value)


def diff(a, b):

    try:
        if a is None or b is None or isinstance(a, bool):
            return None
        return float(b) - float(a)
    except (TypeError, ValueError):
        return None


class Report:

    def __init__(self):
        self.rows = []   # CSV rows
        self.lines = []  # markdown

    def metric(self, section, metric, key, unit, a, b):
        self.rows.append({
            "section": section, "metric": metric, "key": key, "unit": unit,
            "baseline": a, "test": b, "difference": diff(a, b)
        })
        name = metric if not key else f"{metric} – {key}"
        unit = f" ({unit})" if unit else ""
        self.lines.append(f"| {name}{unit} | {fmt(a)} | {fmt(b)} | {fmt(diff(a, b))} |")

    def table(self, title=None):
        if title:
            self.lines += ["", f"### {title}", ""]
        self.lines += ["| Metric | Baseline | Test | Test − baseline |",
                       "|---|---:|---:|---:|"]

    def text(self, *lines):
        self.lines += list(lines)


def build_report(info, runs, results, overlap, notes):

    base, test = runs["baseline"], runs["test"]
    ib, it = info["baseline"], info["test"]
    rb, rt = results["baseline"], results["test"]

    rep = Report()

    # --------------------------------------------------------
    # DEM properties
    # --------------------------------------------------------

    rep.text("", "## DEM properties", "",
             f"- Baseline: `{ib['path']}`",
             f"- Test: `{it['path']}`", "")
    rep.table()

    for label, key in [("xmin", 0), ("ymin", 1), ("xmax", 2), ("ymax", 3)]:
        rep.metric("dem", "extent", label, "m", ib["bounds"][key], it["bounds"][key])

    rep.metric("dem", "width", "", "cells", ib["width"], it["width"])
    rep.metric("dem", "height", "", "cells", ib["height"], it["height"])
    rep.metric("dem", "size", "", "cells", ib["cells"], it["cells"])
    rep.metric("dem", "resolution", "", "m", ib["res_x"], it["res_x"])
    rep.metric("dem", "area", "", "km²", ib["stats"]["area_km2"], it["stats"]["area_km2"])
    rep.metric("dem", "valid area", "", "km²", ib["stats"]["valid_area_km2"], it["stats"]["valid_area_km2"])
    rep.metric("dem", "nodata", "", "%", ib["stats"]["nodata_pct"], it["stats"]["nodata_pct"])
    for key in ["z_min", "z_max", "z_mean"]:
        rep.metric("dem", "elevation", key[2:], "m", ib["stats"][key], it["stats"][key])

    # --------------------------------------------------------
    # Performance
    # --------------------------------------------------------

    rep.text("", "## Performance", "",
             "Peak memory is the peak committed memory of Python and Whitebox together "
             "during each step (Windows job object).", "")
    rep.table()

    def mib(n):
        return None if n is None else n / 2**20

    for step in ANALYSIS_STEPS:
        sb, st = base["steps"][step], test["steps"][step]
        rep.metric("performance", "time", step, "s", sb["seconds"], st["seconds"])
        rep.metric("performance", "peak memory", step, "MiB", mib(sb["peak_bytes"]), mib(st["peak_bytes"]))

    rep.metric("performance", "time", "total", "s", base["total_seconds"], test["total_seconds"])
    rep.metric("performance", "peak memory", "total", "MiB",
               mib(base["total_peak_bytes"]), mib(test["total_peak_bytes"]))

    t_cell = {k: runs[k]["total_seconds"] / info[k]["cells"] * 1e6 for k in runs}
    m_cell = {k: (runs[k]["total_peak_bytes"] or np.nan) / info[k]["cells"] for k in runs}
    rep.metric("performance", "time per million cells", "", "s", t_cell["baseline"], t_cell["test"])
    rep.metric("performance", "peak memory per cell", "", "bytes", m_cell["baseline"], m_cell["test"])

    # --------------------------------------------------------
    # Results
    # --------------------------------------------------------

    rep.text("", "## Results")
    rep.table("Watersheds")

    for scale in TA.WATERSHED_THRESHOLDS_HA:
        wb, wt = rb["watersheds"][scale] or {}, rt["watersheds"][scale] or {}
        for key in ["units", "river", "edge"]:
            rep.metric("results", f"watersheds {scale}", key, "", wb.get(key), wt.get(key))

    rep.table("Streams and rivers")
    rep.metric("results", "stream links", "", "", rb["stream_links"], rt["stream_links"])
    rep.metric("results", "stream network length", "", "km", rb["stream_length_km"], rt["stream_length_km"])

    for name in sorted(set(rb["river_length_km"]) | set(rt["river_length_km"])):
        rep.metric("results", "river centerline length", name, "km",
                   rb["river_length_km"].get(name), rt["river_length_km"].get(name))

    rep.metric("results", "km markers inside DEM", "", "", rb["markers_in_dem"], rt["markers_in_dem"])
    rep.metric("results", "km markers matched to a named river", "", "",
               rb["markers_matched"], rt["markers_matched"])

    rep.table("River bed")
    rep.metric("results", "river bed area", "", "m²", rb["bed_area_m2"], rt["bed_area_m2"])
    rep.metric("results", "HAND threshold chosen", "", "m", rb["hand_threshold_m"], rt["hand_threshold_m"])
    rep.metric("results", "HAND percentile before clamp", "", "m", rb["hand_raw_m"], rt["hand_raw_m"])
    rep.metric("results", "HAND fallback used", "", "", rb["hand_fallback"], rt["hand_fallback"])

    rep.table("Centerlines vs river km markers")
    mb, mt = rb["marker_check"] or {}, rt["marker_check"] or {}
    rep.metric("results", "marker check", "markers checked", "", mb.get("markers"), mt.get("markers"))
    rep.metric("results", "marker check", "on the centerline", "", mb.get("on_centerline"), mt.get("on_centerline"))
    rep.metric("results", "marker check", "near but offset", "", mb.get("near_offset"), mt.get("near_offset"))
    rep.metric("results", "marker check", "no centerline nearby", "",
               mb.get("no_centerline_nearby"), mt.get("no_centerline_nearby"))
    rep.metric("results", "marker check", "median distance of near markers", "m",
               mb.get("median_dist_near_m"), mt.get("median_dist_near_m"))
    rep.metric("results", "marker check", "centerline raster on DEM grid", "",
               mb.get("grid_ok"), mt.get("grid_ok"))
    rep.text("", f"\"On the centerline\" = the marker's cell is within "
                 f"{TA.MARKER_ON_TOLERANCE_CELLS} cell(s) of a centerline-raster cell; \"near\" "
                 f"= a centerline within {TA.MARKER_CHECK_DISTANCE_M:g} m. Per-marker results: "
                 f"`{TA.MARKER_CHECK_GPKG.name}` in each run's vector folder.")

    rep.table("Density (per km² of valid DEM)")
    for key in rb["per_km2"]:
        rep.metric("results", "per km²", key, "", rb["per_km2"][key], rt["per_km2"][key])

    rep.table("Thresholds converted to cells")
    for key in rb["threshold_cells"]:
        rep.metric("results", "threshold", key, "cells", rb["threshold_cells"][key], rt["threshold_cells"][key])

    # --------------------------------------------------------
    # Overlap
    # --------------------------------------------------------

    rep.text("", "## Overlap check", "")

    if overlap is None:
        rep.text("The two DEMs do not overlap, so this section is skipped.")
    else:
        x0, y0, x1, y1 = overlap["bounds"]
        rep.text(
            f"Shared area: x {x0:,.1f}–{x1:,.1f}, y {y0:,.1f}–{y1:,.1f} "
            f"({overlap['area_km2']:.2f} km²). Results are compared inside it only.",
            "",
            "Differences near the edges of the shared area are expected: the DEM extent "
            "changes what drains where, so units and streams cut by either DEM's edge "
            "can legitimately differ. \"Interior\" watersheds exclude units touching the "
            "shared area's edge. Whitebox depression filling is also not deterministic "
            "(two runs on the same DEM differ by < 1 mm on flats), so small differences "
            "appear even for identical DEMs.", ""
        )

        rep.lines += ["| Metric | Value |", "|---|---:|"]

        def single(metric, key, unit, value):
            rep.rows.append({"section": "overlap", "metric": metric, "key": key, "unit": unit,
                             "baseline": None, "test": None, "difference": value})
            name = metric if not key else f"{metric} – {key}"
            rep.lines.append(f"| {name}{f' ({unit})' if unit else ''} | {fmt(value)} |")

        single("overlap area", "", "km²", overlap["area_km2"])

        if overlap["elevation"]:
            e = overlap["elevation"]
            single("elevation difference test − baseline", "mean", "m", e["mean_dz_m"])
            single("elevation difference test − baseline", "mean absolute", "m", e["mean_abs_dz_m"])

        for scale, w in overlap["watersheds"].items():
            if w is None:
                single(f"watershed IoU {scale}", "", "", None)
                continue
            for group in ["all", "interior"]:
                s = w[group]
                single(f"watershed IoU {scale} ({group}, {s['n']} units)", "area-weighted mean", "", s["iou_weighted"])
                single(f"watershed IoU {scale} ({group})", "median", "", s["iou_median"])
                single(f"watershed IoU {scale} ({group})", f"units with IoU ≥ {IOU_GOOD}", "%", s["pct_good"])

        for name, c in overlap["centerlines"].items():
            if c is None:
                single(f"centerline offset {name}", "", "m", None)
                continue
            single(f"centerline offset {name}", "mean", "m", c["mean_m"])
            single(f"centerline offset {name}", "max", "m", c["max_m"])

        b = overlap["bed"]
        single("river bed", "IoU", "", None if b is None else b["iou"])
        single("river bed", "baseline bed covered by test bed", "%", None if b is None else b["pct_baseline_covered"])
        single("river bed", "test bed inside baseline bed", "%", None if b is None else b["pct_test_in_baseline"])

    # --------------------------------------------------------
    # Summary (top of the report)
    # --------------------------------------------------------

    summary, flags = summarise(info, runs, results, overlap, t_cell)

    header = [
        "# Terrain analysis: test DEM vs baseline",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `src/TestFullDem.py`. "
        f"Raw numbers: `{METRICS_CSV.name}`.",
        "",
        "## Summary",
        "",
        *summary,
        "",
        "**Worth checking:**" if flags else "**Nothing suspicious detected.**",
        *[f"- {f}" for f in flags],
    ]

    if notes:
        header += ["", "**Notes:**", *[f"- {n}" for n in notes]]

    return "\n".join(header + rep.lines) + "\n", pd.DataFrame(rep.rows)


def summarise(info, runs, results, overlap, t_cell):

    base, test = runs["baseline"], runs["test"]
    ib, it = info["baseline"], info["test"]
    rb, rt = results["baseline"], results["test"]

    lines = []
    flags = []

    size_ratio = it["cells"] / ib["cells"]

    if test["failed"]:
        lines.append(f"**The test run failed** at {test['failed']}. Later steps were skipped.")
    else:
        ratio = t_cell["test"] / t_cell["baseline"]
        verdict = (
            "scales about linearly" if ratio <= SCALING_MAX_RATIO
            else "scales worse than linearly"
        )
        lines.append(
            f"The test DEM has {it['cells']:,} cells ({size_ratio:.1f}× the baseline). "
            f"The full pipeline ran in {test['total_seconds']:.0f} s vs "
            f"{base['total_seconds']:.0f} s, i.e. {t_cell['test']:.2f} vs "
            f"{t_cell['baseline']:.2f} s per million cells: the code {verdict} "
            f"(ratio {ratio:.2f})."
        )
        if ratio > SCALING_MAX_RATIO:
            flags.append(f"Time per cell is {ratio:.1f}× the baseline (superlinear).")

        bpc = (test["total_peak_bytes"] or np.nan) / it["cells"]
        if not np.isnan(bpc):
            lines.append(
                f"Peak memory was {gib(test['total_peak_bytes']):.2f} GiB "
                f"({bpc:.0f} bytes per cell; the estimate uses {BYTES_PER_CELL_RAM})."
            )

    if base["failed"]:
        flags.append(f"The baseline run failed at {base['failed']}.")

    changes = []
    for scale in TA.WATERSHED_THRESHOLDS_HA:
        a = (rb["watersheds"][scale] or {}).get("units")
        b = (rt["watersheds"][scale] or {}).get("units")
        if a is not None and b is not None:
            changes.append(f"{scale} watersheds {a:,} → {b:,}")
    if rb["stream_length_km"] is not None and rt["stream_length_km"] is not None:
        changes.append(f"stream network {rb['stream_length_km']:.1f} → {rt['stream_length_km']:.1f} km")
    changes.append(f"named rivers {len(rb['river_length_km'])} → {len(rt['river_length_km'])}")
    if rb["bed_area_m2"] is not None or rt["bed_area_m2"] is not None:
        changes.append(f"river bed {fmt(rb['bed_area_m2'], 0)} → {fmt(rt['bed_area_m2'], 0)} m²")
    lines.append("What changed (baseline → test): " + "; ".join(changes) + ".")

    if ib["res_x"] != it["res_x"]:
        flags.append(
            f"Resolutions differ ({ib['res_x']} vs {it['res_x']} m): all ha/m thresholds "
            "convert to different cell counts, so results are not directly comparable."
        )

    if it["stats"]["nodata_pct"] > 0:
        flags.append(
            f"The test DEM has {it['stats']['nodata_pct']:.1f}% nodata. Slope/aspect do not "
            "mask nodata (unchanged behaviour), so values next to nodata cells are not meaningful."
        )

    if rt["markers_in_dem"] and not rt["river_length_km"]:
        flags.append("Km markers fall inside the test DEM but no river was named.")

    mt = rt["marker_check"]
    if mt and mt["markers"]:
        off = mt["near_offset"] + mt["no_centerline_nearby"]
        if off > 0.25 * mt["markers"]:
            flags.append(
                f"{off} of {mt['markers']} km markers in the test DEM are not on a centerline "
                f"({mt['near_offset']} offset, {mt['no_centerline_nearby']} with none nearby)."
            )

    if rt["hand_fallback"]:
        flags.append("The test river bed used HAND_FALLBACK_M (no water-mask cells near the rivers).")

    if overlap is not None:

        e = overlap["elevation"]
        if e and e["mean_abs_dz_m"] > ELEVATION_MAX_MEAN_ABS_M:
            flags.append(f"Mean |elevation difference| in the overlap is {e['mean_abs_dz_m']:.2f} m.")

        for scale, w in overlap["watersheds"].items():
            if w and w["interior"]["n"] and w["interior"]["iou_weighted"] < IOU_GOOD:
                flags.append(
                    f"Interior {scale} watersheds agree poorly in the overlap "
                    f"(area-weighted IoU {w['interior']['iou_weighted']:.2f})."
                )

        for name, c in overlap["centerlines"].items():
            if c and c["mean_m"] > CENTERLINE_MAX_MEAN_M:
                flags.append(f"Centerline '{name}' is on average {c['mean_m']:.1f} m apart in the overlap.")
            if c is None and name != "(whole stream network)":
                flags.append(f"River '{name}' exists in only one run inside the overlap.")

        b = overlap["bed"]
        if b and b["iou"] < IOU_GOOD:
            flags.append(f"River bed IoU in the overlap is only {b['iou']:.2f}.")

        lines.append(
            f"The DEMs overlap on {overlap['area_km2']:.2f} km²; see the overlap check below."
        )
    else:
        lines.append("The DEMs do not overlap, so results could not be compared spatially.")

    return lines, flags


# ============================================================
# MAIN
# ============================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description="Run the full terrain analysis on a DEM and compare it with the main DEM."
    )
    parser.add_argument("dem", type=Path, nargs="?", default=None,
                        help="DEM GeoTIFF in EPSG:2169 (default: paths.comparison_dem)")
    parser.add_argument("--water", type=Path, default=None,
                        help="water mask aligned with the DEM (default: paths.comparison_water, "
                             "else an empty mask)")
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                        help="crop the DEM to this box (EPSG:2169) before running "
                             "(default: paths.comparison_bbox)")
    parser.add_argument("--force", action="store_true",
                        help="run even if the estimated memory or disk exceeds what is available")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"TOML config (default: {PipelineConfig.DEFAULT_CONFIG})")

    return parser.parse_args()


def run_comparison(dem, water=None, bbox=None, force=False, config_path=None):

    # Baseline (main DEM) and test (dem) runs, then the report.
    # Raises ValueError with a clear message if the input is unusable.
    # Returns the report path.
    dem = Path(dem)
    water = Path(water) if water else None
    bbox = list(bbox) if bbox else None

    for path in [dem, water, BASELINE_DEM, BASELINE_WATER]:
        if path is not None and not Path(path).exists():
            raise ValueError(f"file not found: {path}")

    print("=" * 60)
    print("TEST FULL DEM")
    print("=" * 60)

    # --------------------------------------------------------
    # Check the input before doing anything
    # --------------------------------------------------------

    try:
        source = describe(dem)
    except rasterio.errors.RasterioIOError as error:
        raise ValueError(f"cannot read {dem} as a raster: {error}")

    if not source["crs_ok"]:
        with rasterio.open(dem) as src:
            crs = src.crs
        raise ValueError(
            f"{dem} is not in EPSG:{REQUIRED_EPSG} (found: "
            f"{crs.to_string() if crs else 'no CRS'}). Reproject it first, e.g.\n"
            f"  gdalwarp -t_srs EPSG:{REQUIRED_EPSG} input.tif output.tif"
        )

    cells = source["cells"]

    if bbox is not None:
        with rasterio.open(dem) as src:
            window = from_bounds(*bbox, transform=src.transform)
            window = window.round_offsets().round_lengths().intersection(
                Window(0, 0, src.width, src.height))
        cells = int(window.width) * int(window.height)

    total_ram, free_ram = available_ram()
    free_disk = shutil.disk_usage(Path(".").resolve()).free
    need_ram, need_disk = cells * BYTES_PER_CELL_RAM, cells * BYTES_PER_CELL_DISK

    print(f"Input DEM:   {dem}")
    print(f"  Size:       {source['width']:,} x {source['height']:,} = {source['cells']:,} cells")
    if bbox is not None:
        print(f"  Cropped to: {tuple(bbox)} -> {cells:,} cells")
    print(f"  Resolution: {source['res_x']} x {source['res_y']} m")
    print(f"  Data type:  {source['dtype']} (nodata {source['nodata']})")
    print(f"  Estimated peak memory: {gib(need_ram):.1f} GiB "
          f"(free {gib(free_ram):.1f} of {gib(total_ram):.1f} GiB)" if free_ram else
          f"  Estimated peak memory: {gib(need_ram):.1f} GiB")
    print(f"  Estimated disk use:    {gib(need_disk):.1f} GiB (free {gib(free_disk):.1f} GiB)")

    too_big = []
    if free_ram and need_ram > RESOURCE_MARGIN * free_ram:
        too_big.append("memory")
    if need_disk > RESOURCE_MARGIN * free_disk:
        too_big.append("disk")

    if too_big and not force:
        side = int(np.sqrt(RESOURCE_MARGIN * (free_ram or need_ram) / BYTES_PER_CELL_RAM)) * source["res_x"]
        raise ValueError(
            f"the estimated {' and '.join(too_big)} use exceeds what is available.\n"
            f"Whitebox loads whole rasters into memory, so this DEM cannot run on this machine.\n"
            f"Crop it with --bbox / paths.comparison_bbox (about {side / 1000:.0f} x "
            f"{side / 1000:.0f} km fits), or use --force / comparison.force to try anyway."
        )

    # --------------------------------------------------------
    # Fresh output folders
    # --------------------------------------------------------

    for folder in [BASELINE_DIR, TEST_DIR]:
        check_output_dir(folder)
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)

    check_output_dir(CONFIG_COPY)
    shutil.copyfile(config_path or PipelineConfig.config_path(), CONFIG_COPY)

    test_dem, test_water, notes = prepare_test_inputs(dem, water, bbox)

    notes.append(
        f"Both runs use the km markers in `{KM_GPKG}` "
        f"(paths.comparison_km_gpkg; the main run uses `{TA.RIVER_KM_GPKG}`)."
    )

    # --------------------------------------------------------
    # Runs
    # --------------------------------------------------------

    runs = {
        "baseline": run_pipeline("baseline", BASELINE_DEM, BASELINE_WATER, BASELINE_DIR),
        "test": run_pipeline("test", test_dem, test_water, TEST_DIR),
    }

    # --------------------------------------------------------
    # Comparison
    # --------------------------------------------------------

    print("\nComparing runs...")

    info = {}
    results = {}

    for key, run in runs.items():
        info[key] = describe(run["dem"])
        info[key]["stats"] = dem_statistics(run["dem"])
        results[key] = run_results(run, info[key])

    overlap = compare_overlap(runs["baseline"], runs["test"], info["baseline"], info["test"])

    report, metrics = build_report(info, runs, results, overlap, notes)

    check_output_dir(REPORT_PATH)
    REPORT_PATH.write_text(report, encoding="utf-8")
    metrics.to_csv(METRICS_CSV, index=False)

    print(f"\n  Created: {REPORT_PATH}")
    print(f"  Created: {METRICS_CSV}")
    print(f"  Created: {CONFIG_COPY}")

    return REPORT_PATH


def main():

    args = parse_args()

    if not Path("visualization").is_dir():
        sys.exit("ERROR: run this script from the project root (folder containing visualization/).")

    paths = CFG["paths"]

    dem = args.dem or PipelineConfig.optional_path(paths["comparison_dem"])
    water = args.water or PipelineConfig.optional_path(paths["comparison_water"])
    bbox = args.bbox or paths["comparison_bbox"] or None

    if dem is None:
        sys.exit("ERROR: no DEM given and paths.comparison_dem is empty in the config.")

    try:
        run_comparison(dem, water, bbox, args.force or _CMP["force"], args.config)
    except ValueError as error:
        sys.exit(f"\nERROR: {error}")


if __name__ == "__main__":
    main()
