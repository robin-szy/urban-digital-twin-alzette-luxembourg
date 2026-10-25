"""Select complete ownership grid cells; never clip or estimate AOI ownership."""
from __future__ import annotations

import json
import math
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from pyproj import CRS, Transformer

from devika.src.landuse import PreparationError, make_subset, progress, validate_polygons

REQUIRED_FIELDS = ("TOP_LEFT", "PUBLIC_PERCENT", "Commune_percent", "Etat_percent", "Autre_percent")
INTERPRETATION = (
    "Ownership percentages describe the original complete nominal 1 km² grid cells, "
    "not exact parcel ownership or ownership percentages within the selected AOI. "
    "Cells are selected by intersection and are never clipped or area-weighted."
)


def validate_attributes(frame, required_fields, *, percentage_tolerance=0):
    if (isinstance(percentage_tolerance, bool) or not math.isfinite(percentage_tolerance)
            or not 0 <= percentage_tolerance < 1):
        raise PreparationError("percentage_tolerance must be finite and at least 0 but less than 1 percentage point.")
    missing = set(REQUIRED_FIELDS).union(required_fields) - set(frame.columns)
    if missing:
        raise PreparationError(f"Missing ownership attributes: {', '.join(sorted(missing))}")
    identifiers = frame["TOP_LEFT"]
    if identifiers.isna().any() or identifiers.astype(str).str.strip().eq("").any():
        raise PreparationError("TOP_LEFT must be populated for every selected cell.")
    if identifiers.duplicated().any():
        raise PreparationError("Duplicate TOP_LEFT identifiers; inspect the source before exporting.")
    percentage_fields = [field for field in frame.columns if "percent" in field.casefold()]
    summaries = {}
    for field in percentage_fields:
        values = frame[field]
        if values.dropna().map(lambda value: isinstance(value, bool)).any():
            raise PreparationError(f"{field}: boolean values are not ownership percentages.")
        numeric = pd.to_numeric(values, errors="coerce")
        invalid = values.notna() & (numeric.isna() | ~numeric.between(-percentage_tolerance, 100 + percentage_tolerance))
        if invalid.any():
            raise PreparationError(f"{field}: {int(invalid.sum())} nonnumeric, non-finite or out-of-range percentages (expected 0–100).")
        summaries[field] = {
            "min": float(numeric.min()) if numeric.notna().any() else None,
            "max": float(numeric.max()) if numeric.notna().any() else None,
            "null_count": int(values.isna().sum()),
            "numeric_string_count": int(values.map(lambda value: isinstance(value, str)).sum()),
            "rounding_exceptions": [
                {"TOP_LEFT": str(frame.loc[index, "TOP_LEFT"]), "value": float(numeric.loc[index])}
                for index in frame.index[values.notna() & ~numeric.between(0, 100)]
            ],
        }
    if frame["PUBLIC_PERCENT"].isna().all():
        raise PreparationError("PUBLIC_PERCENT is missing for every selected cell.")
    # Values are validated but never converted, filled, normalised, or summed:
    # category definitions may overlap (PUBLIC_PERCENT includes subcategories).
    return summaries


def prepare_ownership(source, output, **kwargs):
    audit = {"status": "running", "source": str(Path(source).resolve()),
             "output": str(Path(output).resolve()), "interpretation": INTERPRETATION}
    try:
        return _prepare_ownership(source, output, audit=audit, **kwargs)
    except Exception as error:
        audit.update(status="failed", error=str(error))
        destination = Path(output).resolve()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            failure = destination.with_suffix(f".failure-{time.time_ns()}.json")
            with failure.open("x") as handle:
                json.dump(audit, handle, indent=2, ensure_ascii=False)
            progress(f"Failure report: {failure}")
        except OSError:
            progress("Could not save failure report; original error follows.")
        if isinstance(error, (ValueError, RuntimeError, OSError)):
            raise
        raise PreparationError(str(error)) from error


def _prepare_ownership(source, output, *, audit, all_features=False, bbox=None, bbox_crs=None, aoi_path=None,
                       required_fields=REQUIRED_FIELDS, max_features=50000,
                       max_output_mb=25, max_feature_mb=4, percentage_tolerance=0):
    source, output = Path(source).resolve(), Path(output).resolve()
    report_path = output.with_suffix(".report.json")
    if not source.is_file():
        raise PreparationError(f"Source file does not exist: {source}")
    if output.suffix != ".geojson":
        raise PreparationError("Output must use the .geojson suffix.")
    if output.exists() or report_path.exists():
        raise PreparationError("Output or report already exists; choose a new output filename.")
    if isinstance(max_features, bool) or not isinstance(max_features, int) or max_features < 1:
        raise PreparationError("max_features must be a positive integer.")
    for value in (max_output_mb, max_feature_mb):
        if not math.isfinite(value) or value <= 0:
            raise PreparationError("Export size limits must be positive and finite.")
    if all_features and any(value is not None for value in (bbox, bbox_crs, aoi_path)):
        raise PreparationError("Full-source mode cannot be combined with an AOI or bbox CRS.")
    subset = None if all_features else make_subset(bbox=bbox, bbox_crs=bbox_crs, aoi_path=aoi_path)
    info = pyogrio.read_info(source)
    if info["crs"] is None or CRS.from_user_input(info["crs"]).to_epsg() is None:
        raise PreparationError("Ownership source requires an EPSG-resolvable CRS; none will be guessed.")
    source_crs = CRS.from_user_input(info["crs"])
    bounds = None if all_features else Transformer.from_crs(subset.crs, source_crs, always_xy=True).transform_bounds(
        *subset.total_bounds, densify_pts=21
    )
    if bounds is not None and not all(math.isfinite(value) for value in bounds):
        raise PreparationError("AOI cannot be transformed to source CRS.")
    progress("Reading all ownership cells..." if all_features else "Reading ownership cells intersecting AOI bounds...")
    candidates = pyogrio.read_dataframe(source, bbox=bounds, max_features=max_features + 1, fid_as_index=True)
    if len(candidates) > max_features:
        raise PreparationError(f"Ownership selection exceeds the {max_features}-candidate limit; reduce the AOI.")
    validate_polygons(candidates, "Ownership candidates")
    source_aoi = None if all_features else subset.to_crs(source_crs).geometry.union_all()
    selected = candidates.copy() if all_features else candidates.loc[candidates.intersects(source_aoi)].copy()
    validate_polygons(selected, "Selected ownership cells")
    percentages = validate_attributes(selected, required_fields, percentage_tolerance=percentage_tolerance)
    audit.update(source_crs=source_crs.to_string(), candidate_count=len(candidates),
                 feature_count=len(selected), percentage_validation=percentages,
                 percentage_tolerance=percentage_tolerance,
                 source_fids=[int(fid) for fid in selected.index])
    # Complete horizontal footprints are retained; do not clip, explode or repair.
    output_z_count = int(selected.geometry.has_z.sum())
    # Transform with source Z intact: some datum transformations use height
    # when calculating XY. Remove Z only from the final horizontal overlay.
    result = selected.to_crs(4326)
    result.geometry = result.geometry.map(shapely.force_2d)
    validate_polygons(result, "Export ownership cells")
    xmin, ymin, xmax, ymax = result.total_bounds
    if not (-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90):
        raise PreparationError("Export coordinates exceed longitude/latitude bounds.")
    # Grid area is a geometry sanity check, not an AOI composition calculation.
    areas = result.to_crs(2169).geometry.area
    nominal_deviations = int(((areas - 1_000_000).abs() > 10_000).sum())
    attributes = [column for column in result.columns if column != result.geometry.name]
    report = {
        **audit, "output_crs": "EPSG:4326", "attributes": attributes,
        "geometry_repairs": 0, "cells_clipped": 0, "output_pieces_with_z_removed": output_z_count,
        "output_bounds": [float(value) for value in result.total_bounds],
        "selection": {"method": "intersects, including boundary touches; complete original cells retained",
                      "bbox": list(bbox) if bbox is not None else None,
                      "bbox_crs": str(bbox_crs) if bbox_crs is not None else None,
                      "aoi_path": str(Path(aoi_path).resolve()) if aoi_path else None},
        "grid_area_check": {"crs": "EPSG:2169", "min_m2": float(areas.min()), "max_m2": float(areas.max()),
                            "nominal_m2": 1_000_000, "cells_outside_one_percent_tolerance": nominal_deviations},
        "warnings": [], "versions": {"geopandas": gpd.__version__, "pyogrio": pyogrio.__version__,
                                       "shapely": shapely.__version__},
    }
    if all_features:
        report["selection"]["method"] = "all source cells; complete original cells retained"
        report["interpretation"] = INTERPRETATION.replace("Cells are selected by intersection", "All source cells are selected")
    rounding_count = sum(len(stats["rounding_exceptions"]) for stats in percentages.values())
    if rounding_count:
        report["warnings"].append(
            f"{rounding_count} percentage value(s) outside 0–100 accepted with explicit "
            f"{percentage_tolerance}-percentage-point rounding tolerance; original values preserved. "
            "See percentage_validation rounding_exceptions."
        )
    if nominal_deviations:
        report["warnings"].append("Some cell areas differ from nominal 1 km² by more than 1%; original footprints retained for review.")
    if any(stats["null_count"] for stats in percentages.values()):
        report["warnings"].append("Missing percentage values were retained as null, not replaced with zero.")
    if any(stats["numeric_string_count"] for stats in percentages.values()):
        report["warnings"].append("Numeric strings were validated but preserved; styling must account for original field types.")
    output.parent.mkdir(parents=True, exist_ok=True)
    progress(f"Validating {len(result)} complete ownership cells...")
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        temporary = Path(staging) / output.name
        def encode_attribute(value):
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            raise TypeError(f"Unsupported attribute type: {type(value).__name__}")
        serialized = result.to_json(drop_id=True, ensure_ascii=False, allow_nan=False, default=encode_attribute)
        temporary.write_text(serialized)
        file_bytes = temporary.stat().st_size
        if file_bytes == 0 or file_bytes > max_output_mb * 1024**2:
            raise PreparationError(f"Output is {file_bytes} bytes; limit is {max_output_mb} MiB.")
        features = json.loads(serialized)["features"]
        feature_bytes = max(len(json.dumps(feature, ensure_ascii=False).encode("utf-8")) for feature in features)
        if feature_bytes > max_feature_mb * 1024**2:
            raise PreparationError(f"A cell exceeds the {max_feature_mb} MiB feature limit.")
        restored = gpd.read_file(temporary, engine="pyogrio")
        audit["round_trip"] = {"expected_crs": "EPSG:4326", "observed_crs": str(restored.crs),
                               "expected_count": len(result), "observed_count": len(restored)}
        if restored.crs is None or restored.crs.to_epsg() != 4326 or len(restored) != len(result):
            raise PreparationError(f"Ownership export read-back mismatch: {audit['round_trip']}")
        validate_polygons(restored, "Ownership round trip")
        validate_attributes(restored, required_fields, percentage_tolerance=percentage_tolerance)
        if not shapely.equals_exact(result.geometry.array, restored.geometry.array, tolerance=1e-12).all():
            raise PreparationError("Round trip changed complete cell geometry.")
        restored_properties = json.loads(restored.to_json(drop_id=True, default=encode_attribute))["features"]
        if [item["properties"] for item in restored_properties] != [item["properties"] for item in features]:
            raise PreparationError("Round trip changed ownership attributes or values.")
        report.update(status="validated", file_size_bytes=file_bytes, largest_feature_bytes=feature_bytes,
                      complete_cell_geometries_preserved=True, all_attributes_preserved=True,
                      valid_geometry_count=len(restored), round_trip=audit["round_trip"])
        temp_report = Path(staging) / report_path.name
        temp_report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        temporary.rename(output)
        temp_report.rename(report_path)
    progress(f"Validated ownership GeoJSON: {output} ({file_bytes:,} bytes)")
    return report
