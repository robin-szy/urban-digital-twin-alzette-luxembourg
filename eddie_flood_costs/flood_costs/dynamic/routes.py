"""
API of the dynamic flood costs, added to the flood-costs blueprint (prefix /flood-costs).

Read the published results (PostGIS, written by the tasks):
  GET  /flood-costs/dynamic/parameters.json                 model assumptions
  GET  /flood-costs/dynamic/curve.json                      cost per slider stage (+ static HQ totals)
  GET  /flood-costs/dynamic/stage/<cm>.json                 totals at a slider stage
  GET  /flood-costs/dynamic/stage/<cm>/<buildingid>.json    one building at a slider stage
  GET  /flood-costs/dynamic/building/<buildingid>/curve.json  one building over all stages
  GET  /flood-costs/dynamic/events.json                     computed periods
  GET  /flood-costs/dynamic/event/<event_id>.json           totals per timestep
  GET  /flood-costs/dynamic/event/<event_id>/<buildingid>.json[?time=ISO]  one building over time

Start a computation (Celery; follow GET /digital-twin-task-status/<task_id>):
  POST /flood-costs/dynamic/curve/run   {"stages": "0:600:10", "durations": "6,24,72"}
  POST /flood-costs/dynamic/event/run   {"start": ISO, "end": ISO, "event_id": optional}

Used by those tasks (the water model lives in this container only):
  GET  /flood-costs/dynamic/depths.json?stage=<cm> | ?key=<level key>
  GET  /flood-costs/dynamic/keyframes.json?start=ISO&end=ISO

Every answer says which static run it belongs to; a result of an older run is "stale".
Unknown values are null, never 0. Outside the current reach's part of the run (Robin's AOI
stand-in, blueprint._reach_subset) a building is "not computed".
"""
import functools
import json
import re
import threading

import numpy as np
import pandas as pd
from flask import jsonify, request

from . import jobs, parameters, store
from .depths import LocalDepths, encode

ACCEPTED, BAD_REQUEST, NOT_FOUND, UNAVAILABLE = 202, 400, 404, 503
EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_LOCK = threading.Lock()


def _engine():
    from .. import blueprint as static  # pylint: disable=import-outside-toplevel
    return static._engine()  # pylint: disable=protected-access


def _subset():
    """Building ids of the published run inside the current reach, or None when all apply."""
    try:
        from .. import blueprint as static  # pylint: disable=import-outside-toplevel
        result = static._reach_subset()  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except  # no AOI module (tests, outside EDDIE)
        return None
    return None if result is None else result[0]


def _unavailable(exc, status=UNAVAILABLE):
    return jsonify({"status": "unavailable", "message": str(exc)}), status


def _json(frame: pd.DataFrame) -> list:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _freshness(conn, stored_version) -> dict:
    run = store.published_run(conn)
    stale = stored_version != run["dataset_version"]
    return {"run_id": run["run_id"], "dataset_version": run["dataset_version"], "stale": stale,
            **({"message": "Computed for an older static result; run it again."} if stale else {})}


def _models(frame: pd.DataFrame) -> list:
    return [c for c in frame.columns if c.startswith(("model", "disruption"))]


def _sum_by(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """Totals of known buildings per stage/timestep (unknown stays out, not 0)."""
    columns = _models(frame)
    grouped = frame.groupby(key)
    out = grouped[columns].sum(min_count=1)
    first = columns[0]
    out["damaged_buildings"] = grouped[first].apply(lambda values: int((values > 0).sum()))
    out["known_buildings"] = grouped[first].count()
    out["unknown_buildings"] = grouped[first].size() - out["known_buildings"]
    return out.reset_index()


# ----------------------------------------------------------------------- depths (backend only)


@functools.lru_cache(maxsize=2)
def _samples_for(dataset_version: str):
    with _engine().connect() as conn:
        run = store.published_run(conn)
        if run["dataset_version"] != dataset_version:
            raise store.DynamicUnavailable("The published run changed; retry.")
        samples = store.load_samples(conn, run)
    return run, samples, store.samples_signature(samples), LocalDepths(samples)


@functools.lru_cache(maxsize=128)
def _depths_for(dataset_version: str, key_text: str) -> list:
    _, _, _, local = _samples_for(dataset_version)
    with _LOCK:
        return encode(local.depths(local.parse_key(key_text)))


def add_routes(blueprint):  # pylint: disable=too-many-statements
    """Register the dynamic routes on the flood-costs blueprint."""

    @blueprint.route("/dynamic/parameters.json", methods=["GET"])
    def dynamic_parameters():
        return jsonify({"status": "success", **jobs.assumptions(),
                        "curve_stages_cm": list(parameters.CURVE_STAGES_CM),
                        "curve_durations_hours": list(parameters.CURVE_DURATIONS_HOURS),
                        "max_event_days": parameters.MAX_EVENT_DAYS,
                        "note": "beta and tau are team assumptions; the Model 3 unit costs are placeholders."})

    @blueprint.route("/dynamic/depths.json", methods=["GET"])
    def dynamic_depths():
        try:
            with _engine().connect() as conn:
                run = store.published_run(conn)
            if "stage" in request.args:
                key_text = LocalDepths.key_text(LocalDepths.stage_key(float(request.args["stage"])))
            elif "key" in request.args:
                key_text = LocalDepths.key_text(LocalDepths.parse_key(request.args["key"]))
            else:
                return jsonify({"status": "error", "message": "Give stage=<cm> or key=<level key>."}), BAD_REQUEST
            _, samples, signature, _ = _samples_for(run["dataset_version"])
            values = _depths_for(run["dataset_version"], key_text)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), BAD_REQUEST
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        return jsonify({"status": "success", "run_id": run["run_id"], "dataset_version": run["dataset_version"],
                        "signature": signature, "key": key_text, "samples": len(samples), "depths_m": values})

    @blueprint.route("/dynamic/keyframes.json", methods=["GET"])
    def dynamic_keyframes():
        try:
            start, end = jobs.parse_period(request.args["start"], request.args["end"])
            frames = LocalDepths.keyframes(start.isoformat(), end.isoformat())
        except (KeyError, ValueError) as exc:
            return jsonify({"status": "error", "message": f"Invalid period: {exc}"}), BAD_REQUEST
        except Exception as exc:  # pylint: disable=broad-except  # e.g. no gauge readings stored
            return _unavailable(exc, NOT_FOUND)
        return jsonify({"status": "success", **frames})

    # ------------------------------------------------------------------- stage curve

    def _curve_frames(conn):
        full = store.read_curve(conn)
        ids = _subset()
        if ids is None:
            return full, False
        buildings = store.read_curve_buildings(conn)
        if not buildings["buildingid"].isin(ids).any():
            raise store.DynamicUnavailable(
                "No building of the selected reach is covered by the published flood-cost run "
                "(e.g. the reach lies outside the gauge coverage km 35-54.3). Not computed - not zero.")
        curve = _sum_by(buildings[buildings["buildingid"].isin(ids)].drop(
            columns=["run_id", "dataset_version", "buildingid"]), "stage_cm")
        for key in ("dataset_version", "computed_utc", "durations_hours"):
            if key in full.columns:
                curve[key] = full[key].iloc[0]
        return curve, True

    @blueprint.route("/dynamic/curve.json", methods=["GET"])
    def dynamic_curve():
        try:
            with _engine().connect() as conn:
                curve, partial = _curve_frames(conn)
                fresh = _freshness(conn, curve["dataset_version"].iloc[0])
                references = store.static_totals(conn)
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        table = curve.set_index("stage_cm")
        equivalent = jobs.models.equivalent_stages(table, references) if references and not partial else {}
        meta = {k: curve[k].iloc[0] for k in ("computed_utc", "durations_hours") if k in curve.columns}
        return jsonify({"status": "success", **fresh, **meta, "partial_reach": partial, "currency": "EUR",
                        "price_year": 2010, "models": _models(curve),
                        "static_totals_eur2010": references, "model1_equivalent_stage_cm": equivalent,
                        "stages": _json(curve.drop(columns=[c for c in ("run_id", "dataset_version", "assumptions")
                                                            if c in curve.columns]))})

    def _nearest_stage(frame: pd.DataFrame, stage: float) -> float:
        stages = np.unique(frame["stage_cm"].to_numpy(dtype=float))
        return float(stages[np.argmin(np.abs(stages - stage))])

    @blueprint.route("/dynamic/stage/<float(signed=False):stage>.json", methods=["GET"])
    @blueprint.route("/dynamic/stage/<int:stage>.json", methods=["GET"])
    def dynamic_stage(stage):
        try:
            with _engine().connect() as conn:
                curve, partial = _curve_frames(conn)
                fresh = _freshness(conn, curve["dataset_version"].iloc[0])
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        used = _nearest_stage(curve, float(stage))
        row = curve[curve["stage_cm"].astype(float) == used].drop(
            columns=[c for c in ("run_id", "dataset_version", "assumptions") if c in curve.columns])
        return jsonify({"status": "success", **fresh, "requested_stage_cm": float(stage), "stage_cm": used,
                        "partial_reach": partial, **_json(row)[0]})

    def _building_guard(buildingid):
        ids = _subset()
        if ids is not None and buildingid not in ids:
            return jsonify({"status": "not_computed", "found": False, "buildingid": buildingid,
                            "message": "Flood damage is not computed for this building in the current reach."}), NOT_FOUND
        return None

    @blueprint.route("/dynamic/stage/<float(signed=False):stage>/<buildingid>.json", methods=["GET"])
    @blueprint.route("/dynamic/stage/<int:stage>/<buildingid>.json", methods=["GET"])
    def dynamic_stage_building(stage, buildingid):
        blocked = _building_guard(buildingid)
        if blocked:
            return blocked
        try:
            with _engine().connect() as conn:
                rows = store.read_curve_buildings(conn, buildingid=buildingid)
                if rows.empty:
                    return jsonify({"status": "not_found", "found": False, "buildingid": buildingid,
                                    "message": "No dynamic cost for this building (not classified, or not in the run)."}), NOT_FOUND
                fresh = _freshness(conn, rows["dataset_version"].iloc[0])
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        used = _nearest_stage(rows, float(stage))
        row = rows[rows["stage_cm"].astype(float) == used].drop(columns=["run_id", "dataset_version"])
        return jsonify({"status": "success", "found": True, **fresh, "buildingid": buildingid,
                        "requested_stage_cm": float(stage), "currency": "EUR", "price_year": 2010, **_json(row)[0]})

    @blueprint.route("/dynamic/building/<buildingid>/curve.json", methods=["GET"])
    def dynamic_building_curve(buildingid):
        blocked = _building_guard(buildingid)
        if blocked:
            return blocked
        try:
            with _engine().connect() as conn:
                rows = store.read_curve_buildings(conn, buildingid=buildingid)
                if rows.empty:
                    return jsonify({"status": "not_found", "found": False, "buildingid": buildingid}), NOT_FOUND
                fresh = _freshness(conn, rows["dataset_version"].iloc[0])
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        return jsonify({"status": "success", "found": True, **fresh, "buildingid": buildingid, "currency": "EUR",
                        "price_year": 2010, "stages": _json(rows.drop(columns=["run_id", "dataset_version", "buildingid"]))})

    # ------------------------------------------------------------------- events

    @blueprint.route("/dynamic/events.json", methods=["GET"])
    def dynamic_events():
        try:
            with _engine().connect() as conn:
                events = store.read_events(conn)
                run = store.published_run(conn)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        if not events.empty:
            events["stale"] = events["dataset_version"] != run["dataset_version"]
            events = events.drop(columns=[c for c in ("assumptions",) if c in events.columns])
        return jsonify({"status": "success", "run_id": run["run_id"], "events": _json(events)})

    @blueprint.route("/dynamic/event/<event_id>.json", methods=["GET"])
    def dynamic_event(event_id):
        try:
            with _engine().connect() as conn:
                meta, totals = store.read_event(conn, event_id)
                fresh = _freshness(conn, meta["dataset_version"])
                ids = _subset()
                if ids is not None:
                    rows = pd.read_sql(store.sa.text(f"SELECT * FROM {store.EVENT_BUILDINGS} WHERE event_id = :e"),
                                       conn, params={"e": event_id})
                    totals = _sum_by(rows[rows["buildingid"].isin(ids)].drop(columns=["event_id", "buildingid"]),
                                     "timestamp")
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        meta = {k: v for k, v in meta.items() if k not in ("run_id", "dataset_version")}
        if isinstance(meta.get("assumptions"), str):
            meta["assumptions"] = json.loads(meta["assumptions"])
        return jsonify({"status": "success", **fresh, "event": json.loads(pd.Series(meta).to_json()),
                        "partial_reach": ids is not None,
                        "timesteps": _json(totals.drop(columns=[c for c in ("event_id",) if c in totals.columns]))})

    @blueprint.route("/dynamic/event/<event_id>/<buildingid>.json", methods=["GET"])
    def dynamic_event_building(event_id, buildingid):
        blocked = _building_guard(buildingid)
        if blocked:
            return blocked
        try:
            with _engine().connect() as conn:
                meta, _ = store.read_event(conn, event_id)
                rows = store.read_event_building(conn, event_id, buildingid)
                fresh = _freshness(conn, meta["dataset_version"])
        except store.DynamicUnavailable as exc:
            return _unavailable(exc, NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return _unavailable(exc)
        if rows.empty:
            return jsonify({"status": "not_found", "found": False, "buildingid": buildingid}), NOT_FOUND
        rows = rows.drop(columns=["event_id"])
        result = {"status": "success", "found": True, **fresh, "event_id": event_id, "buildingid": buildingid}
        if "time" in request.args:
            try:
                when = pd.Timestamp(request.args["time"])
                when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
            except ValueError:
                return jsonify({"status": "error", "message": "Invalid time."}), BAD_REQUEST
            stamps = pd.to_datetime(rows["timestamp"], utc=True)
            before = rows[stamps <= when]
            result["value_at"] = _json(before.tail(1))[0] if not before.empty else None
        else:
            result["timesteps"] = _json(rows)
        return jsonify(result)

    # ------------------------------------------------------------------- start computations

    def _send(task, kwargs):
        from eddie.tasks import app  # pylint: disable=import-outside-toplevel
        task_id = app.send_task(f"eddie_flood_costs.tasks.{task}", kwargs=kwargs).id
        return jsonify({"status": "processing", "task_id": task_id, "parameters": kwargs}), ACCEPTED

    @blueprint.route("/dynamic/curve/run", methods=["POST"])
    def dynamic_curve_run():
        payload = request.get_json(silent=True) or {}
        kwargs = {k: str(payload[k]) for k in ("stages", "durations") if payload.get(k) not in (None, "")}
        return _send("run_dynamic_curve", kwargs)

    @blueprint.route("/dynamic/event/run", methods=["POST"])
    def dynamic_event_run():
        payload = request.get_json(silent=True) or {}
        try:
            jobs.parse_period(payload["start"], payload["end"])
        except (KeyError, ValueError) as exc:
            return jsonify({"status": "error", "message": f"Give a valid start and end: {exc}"}), BAD_REQUEST
        kwargs = {"start": str(payload["start"]), "end": str(payload["end"])}
        if payload.get("event_id"):
            if not EVENT_ID.match(str(payload["event_id"])):
                return jsonify({"status": "error", "message": "event_id: letters, digits and _.:- only."}), BAD_REQUEST
            kwargs["event_id"] = str(payload["event_id"])
        return _send("run_dynamic_event", kwargs)
