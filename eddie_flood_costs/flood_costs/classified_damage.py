"""General JRC damage: every building of a run, using the run's own classification.

damage_calculation.main() reproduces the validated reference run: it checks the
frozen review files, keeps the saved HQ10/HQ100 amounts of the original cohort
and only values HQextreme for newly approved buildings. It stays unchanged.

This module does the same maths for any AOI and any classification, without those
frozen checks. Inputs of one run:

    <run>/depth_reconstruction/  building_reconstructed_depths.geojson
                                 building_depth_samples.csv.gz
    <run>/classification/        building_classification.csv   (build_classification)
    JRC workbook                 (FLOOD_COSTS_INPUT_JRC_WORKBOOK)

It reuses read_jrc, fraction, build_weights and weighted_estimate unchanged and
writes the same products as the reference run, so cost_lookup.build_lookup and
load_flood_costs_to_db publish it without any change.

Rules
- Residential / Commercial / Industrial: valued for HQ10, HQ100 and HQextreme.
- Unknown classification: damage null, status unknown_classification, never EUR 0.
- In the depth stage but not in the classification table (outside the classified
  AOI): damage null, status excluded_outside_corridor.
- Incomplete weighted depth coverage: damage null, status unknown_depth_coverage.
- Shared footprint slivers are valued once (lexical building-ID order).
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import platform
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

from . import SCENARIOS, SCENARIO_PAIRS
from . import damage_calculation as dc

CATEGORIES = dc.CATEGORIES
METHOD_VERSION = "classified-damage-1"
DEPTH_FILES = ("building_reconstructed_depths.geojson", "building_depth_samples.csv.gz")
CLASSIFICATION_FILE = "building_classification.csv"
PRODUCTS = ("building_flood_damage.geojson", "building_flood_damage.csv", "damage_summary.csv",
            "damage_validation.json", "jrc_parameters.json", "sample_damage_weights.csv.gz")

# cost_lookup scopes; kept identical so the lookup validates the result.
CLASSIFIED = "classified_subset"
UNRESOLVED = "unresolved_in_corridor"
EXCLUDED = "excluded_outside_landuse_corridor"
SUMMARY_GROUPS = (*CATEGORIES, "Classified total", "Unresolved in corridor", "Excluded outside corridor",
                  "Pending classification review", "All buildings")
BASE_FLAGS = ("provisional_reconstructed_depths", "vertical_datum_unverified",
              "terrain_depth_used_without_finished_floor_levels")
SOURCE_LABELS = {"reviewed": "reviewed decision (classification_review)",
                 "landuse": "land use 2024 dominant code 111 (Residential proxy)",
                 "osm": "OpenStreetMap rules (Geofabrik free layers)",
                 "none": "no reliable evidence"}


# ---------------------------------------------------------------- inputs

def read_jrc(workbook) -> dict:
    """damage_calculation.read_jrc on an explicit workbook (it reads a module global)."""
    previous = getattr(dc, "WORKBOOK", None)
    dc.WORKBOOK = Path(workbook)
    try:
        return dc.read_jrc()
    finally:
        dc.WORKBOOK = previous


def _blank(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == ""


def load_classification(path) -> dict[str, dict]:
    """building_classification.csv -> {buildingid: row}. Categories outside the JRC three become Unknown."""
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in ("buildingid", "jrc_category"):
        if column not in table.columns:
            raise ValueError(f"{path} lacks column {column}")
    if table["buildingid"].duplicated().any():
        raise ValueError(f"Duplicate building IDs in {path}")
    return {row["buildingid"]: row for row in table.to_dict("records")}


def classification_fields(row: dict | None) -> dict:
    """Lookup-compatible classification fields for one building (cost_lookup._validate_record)."""
    if row is None:
        return {"jrc_category": None, "accepted_provisional_classification": False,
                "classification_scope": EXCLUDED, "classification_source": "not_in_classification_aoi",
                "classification_basis": None, "classification_confidence": None,
                "classification_review_decision": None, "classification_quality_flags": "not_in_classification_aoi",
                "matched_osm_feature_ids": None, "classification_osm_evidence_json": None}
    category = row.get("jrc_category")
    category = category if category in CATEGORIES else None
    source = row.get("classification_source") or "none"
    confidence = row.get("classification_confidence") or None
    if category is None:
        confidence = "unresolved"
    elif confidence not in ("high", "medium"):
        confidence = None          # "proxy" (land use) and blank reviewed values
    reason = row.get("classification_reason") or None
    return {"jrc_category": category, "accepted_provisional_classification": category is not None,
            "classification_scope": CLASSIFIED if category else UNRESOLVED,
            "classification_source": SOURCE_LABELS.get(source, source),
            "classification_basis": reason,
            "classification_confidence": confidence,
            "classification_review_decision": "reviewed" if source == "reviewed" else None,
            "classification_quality_flags": row.get("reason_code") or None,
            "matched_osm_feature_ids": row.get("osm_feature_ids") or None,
            "classification_osm_evidence_json": row.get("osm_evidence_json") or None}


# ---------------------------------------------------------------- calculation

def allocate_overlaps(geometries: dict, ids: list[str]) -> tuple[dict, dict]:
    """Give each shared sliver to the lexically first building; return allocation and removed area."""
    ordered = sorted(ids)
    geoms = np.array([geometries[b] for b in ordered], dtype=object)
    tree = shapely.STRtree(geoms) if len(geoms) else None
    allocated, removed = {}, {}
    for i, bid in enumerate(ordered):
        original = geometries[bid]
        previous = [int(j) for j in tree.query(original, predicate="intersects") if j < i]
        allocation = original.difference(shapely.union_all(geoms[previous])) if previous else original
        allocated[bid] = allocation
        removed[bid] = max(0.0, original.area - allocation.area)
    return allocated, removed


def _empty(status: str, flags: list[str]) -> dict:
    return {"eur2010": None, "fraction": None, "status": status, "positive_damage": None,
            "coverage_fraction": None, "valid_weighted_area_m2": None, "unknown_weighted_area_m2": None,
            "known_area_damage_component_eur2010": None, "known_fraction_component": None,
            "area_weighted_depth_m": None, "valid_dry_weighted_area_m2": None, "valid_wet_weighted_area_m2": None,
            "valid_interior_samples": None, "missing_interior_samples": None,
            "above_curve_maximum_sample_count": None, "quality_flags": "|".join(flags)}


def value_building(record, geometry, allocation, samples_by_scenario, parameter, removed_m2, audit=None):
    """All scenarios of one classified building; returns {scenario: values} and the valuation area."""
    first = samples_by_scenario[SCENARIOS[0]]
    for s in SCENARIOS[1:]:
        cols = ["sample_index", "part_index", "x_epsg2169", "y_epsg2169"]
        if not np.array_equal(first[cols].to_numpy(), samples_by_scenario[s][cols].to_numpy()):
            raise ValueError(f"Sample grids differ between scenarios: {record['buildingid']}")
    area = record["footprint_area_m2"]
    if allocation.is_empty or allocation.area <= 0:
        return {s: _empty("unknown_depth_coverage", [*BASE_FLAGS, "footprint_fully_shared_with_other_building"])
                for s in SCENARIOS}, 0.0, 0
    try:
        weights, interior, unsupported, valuation_area, _ = dc.build_weights(geometry, allocation, first, area)
    except (AssertionError, ValueError, shapely.errors.GEOSException):
        # One degenerate footprint must not stop the AOI; it stays unknown, never zero.
        return {s: _empty("unknown_depth_coverage", [*BASE_FLAGS, "sample_weighting_failed_geometry_review_needed"])
                for s in SCENARIOS}, None, 0
    tolerance = max(1e-6, valuation_area * 1e-9)
    maximum = parameter["maximum_damage_eur2010_per_m2"]
    out = {}
    for s in SCENARIOS:
        sg = samples_by_scenario[s]
        depths = sg.official_masked_depth_m.to_numpy(float)
        valid = sg.reconstruction_valid.to_numpy(dtype=bool) & np.isfinite(depths)
        est = dc.weighted_estimate(weights, valid, depths, unsupported, valuation_area, area, parameter)
        known, money, complete = est["known"], est["monetary"], est["complete"]
        high = (np.where(known, depths, 0) > parameter["depth_m"][-1]) & known
        flags = list(BASE_FLAGS)
        if not complete: flags.append("incomplete_weighted_depth_coverage_full_damage_null")
        if unsupported > tolerance: flags.append("footprint_part_without_interior_samples")
        if record.get(f"samples_missing_{s}", 0): flags.append("original_reconstruction_sample_coverage_incomplete")
        if removed_m2 > 0: flags.append("shared_footprint_overlap_allocated_once")
        if record["classification_source"] == SOURCE_LABELS["landuse"]:
            flags.append("residential_landuse_proxy_not_building_use_survey")
        elif record["classification_source"] == SOURCE_LABELS["osm"]:
            flags.append("osm_rule_classification_not_building_use_survey")
        if high.any(): flags.append("depth_above_6m_fraction_saturated_at_published_maximum")
        if record.get(f"exposure_{s}_code", 0) > 0 and complete and money == 0:
            flags.append("officially_exposed_but_no_positive_weighted_interior_depth")
            if record.get(f"samples_wet_{s}", 0) > 0:
                flags.append("positive_depth_only_in_excluded_boundary_samples")
        out[s] = {"eur2010": money, "fraction": est["full_fraction"],
                  "status": "calculated_positive" if money is not None and money > 0
                  else "calculated_zero" if money is not None else "unknown_depth_coverage",
                  "positive_damage": money > 0 if money is not None else None,
                  "coverage_fraction": est["known_area"] / valuation_area if valuation_area else None,
                  "valid_weighted_area_m2": est["known_area"], "unknown_weighted_area_m2": est["missing_area"],
                  "known_area_damage_component_eur2010": est["known_damage"] if est["known_area"] > 0 else None,
                  "known_fraction_component": est["fraction_component"] if est["known_area"] > 0 else None,
                  "area_weighted_depth_m": float(np.dot(weights[known], depths[known]) / valuation_area) if complete else None,
                  "valid_dry_weighted_area_m2": float(weights[known & (depths == 0)].sum()),
                  "valid_wet_weighted_area_m2": float(weights[known & (depths > 0)].sum()),
                  "valid_interior_samples": int((valid & interior).sum()),
                  "missing_interior_samples": int((~valid & interior).sum()),
                  "above_curve_maximum_sample_count": int(high.sum()), "quality_flags": "|".join(flags)}
        if audit is not None:
            for j in np.flatnonzero(interior):
                row = sg.iloc[j]
                audit.writerow(dc.clean({
                    "buildingid": record["buildingid"], "sample_index": int(row.sample_index),
                    "part_index": int(row.part_index), "x_epsg2169": row.x_epsg2169, "y_epsg2169": row.y_epsg2169,
                    "weight_m2": weights[j], "scenario": s, "reconstruction_valid": bool(valid[j]),
                    "official_masked_depth_m": depths[j], "jrc_damage_fraction": est["fractions"][j],
                    "damage_component_eur2010": float(weights[j] * maximum * est["fractions"][j]) if valid[j] else None}))
    return out, valuation_area, int(interior.sum())


def summarise(results: list[dict]) -> list[dict]:
    rows = []
    for s in SCENARIOS:
        for group_name in SUMMARY_GROUPS:
            group = [r for r in results if r["jrc_category"] == group_name] if group_name in CATEGORIES else \
                [r for r in results if r["jrc_category"]] if group_name == "Classified total" else \
                [r for r in results if r["classification_scope"] == UNRESOLVED] if group_name == "Unresolved in corridor" else \
                [r for r in results if r["classification_scope"] == EXCLUDED] if group_name == "Excluded outside corridor" else \
                [] if group_name == "Pending classification review" else results
            values = [r[f"damage_{s}_eur2010"] for r in group if r[f"damage_{s}_eur2010"] is not None]
            rows.append({"scenario": s.upper(), "group": group_name, "represented_buildings": len(group),
                         "officially_exposed_buildings": sum((r.get(f"exposure_{s}_code") or 0) > 0 for r in group),
                         "calculated_damage_buildings": len(values),
                         "positive_damage_buildings": sum(v > 0 for v in values),
                         "zero_damage_buildings": sum(v == 0 for v in values),
                         "unknown_damage_buildings": len(group) - len(values),
                         "known_estimates_sum_eur2010": math.fsum(values) if values or not group else None,
                         "total_is_complete_for_group": len(values) == len(group), "price_year": 2010,
                         "total_interpretation": "Sum of complete building estimates only; unknowns excluded, "
                                                 "not treated as zero. Groups overlap: do not sum total and breakdown rows."})
    return rows


# ---------------------------------------------------------------- run

def run(depth_dir, classification_csv, workbook, out_dir, write_audit: bool = True) -> dict:
    depth_dir, out_dir = Path(depth_dir), Path(out_dir)
    missing = [str(p) for p in (*(depth_dir / n for n in DEPTH_FILES), Path(classification_csv), Path(workbook))
               if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Classified damage inputs missing: {missing}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty; choose a new run_id")

    jrc = read_jrc(workbook)
    parameters = jrc["parameters"]
    method_checks = dc.numeric_method_checks(parameters)
    document = json.loads((depth_dir / DEPTH_FILES[0]).read_text(encoding="utf-8"))
    features = dc.unique_index([f["properties"] for f in document["features"]], "depth GeoJSON")
    classes = load_classification(classification_csv)
    samples = pd.read_csv(depth_dir / DEPTH_FILES[1], dtype={"buildingid": str, "scenario": str})
    if samples.duplicated(["buildingid", "scenario", "sample_index"]).any():
        raise ValueError("Duplicate depth samples")
    grouped = {key: g.sort_values("sample_index").reset_index(drop=True)
               for key, g in samples.groupby(["buildingid", "scenario"], sort=False)}

    import geopandas as gpd  # pylint: disable=import-outside-toplevel
    projected = gpd.GeoSeries([shape(f["geometry"]) for f in document["features"]], crs=4326).to_crs(2169)
    geometries = dict(zip(features, projected.make_valid() if hasattr(projected, "make_valid") else projected))

    records = {bid: {**props, **classification_fields(classes.get(bid))} for bid, props in features.items()}
    classified = [bid for bid, r in records.items() if r["jrc_category"]]
    allocated, removed_projected = allocate_overlaps(geometries, classified)

    out_dir.mkdir(parents=True, exist_ok=True)
    audit_fields = ["buildingid", "sample_index", "part_index", "x_epsg2169", "y_epsg2169", "weight_m2", "scenario",
                    "reconstruction_valid", "official_masked_depth_m", "jrc_damage_fraction", "damage_component_eur2010"]
    results, overlaps = [], 0
    audit_handle = gzip.open(out_dir / "sample_damage_weights.csv.gz", "wt", encoding="utf-8", newline="") \
        if write_audit else None
    try:
        audit = csv.DictWriter(audit_handle, fieldnames=audit_fields) if audit_handle else None
        if audit: audit.writeheader()
        for bid in sorted(records):
            record = records[bid]
            category = record["jrc_category"]
            record.update({"damage_price_year": 2010, "damage_currency": "EUR", "damage_estimates_provisional": True,
                           "maximum_damage_eur2010_per_m2": parameters[category]["maximum_damage_eur2010_per_m2"] if category else None,
                           "damage_valuation_scope": "JRC structure and standard category contents; no additional adjustments" if category else None,
                           "monetary_method": "interior-sample clipped Voronoi quadrature; full estimate only with complete weighted coverage",
                           "valuation_area_m2": None, "interior_samples_for_weighting": None,
                           "shared_overlap_removed_from_valuation_m2": None})
            if category:
                by_scenario = {s: grouped.get((bid, s)) for s in SCENARIOS}
                if any(v is None for v in by_scenario.values()):
                    values = {s: _empty("unknown_depth_coverage", [*BASE_FLAGS, "no_depth_samples"]) for s in SCENARIOS}
                    area, n = None, 0
                else:
                    scale = record["footprint_area_m2"] / geometries[bid].area
                    removed_m2 = removed_projected[bid] * scale
                    overlaps += removed_m2 > 0
                    record["shared_overlap_removed_from_valuation_m2"] = removed_m2
                    values, area, n = value_building(record, geometries[bid], allocated[bid], by_scenario,
                                                     parameters[category], removed_m2, audit)
                record.update(valuation_area_m2=area, interior_samples_for_weighting=n)
            else:
                status = "unknown_classification" if record["classification_scope"] == UNRESOLVED else "excluded_outside_corridor"
                flag = "classification_unknown_no_zero_loss_imputed" if status == "unknown_classification" \
                    else "outside_classified_aoi_no_zero_loss_imputed"
                values = {s: _empty(status, [*BASE_FLAGS, flag]) for s in SCENARIOS}
            for s, v in values.items():
                record.update({f"damage_{s}_{k}": val for k, val in v.items()})
            for low, high in SCENARIO_PAIRS:
                a, b = record[f"damage_{low}_eur2010"], record[f"damage_{high}_eur2010"]
                record[f"damage_{high}_below_{low}"] = b + 1e-6 < a if a is not None and b is not None else None
            results.append(record)
    finally:
        if audit_handle: audit_handle.close()

    # Same invariants as the reference run.
    for r in results:
        for s in SCENARIOS:
            d, f = r[f"damage_{s}_eur2010"], r[f"damage_{s}_fraction"]
            assert d is None or d >= 0
            assert f is None or 0 <= f <= 1
            if not r["jrc_category"]:
                assert d is None
            if d is not None:
                assert abs(d - r["footprint_area_m2"] * r["maximum_damage_eur2010_per_m2"] * f) < 1e-6

    summaries = summarise(results)
    features_out = [{"type": "Feature", "id": f["properties"]["buildingid"], "geometry": f["geometry"],
                     "properties": dc.clean(records[f["properties"]["buildingid"]])} for f in document["features"]]
    (out_dir / "building_flood_damage.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "name": "classified_building_flood_damage", "features": features_out},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    dc.write_csv(out_dir / "building_flood_damage.csv", results)
    dc.write_csv(out_dir / "damage_summary.csv", summaries)
    dc.write_json(out_dir / "jrc_parameters.json", jrc)

    counts = {"represented_buildings": len(results),
              "accepted_provisional_classifications": len(classified),
              "accepted_category_counts": dict(Counter(r["jrc_category"] for r in results if r["jrc_category"])),
              "unresolved_in_corridor": sum(r["classification_scope"] == UNRESOLVED for r in results),
              "outside_corridor_excluded": sum(r["classification_scope"] == EXCLUDED for r in results),
              "pending_classification_review": 0,
              "classification_sources": dict(Counter(r["classification_source"] for r in results)),
              "shared_footprint_overlaps": int(overlaps)}
    totals = {r["scenario"]: r["known_estimates_sum_eur2010"] for r in summaries if r["group"] == "Classified total"}
    validation = {
        "status": "completed_classified_building_damage", "method_version": METHOD_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(), "currency": "EUR", "price_year": 2010,
        "inputs": {"depth_dir": str(depth_dir), "classification_csv": str(classification_csv), "workbook": str(workbook)},
        "counts": counts, "classified_totals_eur2010": totals, "jrc_parameters": jrc,
        "scenario_and_category_summaries": summaries, "numeric_method_checks": method_checks,
        "limitations": [
            "PROVISIONAL reconstructed-depth estimates; not complete flood losses.",
            "Classifications come from reviewed decisions, land-use proxies and OSM rules, not a building-use survey.",
            "Unknown classifications and incomplete depth coverage stay null, never EUR 0.",
            "2010 euros, JRC structure + standard contents; no inflation, indirect or infrastructure losses."],
        "software": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
                     "shapely": shapely.__version__}}
    dc.write_json(out_dir / "damage_validation.json", validation)

    # Self-check: the published lookup must accept these files.
    from .cost_lookup import build_lookup  # pylint: disable=import-outside-toplevel
    lookup = build_lookup(out_dir)
    return {"monetary": str(out_dir), "dataset_version": lookup["dataset_version"],
            "counts": {k: counts[k] for k in ("represented_buildings", "accepted_provisional_classifications",
                                              "accepted_category_counts", "unresolved_in_corridor",
                                              "outside_corridor_excluded")},
            "classified_totals_eur2010": totals}
