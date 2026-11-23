"""EDDIE imports <plugin>.blueprint for every plugin.

The results are served by GeoServer from the building_population table (tasks.py), on the
existing "Water fees (model result)" layer; these routes let the map run the two models
with their parameters and read a summary of the last result.
"""
from http.client import ACCEPTED, BAD_REQUEST

from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue as ResponseValue

blueprint = Blueprint("eddie_water_fees", __name__)

# Parameters the map may set, with their defaults (tasks.compute_water_fees) and limits.
PARAMETERS = {
    "percentage": (0.10, 0.0, 1.0),
    "unit_price": (7.0, 0.0, 1000.0),
    "rainwater_unit_price": (4.5, 0.0, 1000.0),
}
SEASONS = ("winter", "spring", "summer", "autumn")


@blueprint.route("/water-fees/run", methods=["POST"])
def run_water_fees() -> ResponseValue:
    """
    Run the 15-minute gauge tariff and the seasonal-rain fee in the background.

    JSON (all optional): {"percentage": 0.1, "unit_price": 7, "rainwater_unit_price": 4.5,
    "gauge_start": "2026-11-20T14:00", "gauge_end": "2026-11-20T18:00", "readings_json": null,
    "season": "summer", "season_year": 2024, "use_normals": false,
    "reach_id": null, "variant_key": null}.
    Follow the returned task_id on GET /digital-twin-task-status/<task_id>.
    """
    # pylint: disable=import-outside-toplevel
    from .tasks import compute_water_fees

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

    season = str(payload.get("season", "summer")).strip().lower()
    if season not in SEASONS:
        return jsonify({"status": "error", "message": f"season must be one of {', '.join(SEASONS)}."}), BAD_REQUEST
    arguments["season"] = season

    try:
        arguments["season_year"] = int(payload.get("season_year", 2024))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "season_year must be a year."}), BAD_REQUEST
    if not 1950 <= arguments["season_year"] <= 2100:
        return jsonify({"status": "error", "message": "season_year must be between 1950 and 2100."}), BAD_REQUEST

    for name in ("gauge_start", "gauge_end", "readings_json", "reach_id", "variant_key"):
        value = payload.get(name)
        arguments[name] = str(value) if value not in (None, "") else None
    arguments["use_normals"] = bool(payload.get("use_normals", False))

    task = compute_water_fees.delay(**arguments)
    return jsonify({"status": "processing", "task_id": task.id, "parameters": arguments}), ACCEPTED


@blueprint.route("/water-fees/summary.json", methods=["GET"])
def water_fees_summary() -> ResponseValue:
    """Return the totals of the newest result, or status no_data before any run."""
    # pylint: disable=import-outside-toplevel
    import sqlalchemy as sa
    from eddie.digitaltwin import setup_environment

    from .tasks import COMPUTED_COLUMN, TABLE

    engine = setup_environment.get_database()
    with engine.connect() as conn:
        if not sa.inspect(conn).has_table(TABLE):
            return jsonify({"status": "no_data", "message": "The population model has not been run yet."})
        fields = {column["name"] for column in sa.inspect(conn).get_columns(TABLE)}
        if COMPUTED_COLUMN not in fields:
            return jsonify({"status": "no_data", "message": "The 15-minute tariff and the rain fee have not been run yet."})
        row = conn.execute(sa.text(f"""
            SELECT reach_id, variant_key, COUNT(*) AS buildings,
                   COUNT(*) FILTER (WHERE water_fee_15min_mean_eur > 0) AS fee_buildings,
                   COALESCE(SUM(water_fee_15min_mean_eur), 0) AS tariff_total_eur,
                   MAX(tariff_mean_eur_15min) AS tariff_mean_eur,
                   MIN(water_fee_15min_mean_eur) AS fee_min_eur,
                   MAX(water_fee_15min_max_eur) AS fee_max_eur,
                   MAX(tariff_intervals_15min) AS intervals,
                   MAX(gauge_quality_15min) AS gauge_quality,
                   MAX(gauge_window_start_utc) AS gauge_window_start_utc,
                   MAX(gauge_window_end_utc) AS gauge_window_end_utc,
                   MAX(rain_mm_season) AS rain_mm,
                   MAX(rain_volume_m3) AS rain_volume_m3,
                   COALESCE(SUM(rainwater_cost_eur), 0) AS rainwater_cost_eur,
                   MAX(rain_season) AS rain_season,
                   MAX(rain_source) AS rain_source,
                   MAX({COMPUTED_COLUMN}) AS computed_utc
            FROM "{TABLE}"
            WHERE {COMPUTED_COLUMN} IS NOT NULL
            GROUP BY reach_id, variant_key
            ORDER BY MAX({COMPUTED_COLUMN}) DESC
            LIMIT 1 """)).mappings().one_or_none()
    if row is None:
        return jsonify({"status": "no_data", "message": "The 15-minute tariff and the rain fee have not been run yet."})
    result = {key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in row.items()}
    for key, value in result.items():
        if isinstance(value, float):
            result[key] = round(value, 2)
    return jsonify({"status": "success", **result})
