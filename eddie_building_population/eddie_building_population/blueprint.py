"""EDDIE imports <plugin>.blueprint for every plugin.

The results are served by GeoServer from the building_population table (tasks.py); these routes let the
map run the model with its water-fee parameters and read a summary of the last result.
"""
from http.client import ACCEPTED, BAD_REQUEST

from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue as ResponseValue

blueprint = Blueprint("eddie_building_population", __name__)

# Water-fee parameters the map may set, with their defaults (tasks.compute_building_population) and limits.
PARAMETERS = {
    "total_water_volume": (100000.0, 0.0, 1e9),
    "percentage": (0.10, 0.0, 1.0),
    "unit_price": (7.0, 0.0, 1000.0),
}


@blueprint.route("/building-population/run", methods=["POST"])
def run_building_population() -> ResponseValue:
    """
    Run the population and non-residential water-fee model in the background.

    JSON (all optional): {"total_water_volume": 100000, "percentage": 0.1, "unit_price": 7}.
    Follow the returned task_id on GET /digital-twin-task-status/<task_id>.
    """
    # pylint: disable=import-outside-toplevel
    from .tasks import compute_building_population

    payload = request.get_json(silent=True) or {}
    arguments = {}
    for name, (default, low, high) in PARAMETERS.items():
        try:
            value = float(payload.get(name, default))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": f"{name} must be a number."}), BAD_REQUEST
        if not low <= value <= high:
            return jsonify({"status": "error", "message": f"{name} must be between {low:g} and {high:g}."}), BAD_REQUEST
        arguments[name] = value
    task = compute_building_population.delay(**arguments)
    return jsonify({"status": "processing", "task_id": task.id, "parameters": arguments}), ACCEPTED


@blueprint.route("/building-population/summary.json", methods=["GET"])
def building_population_summary() -> ResponseValue:
    """Return the totals of the last model result (building_population), or status no_data before any run."""
    # pylint: disable=import-outside-toplevel
    import sqlalchemy as sa
    from eddie.digitaltwin import setup_environment

    from .tasks import TABLE

    from eddie_base_data.publish import view_name

    engine = setup_environment.get_database()
    with engine.connect() as conn:
        if not sa.inspect(conn).has_table(TABLE):
            return jsonify({"status": "no_data", "message": "The water-fee model has not been run yet."})
        # AOI change (2026-11-20): the table keeps results for every reach the user has selected;
        # the summary must describe the CURRENT reach only, which is exactly what the published
        # view (<table>_4326) shows. Summing the table added up every reach together.
        row = conn.execute(sa.text(f"""
            SELECT COUNT(*) AS buildings,
                   COALESCE(SUM(est_population), 0) AS est_population,
                   COUNT(*) FILTER (WHERE allocated_water_volume > 0) AS fee_buildings,
                   COALESCE(SUM(allocated_water_volume), 0) AS allocated_water_volume,
                   COALESCE(SUM(total_cost), 0) AS total_cost,
                   MAX(total_water_baseline) AS total_water_volume,
                   MAX(extra_water_pool) AS extra_water_pool,
                   MAX(unit_price) AS unit_price,
                   MAX(computed_utc) AS computed_utc,
                   MAX(reach_id) AS reach_id, MAX(variant_key) AS variant_key
            FROM "{view_name(TABLE)}" """)).mappings().one()
    result = {key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in row.items()}
    for key, value in result.items():
        if key not in ("computed_utc", "reach_id", "variant_key") and value is not None:
            result[key] = round(float(value), 2)
    return jsonify({"status": "success", **result})
