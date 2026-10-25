"""Prepare a bounded LIS-L subset without importing or modifying EDDIE."""
from __future__ import annotations

import json
import math
import tempfile
import sys
import time
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pyogrio
import pandas as pd
from pyproj import CRS, Transformer
import shapely
from shapely.geometry import box
from shapely.geometry.polygon import orient
from shapely.ops import transform
from shapely.validation import explain_validity, make_valid


class PreparationError(ValueError):
    """An input cannot safely be prepared for static ingestion."""


def validate_polygons(frame: gpd.GeoDataFrame, label: str) -> None:
    if frame.crs is None or frame.crs.to_epsg() is None:
        raise PreparationError(f"{label}: an explicit EPSG-resolvable CRS is required; do not guess it.")
    if frame.empty:
        raise PreparationError(f"{label}: no features found.")
    geometry = frame.geometry
    bad = geometry.isna() | geometry.is_empty | ~geometry.is_valid
    bad |= ~geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    if bad.any():
        raise PreparationError(
            f"{label}: {int(bad.sum())} missing, empty, invalid or non-polygon geometries. "
            "No validated output was published."
        )
    if not all(math.isfinite(float(v)) for v in frame.total_bounds):
        raise PreparationError(f"{label}: non-finite coordinates.")


def make_subset(*, bbox=None, bbox_crs=None, aoi_path=None) -> gpd.GeoDataFrame:
    if (bbox is None) == (aoi_path is None):
        raise PreparationError("Choose exactly one subset: bbox with CRS, or an AOI file.")
    if aoi_path is not None:
        subset = gpd.read_file(aoi_path, engine="pyogrio")
    else:
        if bbox_crs is None:
            raise PreparationError("bbox_crs is required; coordinates alone do not identify a CRS.")
        if len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox):
            raise PreparationError("bbox must contain four finite values: xmin ymin xmax ymax.")
        xmin, ymin, xmax, ymax = map(float, bbox)
        if xmin >= xmax or ymin >= ymax:
            raise PreparationError("bbox must have xmin < xmax and ymin < ymax.")
        crs = CRS.from_user_input(bbox_crs)
        if crs.is_geographic and not (-180 <= xmin < xmax <= 180 and -90 <= ymin < ymax <= 90):
            raise PreparationError("Geographic bbox is outside longitude/latitude bounds.")
        subset = gpd.GeoDataFrame(geometry=[box(xmin, ymin, xmax, ymax)], crs=crs)
    validate_polygons(subset, "Subset")
    return subset


# Preserve full coordinate precision. Allow only sub-millimetre numerical error
# when comparing read-back polygons to the EPSG:4326 AOI (about 0.2 mm).
CONTAINMENT_TOLERANCE = 2e-9


def progress(message):
    print(message, file=sys.stderr, flush=True)


def polygon_parts(geometry):
    """Return polygon components and counts of excluded lower-dimensional parts."""
    if geometry.is_empty:
        return [], {}
    if geometry.geom_type == "Polygon":
        return [geometry], {}
    if geometry.geom_type in ("MultiPolygon", "GeometryCollection", "MultiLineString", "MultiPoint"):
        polygons, excluded = [], {}
        for part in geometry.geoms:
            found, other = polygon_parts(part)
            polygons.extend(found)
            for kind, count in other.items():
                excluded[kind] = excluded.get(kind, 0) + count
        return polygons, excluded
    return [], {geometry.geom_type: 1}


def check_containment(frame, aoi):
    if not frame.geometry.covered_by(aoi.buffer(CONTAINMENT_TOLERANCE)).all():
        raise PreparationError("Output geometry extends outside the configured AOI.")


def prepare_landuse(source, output, **kwargs) -> dict:
    """Prepare a clipped copy, retaining diagnostics even if preparation fails."""
    audit = {"status": "running", "source": str(Path(source).resolve()),
             "output": str(Path(output).resolve()), "repair_records": [], "geometry_events": []}
    try:
        return _prepare_landuse(source, output, audit=audit, **kwargs)
    except Exception as error:
        audit.update(status="failed", error=str(error))
        # Unique failure reports preserve earlier successful outputs and diagnostics.
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


def _prepare_landuse(
    source: str | Path,
    output: str | Path,
    *,
    audit: dict,
    all_features: bool = False,
    source_fids=None,
    layer: str = "LandUse_status_2024",
    category_field: str = "LU1_level__3_name",
    bbox=None,
    bbox_crs=None,
    aoi_path=None,
    max_features: int = 50000,
    repair_invalid: bool = False,
    max_output_mb: float = 25,
    max_feature_mb: float = 4,
) -> dict:
    """Filter component bounds, repair locally relevant parts, clip and export.

    Whole national MultiPolygons must be read by the vector driver, but remote
    parts are discarded by their envelopes before validity checks and repairs.
    Only clipped Polygon pieces are reprojected and written. Attributes repeat
    on split pieces; row lineage is recorded in the sidecar report.
    """
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise PreparationError(f"Source file does not exist: {source}")
    if output.suffix != ".geojson":
        raise PreparationError("Output must have the .geojson suffix for EDDIE ingestion.")
    report_path = output.with_suffix(".report.json")
    if output.exists() or report_path.exists():
        raise PreparationError("Output or report already exists; choose a new output filename.")
    if isinstance(max_features, bool) or not isinstance(max_features, int) or max_features < 1:
        raise PreparationError("max_features must be a positive integer.")
    for name, value in (("max_output_mb", max_output_mb), ("max_feature_mb", max_feature_mb)):
        if not math.isfinite(value) or value <= 0:
            raise PreparationError(f"{name} must be positive and finite.")
    if all_features and any(value is not None for value in (bbox, bbox_crs, aoi_path)):
        raise PreparationError("Full-source mode cannot be combined with an AOI or bbox CRS.")
    subset = None if all_features else make_subset(bbox=bbox, bbox_crs=bbox_crs, aoi_path=aoi_path)
    audit["selection"] = {"method": "clip polygon parts to AOI; split pieces retain source attributes",
                          "bbox": list(bbox) if bbox is not None else None,
                          "bbox_crs": str(bbox_crs) if bbox_crs is not None else None,
                          "aoi_path": str(Path(aoi_path).resolve()) if aoi_path else None}
    info = pyogrio.read_info(source, layer=layer)
    if info["crs"] is None or CRS.from_user_input(info["crs"]).to_epsg() is None:
        raise PreparationError("Source layer requires an EPSG-resolvable CRS; none will be guessed.")
    if category_field not in info["fields"]:
        raise PreparationError(f"Missing classification field: {category_field}")
    source_crs = CRS.from_user_input(info["crs"])
    bounds = None if all_features else Transformer.from_crs(subset.crs, source_crs, always_xy=True).transform_bounds(
        *subset.total_bounds, densify_pts=21
    )
    if bounds is not None and not all(math.isfinite(v) for v in bounds):
        raise PreparationError("Subset cannot be transformed to source CRS.")
    audit.update(source_layer=layer, source_crs=source_crs.to_string())
    progress("Reading spatial candidates (national multipart features may still be large)...")
    read_options = {"fids": source_fids} if source_fids is not None else {"bbox": bounds, "max_features": max_features + 1}
    if source_fids is not None and not all_features:
        raise PreparationError("FID batches require full-source mode.")
    candidates = pyogrio.read_dataframe(source, layer=layer, fid_as_index=True, **read_options)
    if len(candidates) > max_features:
        raise PreparationError(f"Subset exceeds the {max_features}-candidate limit; choose a smaller AOI.")
    audit["candidate_count"] = len(candidates)
    audit["source_candidates_with_z"] = int(candidates.geometry.has_z.sum())
    source_aoi = None if all_features else subset.to_crs(source_crs).geometry.union_all()
    # Also clip after reprojection: transformed straight edges are not identical
    # in different CRSs. This enforces the user's AOI in the exported CRS.
    output_aoi = None if all_features else subset.to_crs(4326).geometry.union_all()
    national_transformer = Transformer.from_crs(source_crs, 4326, always_xy=True) if all_features else None
    output_polygons, row_positions, lineage = [], [], []
    skipped_parts = 0
    for position, (fid, geometry) in enumerate(candidates.geometry.items()):
        if position % 10 == 0 or position == len(candidates) - 1:
            progress(f"Processing source {position + 1}/{len(candidates)} (FID {fid})...")
        if geometry is None or geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise PreparationError(f"Source FID {fid}: missing/empty/non-polygon geometry.")
        parts = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
        for part_index, part in enumerate(parts):
            if all_features and part_index % 1000 == 0:
                progress(f"  Processing polygon part {part_index + 1}...")
            identity = {"source_fid": int(fid), "source_part": part_index}
            if part.is_empty or not all(math.isfinite(v) for v in part.bounds):
                raise PreparationError(f"Source FID {fid}, part {part_index}: empty/non-finite geometry.")
            # Envelope filtering requires no topological operations on invalid rings.
            if not all_features and not box(*part.bounds).intersects(source_aoi.envelope):
                skipped_parts += 1
                continue
            fixed = part
            if not part.is_valid:
                reason = explain_validity(part)
                record = {**identity, "reason": reason, "before_type": part.geom_type,
                          "before_area_source_units_squared": float(part.area), "status": "pending"}
                audit["repair_records"].append(record)
                if not repair_invalid:
                    record["status"] = "rejected: repair not enabled"
                    raise PreparationError(f"FID {fid}, part {part_index}: {reason}. No repairs enabled; use --repair-invalid.")
                progress(f"  Repairing part {part_index}: {reason}")
                try:
                    fixed = make_valid(part)
                except Exception as error:
                    record.update(status="failed", error=str(error))
                    raise
                repaired_parts, remnants = polygon_parts(fixed)
                record.update(after_type=fixed.geom_type, polygon_parts=len(repaired_parts),
                              excluded_nonpolygon_components=remnants,
                              after_area_source_units_squared=float(fixed.area))
                if not repaired_parts or not fixed.is_valid:
                    record["status"] = "failed: no valid polygonal result"
                    raise PreparationError(f"FID {fid}, part {part_index}: repair has no valid polygonal result.")
                record["status"] = "repaired"
            else:
                repaired_parts = [fixed]
            for repaired_part in repaired_parts:
                clipped = repaired_part if all_features else repaired_part.intersection(source_aoi)
                pieces, remnants = polygon_parts(clipped)
                if remnants:
                    audit["geometry_events"].append({**identity, "stage": "source_crs_clip",
                                                     "excluded_nonpolygon_components": remnants})
                if not pieces:
                    continue
                transformed = ([transform(national_transformer.transform, piece) for piece in pieces]
                               if all_features else gpd.GeoSeries(pieces, crs=source_crs).to_crs(4326))
                for piece in transformed:
                    final = piece if all_features else piece.intersection(output_aoi)
                    final_parts, remnants = polygon_parts(final)
                    if remnants:
                        audit["geometry_events"].append({**identity, "stage": "output_crs_clip",
                                                         "excluded_nonpolygon_components": remnants})
                    for polygon in final_parts:
                        output_polygons.append(polygon)
                        row_positions.append(position)
                        lineage.append(identity.copy())
                        if len(output_polygons) > max_features:
                            raise PreparationError(f"Clipped output exceeds {max_features} pieces; reduce the AOI.")
    audit["parts_skipped_outside_aoi_bounds"] = skipped_parts
    if not output_polygons:
        raise PreparationError("Selected land use: no features with polygon area inside the AOI.")
    # Copy source rows in output order; FID remains in sidecar, not a new user attribute.
    result = candidates.iloc[row_positions].copy().reset_index(drop=True)
    result = result.drop(columns=result.geometry.name)
    audit["output_pieces_with_z_removed"] = sum(bool(polygon.has_z) for polygon in output_polygons)
    audit["output_dimension"] = "XY; terrain supplies display elevation"
    # Land-use overlays use horizontal coordinates; source Z is not the DEM.
    # GDAL interprets 3D WGS84 GeoJSON as EPSG:4979, so make the promised 2D
    # EPSG:4326 export explicit while leaving source geometry/attributes untouched.
    result = gpd.GeoDataFrame(result, geometry=[shapely.force_2d(p) for p in output_polygons], crs=4326)
    validate_polygons(result, "Clipped land use")
    if not all_features:
        check_containment(result, output_aoi)
    xmin, ymin, xmax, ymax = result.total_bounds
    if not (-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90):
        raise PreparationError("Output coordinates fall outside longitude/latitude bounds.")
    categories = result[category_field]
    if categories.notna().sum() == 0:
        raise PreparationError("All selected category values are missing.")
    nonnull = categories.dropna()
    if not nonnull.map(lambda value: isinstance(value, str)).all():
        raise PreparationError("Land-use category values must be strings or null.")
    counts = nonnull.value_counts().sort_index()
    report = {
        **audit, "output_crs": "EPSG:4326", "category_field": category_field,
        "feature_count": len(result), "source_features_in_output": len(set(row_positions)),
        "output_feature_lineage": lineage,
        "category_counts": {str(k): int(v) for k, v in counts.items()},
        "null_category_count": int(categories.isna().sum()),
        "blank_category_count": int(nonnull.str.strip().eq("").sum()),
        "attributes": [column for column in result.columns if column != result.geometry.name],
        "output_bounds": [float(v) for v in result.total_bounds],
        "aoi_bounds_epsg4326": list(output_aoi.bounds) if output_aoi is not None else None,
        "geometry_repairs": sum(record["status"] == "repaired" for record in audit["repair_records"]),
        "repair_method": "shapely.make_valid (linework)" if repair_invalid else None,
        "repair_scope": "polygon parts whose envelopes intersect source-CRS AOI envelope",
        "repair_area_note": "Source CRS squared units; invalid pre-repair areas are diagnostic only.",
        "validation_scope": "clipped subset only; remote polygon parts are not validated",
        "versions": {"geopandas": gpd.__version__, "pyogrio": pyogrio.__version__,
                     "shapely": shapely.__version__, "geos": shapely.geos_version_string},
    }
    if all_features:
        report["selection"]["method"] = "all source polygon parts; no spatial filtering or clipping"
        report["repair_scope"] = "all polygon parts in selected source records"
        report["validation_scope"] = "all selected source records; see national manifest for completeness"
    output.parent.mkdir(parents=True, exist_ok=True)
    progress(f"Validating export: {len(result)} polygon pieces...")
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        temporary = Path(staging) / output.name
        # GDAL's RFC7946 writer rounded boundary coordinates in testing even with
        # explicit coordinate precision. Serialize full-precision coordinates;
        # use GDAL independently to validate the resulting standard GeoJSON.
        result.geometry = result.geometry.map(lambda polygon: orient(polygon, sign=1.0))
        def encode_attribute(value):
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            raise TypeError(f"Unsupported GeoJSON attribute type: {type(value).__name__}")
        temporary.write_text(result.to_json(drop_id=True, ensure_ascii=False,
                                            allow_nan=False, default=encode_attribute))
        file_bytes = temporary.stat().st_size
        if file_bytes == 0 or file_bytes > max_output_mb * 1024**2:
            raise PreparationError(f"Output is {file_bytes} bytes; limit is {max_output_mb} MiB. Reduce the AOI.")
        features = json.loads(temporary.read_text())["features"]
        feature_bytes = max(len(json.dumps(feature, ensure_ascii=False).encode("utf-8")) for feature in features)
        if feature_bytes > max_feature_mb * 1024**2:
            raise PreparationError(f"A clipped feature is {feature_bytes} bytes; limit is {max_feature_mb} MiB. Reduce the AOI.")
        restored = gpd.read_file(temporary, engine="pyogrio")
        round_trip = {"expected_crs": "EPSG:4326", "observed_crs": str(restored.crs),
                      "expected_feature_count": len(result), "observed_feature_count": len(restored),
                      "observed_z_feature_count": int(restored.geometry.has_z.sum())}
        audit["round_trip"] = round_trip
        report["round_trip"] = round_trip
        if restored.crs is None or restored.crs.to_epsg() != 4326:
            raise PreparationError(f"Export CRS mismatch: expected EPSG:4326, observed {restored.crs}.")
        if len(restored) != len(result):
            raise PreparationError(f"Export feature count mismatch: expected {len(result)}, observed {len(restored)}.")
        if restored.geometry.has_z.any():
            raise PreparationError("Export dimension mismatch: expected XY, observed Z coordinates.")
        validate_polygons(restored, "Export round trip")
        if not all_features:
            check_containment(restored, output_aoi)
        expected_categories = [None if value is None or pd.isna(value) else value for value in categories]
        if [feature["properties"].get(category_field) for feature in features] != expected_categories:
            raise PreparationError("Export round trip changed category values.")
        if any(set(feature["properties"]) != set(report["attributes"]) for feature in features):
            raise PreparationError("Export round trip changed attribute names.")
        report.update(status="validated", file_size_bytes=file_bytes, largest_feature_bytes=feature_bytes,
                      size_limits_mib={"file": max_output_mb, "feature": max_feature_mb},
                      output_within_aoi=None if all_features else True, containment_tolerance_degrees=CONTAINMENT_TOLERANCE,
                      valid_geometry_count=int(restored.geometry.is_valid.sum()))
        temp_report = Path(staging) / report_path.name
        temp_report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        temporary.rename(output)
        temp_report.rename(report_path)
    progress(f"Validated GeoJSON: {output} ({file_bytes:,} bytes)")
    return report
