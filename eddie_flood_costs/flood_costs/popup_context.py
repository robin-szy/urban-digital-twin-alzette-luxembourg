"""Read-only presentation context; never reconstruct depths or calculate damage."""
import hashlib
import json
import math
from pathlib import Path

from .cost_lookup import LookupValidationError, SCENARIOS, _record


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        return None
    return value


class PopupDetails:
    """Index the original monetary GeoJSON, retaining no geometry in the response.

    Its bytes must match the GeoJSON SHA-256 recorded by the lookup builder before
    area/depth/exposure fields can supplement that lookup. This prevents joining
    costs to a different run's context, including multipart display areas.
    """

    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw)
            if document.get("type") != "FeatureCollection":
                raise ValueError("Expected a monetary FeatureCollection.")
            self.records = {}
            for feature in document["features"]:
                properties = feature["properties"]
                bid = properties["buildingid"]
                if not isinstance(bid, str) or not bid or bid in self.records:
                    raise ValueError("Missing or duplicate original building ID.")
                self.records[bid] = properties
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LookupValidationError("Invalid popup context data.") from exc

    def for_building(self, lookup, buildingid):
        document = lookup.to_dict()
        sources = document.get("metadata", {}).get("source_sha256")
        expected = sources.get("building_flood_damage.geojson") if isinstance(sources, dict) else None
        if expected != self.sha256:
            raise LookupValidationError("Popup context does not match lookup provenance.")
        properties = self.records.get(buildingid)
        if properties is None:
            raise LookupValidationError("Original building ID is absent from popup context.")
        if _record(properties, lookup.scenarios) != document["buildings"].get(buildingid):
            raise LookupValidationError("Popup classification/damage disagrees with lookup.")
        return properties


class DatabaseDetails:
    """Whole-building context from the published table flood_cost_buildings (tasks.load_flood_costs_to_db).

    Used when no details file is configured, so every published run shows area, depth and
    exposure without a run-specific path. A row is used only if its classification and
    damage equal the lookup's record for that building (the same check as PopupDetails),
    so context from another run or reach is never joined to these costs.
    """

    TABLE = "flood_cost_buildings"

    def __init__(self, engine, run_id=None):
        self.engine = engine
        self.run_id = run_id

    def for_building(self, lookup, buildingid):
        from sqlalchemy import text  # pylint: disable=import-outside-toplevel
        sql = f'SELECT * FROM "{self.TABLE}" WHERE buildingid = :bid'
        parameters = {"bid": buildingid}
        if self.run_id:
            sql += " AND run_id = :run"
            parameters["run"] = self.run_id
        try:
            with self.engine.connect() as connection:
                rows = [dict(row) for row in connection.execute(text(sql), parameters).mappings()]
        except Exception as exc:  # table missing or database unavailable
            raise LookupValidationError("Popup context table unavailable.") from exc
        for row in rows:  # some databases (SQLite) return booleans as 0/1
            for key in ("accepted_provisional_classification", "damage_estimates_provisional"):
                if type(row.get(key)) is int and row[key] in (0, 1):
                    row[key] = bool(row[key])
        expected = lookup.to_dict()["buildings"].get(buildingid)
        matches = []
        for row in rows:
            try:
                if _record(row, lookup.scenarios) == expected:
                    matches.append(row)
            except (LookupValidationError, KeyError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise LookupValidationError("No single published context row matches the lookup for this building.")
        properties = matches[0]
        properties.pop("geometry", None)
        return properties


def build_popup_payload(lookup, buildingid, details=None, context_warning=None):
    """Join by the exact original ID; unknown amounts stay null, including 404s."""
    costs = {scenario: lookup.building_cost(buildingid, scenario) for scenario in lookup.scenarios}
    first = costs["hq10"]
    properties = {}
    warnings = []
    if not first["found"]:
        warnings.append("No flood-cost record matches this building.")
    elif details is not None:
        try:
            properties = details.for_building(lookup, buildingid)
        except LookupValidationError:
            context_warning = "Whole-building area, depth and exposure could not be verified against the cost dataset."
    if context_warning:
        warnings.append(context_warning)
    category = first["jrc_category"] if first["accepted_provisional_classification"] else None
    result = {
        "status": "success" if first["found"] else "not_found",
        "buildingid": buildingid, "found": first["found"],
        "category": category or "Unknown", "currency": "EUR", "price_year": 2010,
        "provisional": True, "dataset_version": first["dataset_version"],
        "schema_version": first["schema_version"], "available_scenarios": list(lookup.scenarios),
        "footprint_area_m2": _number(properties.get("footprint_area_m2"), positive=True),
        "context_available": bool(properties), "warnings": warnings,
    }
    for scenario, cost in costs.items():
        result[scenario] = {
            "damage_eur2010": cost["damage_eur2010"], "monetary_status": cost["monetary_status"],
            "quality_flags": cost["quality_flags"],
            "depth_mean_m": _number(properties.get(f"depth_{scenario}_mean_m")),
            "exposure_status": properties.get(f"exposure_{scenario}_status"),
        }
    return result
