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


def _reach_subset():
    """AOI stand-in (Robin, 2026-11-25, task 8.10) until your damage step runs per reach.

    Replaces the all-or-nothing guard of 2026-11-20 (_require_current_reach), which hid ALL
    damage as soon as another reach was selected. Your method itself is untouched.

    Your loaded run (today: km 35-50) is used as it is, but only for the buildings that lie in
    the CURRENT reach's 500 m corridor (the area the AOI module gives "flood_costs"):
      * returns None when the run was computed for the current reach: everything applies;
      * otherwise returns (ids, buildings_in_reach): the building ids of your run inside the
        current corridor, and how many buildings the reach has (building_population), for a
        "N of M buildings in this reach have a damage estimate" label.
    Buildings outside that set are reported "not computed for this reach" - never 0 EUR.
    The map layer is cut the same way (flood_cost_buildings_4326, see tasks.py).
    """
    # pylint: disable=import-outside-toplevel
    from sqlalchemy import text
    from eddie_aoi.current import current_reach
    from eddie_base_data.publish import current_area_filter

    with _engine().connect() as conn:
        current = current_reach(conn).reach_id
        computed_for = conn.execute(text(
            "SELECT DISTINCT b.reach_id FROM flood_cost_buildings b "
            "JOIN flood_cost_lookup l ON l.run_id = b.run_id")).scalars().all()
        if computed_for == [current]:
            return None
        ids = set(conn.execute(text(
            "SELECT b.buildingid FROM flood_cost_buildings b JOIN flood_cost_lookup l ON l.run_id = b.run_id "
            f"WHERE {current_area_filter(conn)}")).scalars().all())
        in_reach = conn.execute(text(
            "SELECT count(*) FROM building_population WHERE reach_id = :r AND variant_key = 'corridor_500'"),
            {"r": current}).scalar()
    return ids, int(in_reach or 0)


def _not_computed(buildingid):
    """A building of the current reach that your loaded run does not cover (task 8.10)."""
    return jsonify({"status": "not_computed", "found": False, "buildingid": buildingid, "damage_eur2010": None,
                    "message": "Flood damage is not computed for this building in the current reach."}), 404


def _lookup_from_database() -> CostLookup:
    """The result loaded by tasks.load_flood_costs_to_db, used when no lookup file is configured."""
    try:
        from sqlalchemy import text  # pylint: disable=import-outside-toplevel
        with _engine().connect() as conn:
            version = conn.execute(text("SELECT dataset_version FROM flood_cost_lookup")).scalar_one()
            # AOI: the reach check moved to the routes (_reach_subset), so it can be partial.
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
        lookup = _lookup()
        result = lookup.summary(scenario)
        subset = _reach_subset() if not _lookup_path_configured() else None
        if subset is not None:
            # AOI stand-in (task 8.10): the totals of your run's buildings inside the current
            # reach only, computed with your own _totals, and labelled partial.
            from .cost_lookup import _totals  # pylint: disable=import-outside-toplevel
            ids, in_reach = subset
            buildings = {bid: rec for bid, rec in lookup.to_dict()["buildings"].items() if bid in ids}
            # Same keys as your summary, recomputed over the subset; none in the reach -> no
            # total at all (None), never 0 EUR.
            subset_totals = _totals(buildings, lookup.scenarios)[scenario] if buildings else {"total_eur2010": None}
            result = {**result, **subset_totals, "partial": True, "buildings_with_estimate": len(buildings), "buildings_in_reach": in_reach,
                      "partial_note": f"{len(buildings)} of {in_reach} buildings in this reach have a damage "
                                      "estimate (from the km 35-50 run); the others are not computed."}
    except InvalidScenario as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except (OSError, LookupValidationError) as exc:
        return _unavailable(exc)
    return jsonify({"status": "success", **result})


def _lookup_path_configured() -> bool:
    """True when the costs come from a configured lookup file (no database, no AOI stand-in)."""
    return bool(current_app.config.get("FLOOD_COSTS_LOOKUP_PATH") or os.environ.get("FLOOD_COSTS_LOOKUP_PATH"))


@blueprint.route("/buildings/<buildingid>/<scenario>.json", methods=["GET"])
def building_cost(buildingid, scenario):
    """Known unknown costs return 200/null; absent IDs return 404/null."""
    try:
        scenario = normalize_scenario(scenario)
        subset = _reach_subset() if not _lookup_path_configured() else None
        if subset is not None and buildingid not in subset[0]:
            return _not_computed(buildingid)  # AOI stand-in (task 8.10): outside your run's part of the reach
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
        subset = _reach_subset() if not _lookup_path_configured() else None
    except (OSError, LookupValidationError) as exc:
        return _unavailable(exc)
    if subset is not None and buildingid not in subset[0]:
        return _not_computed(buildingid)  # AOI stand-in (task 8.10)
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

