"""Read-only EDDIE Flask blueprint. No data is loaded during plugin discovery."""
from functools import lru_cache
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from .cost_lookup import CostLookup, InvalidScenario, LookupValidationError, normalize_scenario

# EDDIE imports <plugin>.blueprint and registers this exact module-level name.
blueprint = Blueprint("eddie_flood_costs", __name__, url_prefix="/flood-costs")


@lru_cache(maxsize=4)
def _load_snapshot(path: str, mtime_ns: int, size: int) -> CostLookup:
    return CostLookup.load(path)


def _lookup() -> CostLookup:
    configured = current_app.config.get("FLOOD_COSTS_LOOKUP_PATH") or os.environ.get("FLOOD_COSTS_LOOKUP_PATH")
    if not configured:
        raise LookupValidationError("FLOOD_COSTS_LOOKUP_PATH is not configured.")
    path = Path(configured).expanduser().resolve()
    stat = path.stat()
    return _load_snapshot(str(path), stat.st_mtime_ns, stat.st_size)


def _unavailable(exc):
    current_app.logger.warning("Flood-cost lookup unavailable: %s", exc)
    return jsonify({"status": "unavailable", "damage_eur2010": None,
                    "message": "Flood-cost data unavailable; check FLOOD_COSTS_LOOKUP_PATH and lookup validation."}), 503


@blueprint.route("/summary/<scenario>.json", methods=["GET"])
def scenario_summary(scenario):
    """Validated classified-subset sums; never sum unresolved or excluded buildings."""
    try:
        scenario = normalize_scenario(scenario)
        result = _lookup().summary(scenario)
    except InvalidScenario as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except (OSError, LookupValidationError) as exc:
        return _unavailable(exc)
    return jsonify({"status": "success", **result})


@blueprint.route("/buildings/<buildingid>/<scenario>.json", methods=["GET"])
def building_cost(buildingid, scenario):
    """Known unknown costs return 200/null; absent IDs return 404/null."""
    try:
        scenario = normalize_scenario(scenario)
        result = _lookup().building_cost(buildingid, scenario)
    except InvalidScenario as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except (OSError, LookupValidationError) as exc:
        return _unavailable(exc)
    return jsonify({"status": "success" if result["found"] else "not_found", **result}), 200 if result["found"] else 404

