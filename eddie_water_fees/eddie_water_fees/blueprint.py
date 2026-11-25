"""EDDIE imports <plugin>.blueprint for every plugin.

The results are served by GeoServer from the building_population table (tasks.py), on the
existing "Water fees (model result)" layer; these routes let the map run the two models
with their parameters and read a summary of the last result.

Routes:
    POST /water-fees/run                          run both scenarios in the background
    GET  /water-fees/summary.json                 totals of the newest result
    GET  /water-fees/building/<building_id>.json  one building's prices, per scenario and summed
    GET  /water-fees/popup.js                     the map's click popup (static/water_fees_popup.js)
"""
import re
from http.client import ACCEPTED, BAD_REQUEST
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory
from flask.typing import ResponseReturnValue as ResponseValue

blueprint = Blueprint("eddie_water_fees", __name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# A building id from the map ("ACT_<uuid>") or from the table ("Building2D.ACT_<uuid>").
BUILDING_ID = re.compile(r"^[A-Za-z0-9_.:-]{4,120}$")
# Columns the click popup reads, in the order they are selected.
BUILDING_COLUMNS = (
    "gml_id", "building_type", "footprint_m2", "est_population", "reach_id", "variant_key",
    "water_area_share_15min", "allocated_water_volume_15min_m3",
    "water_fee_15min_mean_eur", "water_fee_15min_min_eur", "water_fee_15min_max_eur",
    "tariff_mean_eur_15min", "tariff_intervals_15min", "gauge_quality_15min",
    "gauge_window_start_utc", "gauge_window_end_utc",
    "tariff_note_15min",  # AOI change (Robin, 2026-11-25): why scenario 1 is missing / partial
    "rainwater_area_share", "allocated_rainwater_volume_m3", "rainwater_cost_eur",
    "rain_mm_season", "rainwater_unit_price_eur_per_m3", "rain_season", "rain_source",
    "distance_to_river_m", "flood_risk_fee_eur",
    "pollution_multiplier", "water_quality_charge_eur",
    "water_consumption_liters", "water_consumption_m3", "water_consumption_cost_eur",
    "water_fees_computed_utc",
)

# Parameters the map may set, with their defaults (tasks.compute_water_fees) and limits.
PARAMETERS = {
    "percentage": (0.10, 0.0, 1.0),
    "unit_price": (7.0, 0.0, 1000.0),
    "rainwater_unit_price": (4.5, 0.0, 1000.0),
    "distance_threshold": (50.0, 0.0, 10000.0),
    "flood_risk_base_fee": (150.0, 0.0, 1e6),
    "quality_unit_price": (2.5, 0.0, 1000.0),
    "liters_per_person": (160.0, 0.0, 10000.0),
    "unit_price_per_liter": (0.005, 0.0, 100.0),
    "usable_space_pct": (0.80, 0.0, 1.0),
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
        # AOI change: the note column only exists after the first run of the AOI-aware task.
        note_sql = "MAX(tariff_note_15min)" if "tariff_note_15min" in fields else "NULL"
        row = conn.execute(sa.text(f"""
            SELECT reach_id, variant_key, COUNT(*) AS buildings,
                   COUNT(*) FILTER (WHERE water_fee_15min_mean_eur > 0) AS fee_buildings,
                   -- AOI change: no COALESCE, so a reach without scenario 1 gives NULL, not 0;
                   -- tariff_note says why (gauge coverage).
                   SUM(water_fee_15min_mean_eur) AS tariff_total_eur,
                   {note_sql} AS tariff_note,
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


def _number(value) -> float | None:
    """A rounded float, or None for a NULL column."""
    return None if value is None else round(float(value), 2)


def _tariff_detail(values: dict) -> str:
    """How the 15-minute tariff of this building was computed, in one line."""

    def stamp(value) -> str:
        return value.strftime("%Y-%m-%d %H:%M") if value is not None else "?"

    # AOI change: a reach outside the gauge coverage has no tariff; say why instead of the
    # (empty) window. A partly covered reach gets its note appended at the end.
    note = values.get("tariff_note_15min")
    if values.get("water_fee_15min_mean_eur") is None and note:
        return str(note)
    parts = []
    if values.get("tariff_intervals_15min"):
        parts.append(f"mean of {values['tariff_intervals_15min']} intervals")
    parts.append(f"gauges {stamp(values.get('gauge_window_start_utc'))} to "
                 f"{stamp(values.get('gauge_window_end_utc'))} UTC")
    if values.get("gauge_quality_15min"):
        parts.append(str(values["gauge_quality_15min"]))
    low = _number(values.get("water_fee_15min_min_eur"))
    high = _number(values.get("water_fee_15min_max_eur"))
    if low is not None and high is not None and low != high:
        parts.append(f"range \u20ac{low:,.2f} to \u20ac{high:,.2f}")
    if note:
        parts.append(str(note))  # AOI change: "partial: river volume only for km ..."
    return "; ".join(parts)


def _flood_risk_detail(values: dict) -> str:
    """How the proximity levy of this building was computed, in one line."""
    distance = _number(values.get("distance_to_river_m"))
    if distance is None:
        return "distance to the river not computed"
    if distance > 50.0:
        return f"{distance:,.0f} m from the river, outside the levy zone"
    return f"{distance:,.1f} m from the river, inside the levy zone"


def _quality_detail(values: dict) -> str:
    """How the water-quality surcharge of this building was computed, in one line."""
    parts = []
    footprint = _number(values.get("footprint_m2"))
    multiplier = _number(values.get("pollution_multiplier"))
    if footprint is not None:
        parts.append(f"{footprint:,.0f} m\u00b2 footprint")
    if multiplier is not None:
        kind = {3.0: "industrial", 2.0: "commercial / other", 1.0: "residential"}.get(multiplier)
        parts.append(f"\u00d7{multiplier:g}" + (f" {kind} runoff" if kind else " runoff"))
    return "; ".join(parts)


def _consumption_detail(values: dict) -> str:
    """How the consumption of this building was computed, in one line."""
    parts = []
    volume = _number(values.get("water_consumption_m3"))
    if volume is not None:
        parts.append(f"{volume:,.1f} m\u00b3")
    population = _number(values.get("est_population"))
    if population:
        parts.append(f"{population:,.1f} people \u00d7 160 l")
    else:
        parts.append("non-residential: usable floor space \u00d7 160/3 l")
    return "; ".join(parts)


def _rain_detail(values: dict) -> str:
    """How the seasonal-rain fee of this building was computed, in one line."""
    parts = []
    if values.get("rain_season"):
        parts.append(str(values["rain_season"]))
    if values.get("rain_mm_season") is not None:
        parts.append(f"{round(float(values['rain_mm_season']), 1)} mm of rain")
    if values.get("rainwater_unit_price_eur_per_m3") is not None:
        parts.append(f"\u20ac{round(float(values['rainwater_unit_price_eur_per_m3']), 2)}/m\u00b3")
    if values.get("rain_source"):
        parts.append(str(values["rain_source"]))
    return "; ".join(parts)


@blueprint.route("/water-fees/building/<path:building_id>.json", methods=["GET"])
def water_fees_building(building_id: str) -> ResponseValue:
    """
    One building's prices: scenario 1, scenario 2 and their sum.

    ``building_id`` is the map's id (``ACT_...``, the 3D buildings' ``buildingid``) or the
    table's (``Building2D.ACT_...``, its ``gml_id`` / ``localid``). The newest computed scope
    wins, so a clicked building shows the last run's prices whichever scope holds them.

    Example: /water-fees/building/ACT_3e3e3aaf-17fa-4dd2-9dca-4405b04fc25e.json
    """
    # pylint: disable=import-outside-toplevel
    import sqlalchemy as sa
    from eddie.digitaltwin import setup_environment

    from .tasks import COMPUTED_COLUMN, TABLE

    if not BUILDING_ID.match(building_id):
        return jsonify({"status": "error", "message": "Invalid building id."}), BAD_REQUEST
    plain = building_id[len("Building2D."):] if building_id.startswith("Building2D.") else building_id
    if not BUILDING_ID.match(plain):
        return jsonify({"status": "error", "message": "Invalid building id."}), BAD_REQUEST

    engine = setup_environment.get_database()
    with engine.connect() as conn:
        if not sa.inspect(conn).has_table(TABLE):
            return jsonify({"status": "no_data", "building_id": plain,
                            "message": "The population model has not been run yet."})
        existing = {column["name"] for column in sa.inspect(conn).get_columns(TABLE)}
        columns = [name for name in BUILDING_COLUMNS if name in existing]
        needed = ("water_fee_15min_mean_eur", "rainwater_cost_eur", "flood_risk_fee_eur",
                  "water_quality_charge_eur", "water_consumption_cost_eur")
        if [name for name in needed if name not in columns]:
            return jsonify({"status": "no_data", "building_id": plain,
                            "message": "The five water-fee scenarios have not been run yet."})
        select = ", ".join(f'"{name}"' for name in columns)
        row = conn.execute(
            sa.text(
                f'SELECT {select} FROM "{TABLE}" '
                "WHERE gml_id IN (:plain, :prefixed) OR localid IN (:plain, :prefixed) "
                f'ORDER BY "{COMPUTED_COLUMN}" DESC NULLS LAST LIMIT 1'
            ),
            {"plain": plain, "prefixed": f"Building2D.{plain}"},
        ).mappings().one_or_none()

    if row is None:
        return jsonify({"status": "no_data", "building_id": plain,
                        "message": "This building is not in a scope with population results."})
    values = dict(row)
    if values.get(COMPUTED_COLUMN) is None:
        return jsonify({"status": "no_data", "building_id": plain,
                        "message": "The 15-minute tariff and the rain fee have not been run for this building yet."})

    # AOI change (Robin, 2026-11-25): a NULL tariff now means "not computed for this reach"
    # (outside the gauge coverage), so it stays None - the popup shows "–" - instead of
    # becoming 0.0. The total then leaves it out, and the note says so.
    tariff_not_computed = values.get("water_fee_15min_mean_eur") is None
    tariff = None if tariff_not_computed else _number(values.get("water_fee_15min_mean_eur"))
    rain = _number(values.get("rainwater_cost_eur")) or 0.0
    flood = _number(values.get("flood_risk_fee_eur")) or 0.0
    quality = _number(values.get("water_quality_charge_eur")) or 0.0
    consumption = _number(values.get("water_consumption_cost_eur")) or 0.0
    computed = values.get(COMPUTED_COLUMN)
    return jsonify({
        "status": "success",
        "building_id": plain,
        "currency": "EUR",
        "building": {
            "gml_id": values.get("gml_id"),
            "type": values.get("building_type"),
            "footprint_m2": _number(values.get("footprint_m2")),
            "population": _number(values.get("est_population")),
        },
        "scope": {"reach_id": values.get("reach_id"), "variant_key": values.get("variant_key")},
        "scenarios": [
            {
                "id": "river_tariff",
                "name": "Scenario 1 \u00b7 river water tariff (15-minute gauges)",
                "cost_eur": tariff,
                "volume_m3": _number(values.get("allocated_water_volume_15min_m3")),
                "detail": _tariff_detail(values),
            },
            {
                "id": "seasonal_rain",
                "name": "Scenario 2 \u00b7 seasonal rain on the roofs",
                "cost_eur": rain,
                "volume_m3": _number(values.get("allocated_rainwater_volume_m3")),
                "detail": _rain_detail(values),
            },
            {
                "id": "flood_risk",
                "name": "Scenario 3 \u00b7 flood-risk proximity levy",
                "cost_eur": flood,
                "volume_m3": None,
                "detail": _flood_risk_detail(values),
            },
            {
                "id": "water_quality",
                "name": "Scenario 4 \u00b7 water-quality surcharge",
                "cost_eur": quality,
                "volume_m3": None,
                "detail": _quality_detail(values),
            },
            {
                "id": "consumption",
                "name": "Scenario 5 \u00b7 water consumption of the building",
                "cost_eur": consumption,
                "volume_m3": _number(values.get("water_consumption_m3")),
                "detail": _consumption_detail(values),
            },
        ],
        "total_eur": round((tariff or 0.0) + rain + flood + quality + consumption, 2),
        "computed_utc": computed.isoformat() if hasattr(computed, "isoformat") else computed,
        # AOI change: first case added; the residential note is unchanged.
        "note": ("Total without scenario 1: the river tariff is not computed for this reach."
                 if tariff_not_computed else
                 "Residential: the river-tariff pool is shared by the non-residential buildings."
                 if tariff == 0.0 else None),
    })


@blueprint.route("/water-fees/popup.js", methods=["GET"])
def water_fees_popup_script() -> ResponseValue:
    """The click popup's script, served by the plugin so that no other code has to hold it."""
    return send_from_directory(STATIC_DIR, "water_fees_popup.js",
                               mimetype="application/javascript", max_age=60)
