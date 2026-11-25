"""Read-only EDDIE Flask blueprint. No data is loaded during plugin discovery."""
from functools import lru_cache
import json
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from .cost_lookup import CostLookup, InvalidScenario, LookupValidationError, normalize_scenario
from .popup_context import DatabaseDetails, PopupDetails, build_popup_payload

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


def _require_current_reach(conn):
    """AOI change (Robin, 2026-11-20): only serve damage computed for the twin's CURRENT reach.

    The loaded result belongs to the run that wrote it (its rows in flood_cost_buildings carry
    the reach). When the user has selected another reach, these figures describe a different
    stretch of river, so they are reported as unavailable instead of shown as if they applied.
    """
    from sqlalchemy import text  # pylint: disable=import-outside-toplevel
    from eddie_aoi.current import current_reach  # pylint: disable=import-outside-toplevel

    computed_for = conn.execute(text(
        "SELECT DISTINCT b.reach_id FROM flood_cost_buildings b "
        "JOIN flood_cost_lookup l ON l.run_id = b.run_id")).scalars().all()
    current = current_reach(conn).reach_id
    if computed_for != [current]:
        raise LookupValidationError(
            f"The loaded flood costs were computed for {computed_for}, not for the current reach "
            f"{current}; they are not shown for it.")


def _lookup_from_database() -> CostLookup:
    """The result loaded by tasks.load_flood_costs_to_db, used when no lookup file is configured."""
    try:
        from sqlalchemy import text  # pylint: disable=import-outside-toplevel
        with _engine().connect() as conn:
            version = conn.execute(text("SELECT dataset_version FROM flood_cost_lookup")).scalar_one()
            _require_current_reach(conn)
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


@lru_cache(maxsize=4)
def _load_popup_details(path: str, mtime_ns: int, size: int) -> PopupDetails:
    return PopupDetails(path)


@lru_cache(maxsize=4)
def _database_run_id(dataset_version: str):
    """The run_id stored with the loaded lookup; None if the table has no run_id column."""
    try:
        from sqlalchemy import text  # pylint: disable=import-outside-toplevel
        with _engine().connect() as conn:
            return conn.execute(text("SELECT run_id FROM flood_cost_lookup WHERE dataset_version = :v"),
                                {"v": dataset_version}).scalar()
    except Exception:  # pylint: disable=broad-except
        return None


@blueprint.route("/popup/<buildingid>.json", methods=["GET"])
def building_popup(buildingid):
    """Available precomputed scenarios for one original ID; no geometry or totals.

    Optional context is read from the original monetary GeoJSON, never generated
    here. The existing scenario and summary endpoint contracts stay unchanged.
    """
    try:
        lookup = _lookup()
    except (OSError, LookupValidationError) as exc:
        return _unavailable(exc)
    details, warning = None, None
    configured = current_app.config.get("FLOOD_COSTS_DETAILS_PATH") or os.environ.get("FLOOD_COSTS_DETAILS_PATH")
    if configured:
        try:
            path = Path(configured).expanduser().resolve()
            stat = path.stat()
            details = _load_popup_details(str(path), stat.st_mtime_ns, stat.st_size)
        except (OSError, LookupValidationError) as exc:
            current_app.logger.warning("Flood-cost popup context unavailable: %s", exc)
            warning = "Whole-building area, depth and exposure are unavailable."
    elif not (current_app.config.get("FLOOD_COSTS_LOOKUP_PATH") or os.environ.get("FLOOD_COSTS_LOOKUP_PATH")):
        # Costs came from the database: take area, depth and exposure from the same published run.
        details = DatabaseDetails(_engine(), _database_run_id(lookup.to_dict()["dataset_version"]))
    result = build_popup_payload(lookup, buildingid, details, warning)
    return jsonify(result), 200 if result["found"] else 404

