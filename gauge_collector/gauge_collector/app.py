# -*- coding: utf-8 -*-
"""
HTTP API of the gauge collector.

    GET  /          service info and the state of the last runs
    GET  /status    readings stored per station (count, first, last)
    POST /collect   incremental run now (returns its report)
    POST /backfill  load the history in the background (?start=YYYY-MM-DD&end=YYYY-MM-DD,
                    default: everything the source keeps); follow it on GET /

Each request (and each scheduled run) first checks the date of the last successful collection,
stored in the database; when it is missing or older than 7 days, a refresh of everything the
source keeps since then starts in the background (collector.refresh_if_stale).

While the service runs, an incremental run also starts every COLLECT_INTERVAL_MIN
minutes (default 15, the source's update interval; 0 switches it off).
"""

import os
import threading
import traceback
from datetime import datetime, timezone
from http.client import ACCEPTED, BAD_REQUEST, CONFLICT

from flask import Flask, jsonify, request

from gauge_collector import collector


COLLECT_INTERVAL_MIN = float(os.environ.get("COLLECT_INTERVAL_MIN", "15"))

app = Flask(__name__)

# One run at a time: an incremental run and a backfill would only fetch the same readings twice.
_job_lock = threading.Lock()
_state = {"running": None, "last_collect": None, "last_backfill": None, "last_refresh": None}


def _run(kind: str, function, *args) -> dict:
    with _job_lock:
        _state["running"] = kind
        try:
            report = function(*args)
        except Exception as exc:  # pylint: disable=broad-except
            traceback.print_exc()
            report = {"mode": kind, "error": str(exc), "finished": datetime.now(timezone.utc).isoformat()}
        finally:
            _state["running"] = None
        _state[f"last_{kind}"] = report
        return report


def _start_refresh_if_due() -> None:
    """Start the weekly refresh in the background when it is due and no other run is going on."""
    if _job_lock.locked():
        return
    try:
        due = collector.refresh_due()
    except Exception:  # pylint: disable=broad-except
        # The database may be unreachable; the request itself reports that.
        traceback.print_exc()
        return
    if due:
        threading.Thread(target=_run, args=("refresh", collector.refresh_if_stale), daemon=True).start()


@app.before_request
def _check_refresh() -> None:
    _start_refresh_if_due()


def _scheduler() -> None:
    stop = threading.Event()
    while not stop.wait(COLLECT_INTERVAL_MIN * 60):
        _start_refresh_if_due()
        if not _job_lock.locked():
            report = _run("collect", collector.collect)
            print(f"[collect] {report.get('inserted', 0)} new readings {report.get('error', '')}", flush=True)


def _parse_day(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


@app.get("/")
def info():
    return jsonify({
        "service": "gauge_collector",
        "source": collector.API_ROOT,
        "river_ids": collector.RIVER_IDS,
        "table": collector.HISTORY_TABLE,
        "collect_interval_min": COLLECT_INTERVAL_MIN,
        **_state,
    })


@app.get("/status")
def stations_status():
    return jsonify(collector.status())


@app.post("/collect")
def collect_now():
    if _job_lock.locked():
        return jsonify({"status": "busy", "running": _state["running"]}), CONFLICT
    return jsonify(_run("collect", collector.collect))


@app.post("/backfill")
def backfill():
    if _job_lock.locked():
        return jsonify({"status": "busy", "running": _state["running"]}), CONFLICT
    try:
        start, end = _parse_day(request.args.get("start")), _parse_day(request.args.get("end"))
    except ValueError:
        return jsonify({"status": "error", "message": "Use start=YYYY-MM-DD and end=YYYY-MM-DD."}), BAD_REQUEST
    threading.Thread(target=_run, args=("backfill", collector.backfill, start, end), daemon=True).start()
    return jsonify({"status": "started", "start": (start or collector.HISTORY_START).isoformat(), "follow": "GET /"}), ACCEPTED


if COLLECT_INTERVAL_MIN > 0:
    threading.Thread(target=_scheduler, daemon=True).start()
