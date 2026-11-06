"""Versioned geometry-free cost lookup, source reconciliation, and local builder.

This module uses only the Python standard library. It does not calculate flood
depths or monetary damages, connect to EDDIE, or change source result files.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
SCENARIOS = ("hq10", "hq100")
CATEGORIES = ("Residential", "Commercial", "Industrial")
SOURCE_FILES = (
    "building_flood_damage.csv", "building_flood_damage.geojson",
    "damage_summary.csv", "damage_validation.json",
)
RECONCILIATION_TOLERANCE_EUR = 1e-6
CLASSIFIED = "classified_subset"
UNRESOLVED = "unresolved_in_corridor"
EXCLUDED = "excluded_outside_landuse_corridor"
KNOWN_STATUSES = {"calculated_positive", "calculated_zero"}
UNKNOWN_STATUSES = {"unknown_classification", "excluded_outside_corridor", "unknown_depth_coverage"}


class LookupValidationError(ValueError):
    """Input data violates the lookup schema or source reconciliation rules."""


class InvalidScenario(ValueError):
    """Only precomputed HQ10 and HQ100 estimates are supported."""


def normalize_scenario(scenario: str) -> str:
    if not isinstance(scenario, str) or scenario.strip().lower() not in SCENARIOS:
        raise InvalidScenario("Use hq10 or hq100; manual levels and HQ Extreme are not supported.")
    return scenario.strip().lower()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LookupValidationError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate JSON key/building ID: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LookupValidationError(f"Non-finite JSON constant: {value}")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise LookupValidationError("Invalid lookup/source JSON.") from exc
    _require(isinstance(value, dict), "JSON root must be an object.")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        _require(bool(reader.fieldnames), f"Missing CSV header: {path.name}")
        _require(len(reader.fieldnames) == len(set(reader.fieldnames)), f"Duplicate CSV columns: {path.name}")
        rows = list(reader)
    _require(all(None not in row and all(v is not None for v in row.values()) for row in rows),
             f"Malformed CSV rows: {path.name}")
    return rows


def _index(rows: list[dict], label: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        bid = row.get("buildingid")
        _require(isinstance(bid, str) and bool(bid.strip()) and bid == bid.strip(),
                 f"Missing/invalid building ID in {label}.")
        _require(bid not in result, f"Duplicate building ID in {label}: {bid}")
        result[bid] = row
    return result


def _amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    _require(not isinstance(value, bool), "A boolean is not a monetary amount.")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise LookupValidationError("Invalid monetary amount.") from exc
    _require(math.isfinite(number) and number >= 0, "Monetary amounts must be finite and nonnegative.")
    return number


def _same_amount(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=0, abs_tol=RECONCILIATION_TOLERANCE_EUR)


def _flags(value: str | None) -> list[str]:
    return value.split("|") if value else []


def _boolean(value: str | bool) -> bool:
    if value is True or value == "True":
        return True
    if value is False or value == "False":
        return False
    raise LookupValidationError("Classification acceptance must be True or False.")


def _record(row: dict) -> dict:
    required = ("jrc_category", "accepted_provisional_classification", "classification_scope",
                "classification_confidence", "classification_source", "classification_basis",
                "classification_review_decision")
    _require(all(key in row for key in required), "Missing classification fields in source results.")
    _require(str(row.get("damage_price_year")) == "2010" and row.get("damage_currency") == "EUR"
             and _boolean(row.get("damage_estimates_provisional")) is True,
             "Each source record must contain provisional 2010 EUR estimates.")
    result = {
        "jrc_category": row["jrc_category"] or None,
        "accepted_provisional_classification": _boolean(row["accepted_provisional_classification"]),
        "classification_scope": row["classification_scope"],
        "classification_confidence": row["classification_confidence"] or None,
        "classification_source": row["classification_source"],
        "classification_basis": row["classification_basis"] or None,
        "classification_review_decision": row["classification_review_decision"] or None,
    }
    for scenario in SCENARIOS:
        prefix = f"damage_{scenario}_"
        _require(all(prefix + suffix in row for suffix in ("eur2010", "status", "quality_flags")),
                 "Missing scenario fields in source results.")
        result[scenario] = {
            "damage_eur2010": _amount(row[prefix + "eur2010"]),
            "status": row[prefix + "status"],
            "quality_flags": _flags(row[prefix + "quality_flags"]),
        }
    return result


def _group_summary(records: list[dict], scenario: str) -> dict:
    amounts = [r[scenario]["damage_eur2010"] for r in records if r[scenario]["damage_eur2010"] is not None]
    accepted = [r for r in records if r["accepted_provisional_classification"]]
    known = [r[scenario]["damage_eur2010"] for r in accepted if r[scenario]["damage_eur2010"] is not None]
    return {
        "represented_buildings": len(records),
        "accepted_provisional_classifications": len(accepted),
        "calculated_damage_buildings": len(amounts),
        "positive_damage_buildings": sum(a > 0 for a in amounts),
        "zero_damage_buildings": sum(a == 0 for a in amounts),
        "unknown_damage_buildings": len(records) - len(amounts),
        "classified_unknown_damage_buildings": len(accepted) - len(known),
        "total_eur2010": math.fsum(known) if known or not accepted else None,
        "complete_for_classified_subset": len(known) == len(accepted),
    }


def _totals(buildings: dict) -> dict:
    records = list(buildings.values())
    result = {}
    for scenario in SCENARIOS:
        summary = _group_summary(records, scenario)
        summary.update({
            "unresolved_classifications": sum(r["classification_scope"] == UNRESOLVED for r in records),
            "outside_corridor_excluded": sum(r["classification_scope"] == EXCLUDED for r in records),
            "by_category": {category: _group_summary([r for r in records if r["jrc_category"] == category], scenario)
                            for category in CATEGORIES},
        })
        result[scenario] = summary
    return result


def _validate_record(bid: str, record: Any) -> None:
    _require(isinstance(record, dict), f"Building record must be an object: {bid}")
    required = {"jrc_category", "accepted_provisional_classification", "classification_scope",
                "classification_confidence", "classification_source", "classification_basis",
                "classification_review_decision", *SCENARIOS}
    _require(set(record) == required, f"Unexpected/missing building fields: {bid}")
    accepted = record["accepted_provisional_classification"]
    _require(isinstance(accepted, bool), f"Invalid acceptance flag: {bid}")
    _require(record["classification_scope"] in (CLASSIFIED, UNRESOLVED, EXCLUDED), f"Invalid scope: {bid}")
    _require((accepted and record["jrc_category"] in CATEGORIES and record["classification_scope"] == CLASSIFIED)
             or (not accepted and record["jrc_category"] is None and record["classification_scope"] != CLASSIFIED),
             f"Category, scope and acceptance disagree: {bid}")
    _require(record["classification_confidence"] in (None, "high", "medium", "unresolved"), f"Invalid confidence: {bid}")
    _require(isinstance(record["classification_source"], str) and bool(record["classification_source"]),
             f"Missing classification source: {bid}")
    for key in ("classification_basis", "classification_review_decision"):
        _require(record[key] is None or isinstance(record[key], str), f"Invalid classification metadata: {bid}")
    for scenario in SCENARIOS:
        cost = record[scenario]
        _require(isinstance(cost, dict) and set(cost) == {"damage_eur2010", "status", "quality_flags"},
                 f"Invalid scenario schema: {bid}/{scenario}")
        amount = cost["damage_eur2010"]
        _require(amount is None or (isinstance(amount, (int, float)) and not isinstance(amount, bool)),
                 f"Monetary values must be numbers or null: {bid}/{scenario}")
        _amount(amount)
        _require(isinstance(cost["quality_flags"], list)
                 and all(isinstance(flag, str) and flag for flag in cost["quality_flags"]), f"Invalid flags: {bid}")
        status = cost["status"]
        _require(isinstance(status, str) and status in KNOWN_STATUSES | UNKNOWN_STATUSES, f"Invalid cost status: {bid}")
        if status in KNOWN_STATUSES:
            _require(accepted and amount is not None, f"Known money requires accepted classification: {bid}")
            _require((status == "calculated_zero" and amount == 0)
                     or (status == "calculated_positive" and amount > 0), f"Status/amount mismatch: {bid}")
        else:
            _require(amount is None, f"Unknown damage must be null, never zero: {bid}")
            expected = "unknown_depth_coverage" if accepted else "unknown_classification" if record["classification_scope"] == UNRESOLVED else "excluded_outside_corridor"
            _require(status == expected, f"Scope/status mismatch: {bid}")


def _validate_summary(stored: Any, expected: dict, label: str) -> None:
    _require(isinstance(stored, dict) and set(stored) == set(expected), f"Invalid summary schema: {label}")
    for key, value in expected.items():
        actual = stored[key]
        if isinstance(value, dict):
            _validate_summary(actual, value, f"{label}/{key}")
        elif key == "total_eur2010":
            _require(actual is None or (isinstance(actual, (int, float)) and not isinstance(actual, bool)),
                     f"Invalid summary money: {label}")
            _require(_same_amount(_amount(actual), value), f"Summary total does not reconcile: {label}")
        else:
            _require(type(actual) is type(value) and actual == value, f"Summary count/flag mismatch: {label}/{key}")


class CostLookup:
    """Validated immutable-by-interface lookup. Unknown IDs and costs return null."""

    def __init__(self, document: dict):
        _require(isinstance(document, dict), "Lookup must be an object.")
        _require(document.get("schema_version") == SCHEMA_VERSION, "Unsupported lookup schema version.")
        _require(isinstance(document.get("dataset_version"), str) and bool(document["dataset_version"]), "Missing dataset version.")
        metadata = document.get("metadata", {})
        _require(isinstance(metadata, dict) and metadata.get("currency") == "EUR"
                 and metadata.get("price_year") == 2010 and metadata.get("provisional") is True,
                 "Lookup must contain provisional 2010 EUR estimates.")
        buildings = document.get("buildings")
        _require(isinstance(buildings, dict) and bool(buildings), "Lookup requires buildings keyed by original ID.")
        for bid, record in buildings.items():
            _require(isinstance(bid, str) and bool(bid.strip()) and bid == bid.strip(), "Invalid building ID.")
            _validate_record(bid, record)
        _validate_summary(document.get("summaries"), _totals(buildings), "summaries")
        self._document = copy.deepcopy(document)

    @classmethod
    def load(cls, path: str | Path) -> CostLookup:
        return cls(_read_json(Path(path)))

    def to_dict(self) -> dict:
        return copy.deepcopy(self._document)

    def _metadata(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "dataset_version": self._document["dataset_version"],
                "currency": "EUR", "price_year": 2010, "provisional": True,
                "scope": "classified_subset; not complete AOI flood losses"}

    def building_cost(self, buildingid: str, scenario: str) -> dict:
        scenario = normalize_scenario(scenario)
        _require(isinstance(buildingid, str) and bool(buildingid.strip()), "A building ID is required.")
        record = self._document["buildings"].get(buildingid)
        result = {**self._metadata(), "buildingid": buildingid, "scenario": scenario, "found": record is not None}
        if record is None:
            result.update({"damage_eur2010": None, "monetary_status": "missing_building", "quality_flags": [],
                           "jrc_category": None, "classification_confidence": None,
                           "accepted_provisional_classification": False, "classification_scope": "not_in_cost_inventory",
                           "classification_source": None, "classification_review_decision": None})
        else:
            result.update({key: copy.deepcopy(value) for key, value in record.items() if key not in SCENARIOS})
            result.update({"damage_eur2010": record[scenario]["damage_eur2010"],
                           "monetary_status": record[scenario]["status"],
                           "quality_flags": list(record[scenario]["quality_flags"])})
        return result

    def damage(self, buildingid: str, scenario: str) -> float | None:
        return self.building_cost(buildingid, scenario)["damage_eur2010"]

    def summary(self, scenario: str) -> dict:
        scenario = normalize_scenario(scenario)
        return {**self._metadata(), "scenario": scenario, **copy.deepcopy(self._document["summaries"][scenario])}


def build_lookup(source_dir: str | Path) -> dict:
    """Reconcile all four saved results, then derive a compact lookup without geometry."""
    source_dir = Path(source_dir)
    paths = {name: source_dir / name for name in SOURCE_FILES}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    rows = _index(_read_csv(paths["building_flood_damage.csv"]), "building damage CSV")
    geo = _read_json(paths["building_flood_damage.geojson"])
    _require(geo.get("type") == "FeatureCollection" and isinstance(geo.get("features"), list), "Invalid source GeoJSON.")
    _require(all(isinstance(f, dict) and isinstance(f.get("properties"), dict) for f in geo["features"]), "Missing GeoJSON properties.")
    geo_rows = _index([feature["properties"] for feature in geo["features"]], "building damage GeoJSON")
    _require(set(rows) == set(geo_rows), "CSV/GeoJSON building ID sets disagree.")
    buildings = {}
    for bid, row in rows.items():
        record = _record(row)
        counterpart = _record(geo_rows[bid])
        _validate_record(bid, record)
        for key in record:
            if key in SCENARIOS:
                _require(_same_amount(record[key]["damage_eur2010"], counterpart[key]["damage_eur2010"])
                         and record[key]["status"] == counterpart[key]["status"]
                         and record[key]["quality_flags"] == counterpart[key]["quality_flags"],
                         f"CSV/GeoJSON scenario mismatch: {bid}/{key}")
            else:
                _require(record[key] == counterpart[key], f"CSV/GeoJSON classification mismatch: {bid}/{key}")
        buildings[bid] = record
    totals = _totals(buildings)
    validation = _read_json(paths["damage_validation.json"])
    counts = validation.get("counts", {})
    _require(isinstance(counts, dict), "Invalid validation-report counts.")
    records = list(buildings.values())
    expected_counts = {
        "represented_buildings": len(records),
        "accepted_provisional_classifications": sum(r["accepted_provisional_classification"] for r in records),
        "unresolved_in_corridor": sum(r["classification_scope"] == UNRESOLVED for r in records),
        "outside_corridor_excluded": sum(r["classification_scope"] == EXCLUDED for r in records),
        "accepted_category_counts": {c: sum(r["jrc_category"] == c for r in records) for c in CATEGORIES
                                     if any(r["jrc_category"] == c for r in records)},
    }
    for key, value in expected_counts.items():
        _require(counts.get(key) == value, f"Validation report count mismatch: {key}")
    _require(validation.get("currency") == "EUR" and validation.get("price_year") == 2010, "Source valuation units disagree.")
    saved = {}
    for row in _read_csv(paths["damage_summary.csv"]):
        _require(all(k in row for k in ("scenario", "group", "known_estimates_sum_eur2010")),
                 "Missing saved-summary columns.")
        pair = (normalize_scenario(row["scenario"]), row["group"])
        _require(pair not in saved, f"Duplicate saved summary: {pair}")
        saved[pair] = row
    for scenario in SCENARIOS:
        groups = {"Classified total": [r for r in records if r["accepted_provisional_classification"]],
                  **{c: [r for r in records if r["jrc_category"] == c] for c in CATEGORIES},
                  "Unresolved in corridor": [r for r in records if r["classification_scope"] == UNRESOLVED],
                  "Excluded outside corridor": [r for r in records if r["classification_scope"] == EXCLUDED],
                  "All buildings": records}
        for name, group in groups.items():
            _require((scenario, name) in saved, f"Missing saved summary: {scenario}/{name}")
            row = saved[(scenario, name)]
            computed = _group_summary(group, scenario)
            for key in ("represented_buildings", "calculated_damage_buildings", "positive_damage_buildings",
                        "zero_damage_buildings", "unknown_damage_buildings"):
                _require(row.get(key) == str(computed[key]), f"Saved summary count mismatch: {scenario}/{name}/{key}")
            # Unknown-only groups have no monetary total; never reinterpret their null as zero.
            known_values = [r[scenario]["damage_eur2010"] for r in group if r[scenario]["damage_eur2010"] is not None]
            expected = math.fsum(known_values) if known_values or not group else None
            _require(_same_amount(_amount(row["known_estimates_sum_eur2010"]), expected),
                     f"Saved summary total mismatch: {scenario}/{name}")
    document = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": "sha256:" + hashes["building_flood_damage.csv"],
        "metadata": {"currency": "EUR", "price_year": 2010, "provisional": True,
                     "vertical_datum_verified": False, "scope": "classified_subset; not complete AOI flood losses",
                     "source_calculated_utc": validation.get("created_utc"),
                     "source_sha256": hashes,
                     "note": "Saved structure-and-contents estimates, unchanged. Null is unknown; zero is a valid estimate."},
        "buildings": dict(sorted(buildings.items())), "summaries": totals,
    }
    CostLookup(document)
    _require(all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name] for name, path in paths.items()),
             "A source file changed during lookup generation.")
    return document


def write_lookup(source_dir: str | Path, output: str | Path) -> Path:
    """Build a new external artifact; never overwrite or write into a Git checkout."""
    source_dir, output = Path(source_dir).resolve(), Path(output).resolve()
    _require(not output.is_relative_to(source_dir), "Keep the lookup separate from the original monetary result files.")
    _require(not any((parent / ".git").exists() for parent in output.parents), "Generated lookup must remain outside Git checkouts.")
    if output.exists():
        raise FileExistsError(f"Lookup already exists; refusing overwrite: {output}")
    document = build_lookup(source_dir)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
    CostLookup.load(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an external versioned lookup from saved flood-cost results.")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    path = write_lookup(args.source_dir, args.output)
    lookup = CostLookup.load(path)
    print(json.dumps({"output": str(path), "bytes": path.stat().st_size,
                      "summaries": {s: lookup.summary(s) for s in SCENARIOS}}, indent=2))


if __name__ == "__main__":
    main()

