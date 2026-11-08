"""Read-only EDDIE Flask blueprint. No data is loaded during plugin discovery."""
from functools import lru_cache
import json
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from .cost_lookup import CostLookup, InvalidScenario, LookupValidationError, normalize_scenario

# EDDIE imports <plugin>.blueprint and registers this exact module-level name.
blueprint = Blueprint("eddie_flood_costs", __name__, url_prefix="/flood-costs")


@lru_cache(maxsize=4)
def _load_snapshot(path: str, mtime_ns: int, size: int) -> CostLookup:
    return CostLookup.load(path)


@lru_cache(maxsize=4)
def _load_db_snapshot(dataset_version: str) -> CostLookup:
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel
    with _engine().connect() as conn:
        document = conn.execute(text("SELECT lookup FROM flood_cost_lookup WHERE dataset_version = :v"),
                                {"v": dataset_version}).scalar_one()
    return CostLookup(json.loads(document))


@lru_cache(maxsize=1)
def _engine():
    from eddie.digitaltwin.setup_environment import get_database  # pylint: disable=import-outside-toplevel
    return get_database()


def _lookup_from_database() -> CostLookup:
    """The result loaded by tasks.load_flood_costs_to_db, used when no lookup file is configured."""
    try:
        from sqlalchemy import text  # pylint: disable=import-outside-toplevel
        with _engine().connect() as conn:
            version = conn.execute(text("SELECT dataset_version FROM flood_cost_lookup")).scalar_one()
        return _load_db_snapshot(version)
    except LookupValidationError:
        raise
    except Exception as exc:  # outside EDDIE, database down, or nothing loaded yet
        raise LookupValidationError("FLOOD_COSTS_LOOKUP_PATH is not configured and no result is loaded "
                                    "into the database.") from exc


def _lookup() -> CostLookup:
    configured = current_app.config.get("FLOOD_COSTS_LOOKUP_PATH") or os.environ.get("FLOOD_COSTS_LOOKUP_PATH")
    if not configured:
        return _lookup_from_database()
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

