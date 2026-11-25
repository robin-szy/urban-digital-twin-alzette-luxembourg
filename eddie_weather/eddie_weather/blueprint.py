"""Data API of the weather module, for the map (weather-control.js) and the other modules.

GET  /weather/places                 sub-catchments and gauge catchments (GeoJSON)
GET  /weather/series                 values of places between two times, level chosen to fit
GET  /weather/at                     one value per place at one time (to colour the map)
GET  /weather/status                 what is stored, the backfill log, the model scores
POST /weather/run/<step>             start a step now (setup, backfill, catch_up, forecast, flow, calibrate)

Answers are small (a few thousand values at most) and carry cache headers: values of the
last days change, older final values do not.
"""

from __future__ import annotations

from http.client import ACCEPTED, BAD_REQUEST, NOT_FOUND

import pandas as pd
from flask import Blueprint, jsonify, request

blueprint = Blueprint("eddie_weather", __name__)

VARIABLES = ("rain", "temperature", "flow")
LEVELS = ("auto", "15min", "hour", "day", "month", "season", "year")


def _engine():
    from eddie.digitaltwin import setup_environment  # pylint: disable=import-outside-toplevel
    return setup_environment.get_database()


def _error(message: str, code: int = BAD_REQUEST):
    return jsonify({"status": "error", "message": message}), code


def _cached(response, recent: bool):
    response.headers["Cache-Control"] = "public, max-age=120" if recent else "public, max-age=86400"
    return response


def _time(name: str):
    value = request.args.get(name)
    if not value:
        raise ValueError(f"{name} is needed (a date or an ISO time, e.g. 2021-07-14 or 2021-07-14T12:00Z)")
    # A date or time without a zone is Luxembourg time, as people read it: 2025-12-01 is the
    # local midnight that starts winter, 2025-11-30T23:00Z.
    from eddie_weather.catchment import LOCAL_TIME_ZONE  # pylint: disable=import-outside-toplevel
    ts = pd.Timestamp(value)
    return ts.tz_localize(LOCAL_TIME_ZONE).tz_convert("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


@blueprint.route("/weather/places", methods=["GET"])
def places():
    from eddie_weather import db  # pylint: disable=import-outside-toplevel

    kind = request.args.get("kind")
    with _engine().begin() as conn:
        db.ensure_tables(conn)
        rows = db.read_places(conn, kind)
    features = [{"type": "Feature", "id": r["place_id"], "geometry": r["geojson"],
                 "properties": {"id": r["place_id"], "name": r["name"], "kind": r["kind"],
                                "gauge_id": r["gauge_id"], "area_km2": round(r["area_km2"], 2)}} for r in rows]
    return _cached(jsonify({"type": "FeatureCollection", "features": features}), recent=False)


@blueprint.route("/weather/series", methods=["GET"])
def series():
    from eddie_weather import db, pipeline  # pylint: disable=import-outside-toplevel

    try:
        start, end = _time("start"), _time("end")
        variable = request.args.get("variable", "rain")
        level = request.args.get("level", "auto")
        if variable not in VARIABLES:
            raise ValueError(f"variable must be one of {', '.join(VARIABLES)}")
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        wanted = [p for p in request.args.get("places", "").split(",") if p]
    except ValueError as error:
        return _error(str(error))
    with _engine().begin() as conn:
        rows = {r["place_id"]: r for r in db.read_places(conn)}
        wanted = wanted or [p for p, r in rows.items() if r["kind"] == "sub_catchment"]
        unknown = [p for p in wanted if p not in rows]
        if unknown:
            return _error(f"unknown places: {', '.join(unknown)}", NOT_FOUND)
        kind = "gauge_catchment" if any(rows[p]["kind"] == "gauge_catchment" for p in wanted) else "sub_catchment"
        try:
            answer = pipeline.series(conn, wanted, variable, start, end, level, kind,
                                     {p: rows[p]["area_km2"] for p in wanted})
        except ValueError as error:
            return _error(str(error))
    recent = end > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=10)
    return _cached(jsonify(answer), recent)


@blueprint.route("/weather/at", methods=["GET"])
def at():
    """One value per place for the period holding ``time`` (default: the hour)."""
    from eddie_weather import db  # pylint: disable=import-outside-toplevel

    try:
        time = _time("time")
        variable = request.args.get("variable", "rain")
        level = request.args.get("level", "hour")
        if variable not in VARIABLES or level not in LEVELS[1:]:
            raise ValueError("unknown variable or level")
    except ValueError as error:
        return _error(str(error))
    span = {"15min": pd.Timedelta(minutes=15), "hour": pd.Timedelta(hours=1), "day": pd.Timedelta(days=1),
            "month": pd.Timedelta(days=31), "season": pd.Timedelta(days=92), "year": pd.Timedelta(days=366)}[level]
    with _engine().begin() as conn:
        ids = [r["place_id"] for r in db.read_places(conn)]
        long = db.read_values(conn, ids, variable, level, time - span + pd.Timedelta(seconds=1), time + pd.Timedelta(seconds=1))
    latest = long.sort_values("time_utc").groupby("place_id").tail(1)
    values = {r.place_id: {"value": round(float(r.value), 3), "time": r.time_utc.isoformat(), "quality": r.quality}
              for r in latest.itertuples()}
    recent = time > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=10)
    return _cached(jsonify({"variable": variable, "level": level, "time": time.isoformat(), "places": values}), recent)


@blueprint.route("/weather/status", methods=["GET"])
def status():
    from eddie_weather import db  # pylint: disable=import-outside-toplevel

    with _engine().begin() as conn:
        db.ensure_tables(conn)
        models = db.read_models(conn)
        answer = {
            "places": len(db.read_places(conn)),
            "coverage": db.coverage(conn),
            "jobs": db.read_jobs(conn),
            "models": {str(k): {"params": m["params"], "scores": m["scores"],
                                "calibrated_at": str(m["calibrated_at"]), "state_day": str(m["state_day"])}
                       for k, m in models.items()},
        }
    response = jsonify(answer)
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.route("/weather/run/<step>", methods=["POST"])
def run(step: str):
    from eddie_weather import tasks  # pylint: disable=import-outside-toplevel

    payload = request.get_json(silent=True) or {}
    starters = {
        "setup": lambda: tasks.setup_places.delay(payload["path"], payload.get("id_field")),
        "backfill": lambda: tasks.backfill.delay(int(payload.get("first_year", 1950)), int(payload.get("max_chunks", 1))),
        "catch_up": tasks.catch_up.delay,
        "forecast": tasks.update_forecast.delay,
        "flow": tasks.update_flow.delay,
        "calibrate": lambda: tasks.calibrate.delay(int(payload["gauge_id"]), payload["flow_csv"],
                                                   int(payload.get("iterations", 1500))),
    }
    if step not in starters:
        return _error(f"step must be one of {', '.join(starters)}", NOT_FOUND)
    try:
        task = starters[step]()
    except (KeyError, ValueError) as error:
        return _error(f"missing or wrong value: {error}")
    return jsonify({"status": "processing", "task_id": task.id, "step": step}), ACCEPTED
