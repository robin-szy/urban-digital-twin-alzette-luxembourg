# -*- coding: utf-8 -*-
"""
Fetch gauge readings from the Héichwaasser API (inondations.lu, Administration de
la gestion de l'eau) and add them to water_level_history in the project's PostGIS.

Source facts (checked against the API):
- one reading every 15 minutes per station, published with about 30-60 minutes delay;
- the full history is kept, back to 2017-12-14 for every station;
- GET /stations/<id> returns the latest 100 readings (about 25 hours);
  GET /stations/<id>/start/<unix>/end/<unix> returns any period (a whole year in one call works).

Timestamps are stored the way eddie_water_level.tasks.load_water_level_period stores
them (the API's local-time stamps, labelled UTC), so rows from both writers match
and the period filters line up with the API's own.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import sqlalchemy as sa
from sqlalchemy import text


API_ROOT = os.environ.get("GAUGE_API_ROOT", "https://heichwaasser.lu/api/v1")

# Alzette (17) and its gauged tributaries: Attert (23), Eisch (13), Mamer (15), Wark (9).
RIVER_IDS = [int(value) for value in os.environ.get("GAUGE_RIVER_IDS", "17,23,13,15,9").split(",") if value.strip()]

HISTORY_TABLE = "water_level_history"

# One row per job: the time of the last successful collection (see refresh_if_stale).
STATUS_TABLE = "gauge_collection_status"
STATUS_JOB = "heichwaasser"

# When the gauge API / collector is used and the last successful collection is older than this
# (or unknown), everything the source keeps since then is fetched first.
REFRESH_AFTER = timedelta(days=int(os.environ.get("GAUGE_REFRESH_AFTER_DAYS", "7")))

# How far back the source keeps readings, if it ever stops keeping the full history
# (GAUGE_SOURCE_HISTORY_DAYS); unset: everything since HISTORY_START.
SOURCE_HISTORY_DAYS = os.environ.get("GAUGE_SOURCE_HISTORY_DAYS")

logger = logging.getLogger(__name__)

# First reading the source keeps.
HISTORY_START = datetime(2017, 12, 14, tzinfo=timezone.utc)

# An incremental run re-reads this much before the last stored reading (duplicates are skipped).
OVERLAP = timedelta(hours=2)

# Without any stored reading, an incremental run starts this far back; use backfill for more.
FIRST_RUN_WINDOW = timedelta(days=1)

# The API's stamps are local time labelled UTC, so the latest readings look up to 2 h in the future.
END_MARGIN = timedelta(hours=3)

BACKFILL_CHUNK = timedelta(days=90)

# Pause between API requests, to be gentle with the public service.
REQUEST_PAUSE_S = 0.5

REQUEST_TIMEOUT_S = 120


# ============================================================
# DATABASE
# ============================================================

def get_engine() -> sa.engine.Engine:
    """Engine for the project's PostGIS, from the same POSTGRES_* variables as the backend."""
    url = sa.engine.URL.create(
        "postgresql",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        database=os.environ["POSTGRES_DB"],
    )
    return sa.create_engine(url, pool_pre_ping=True)


def ensure_table(conn) -> None:
    """water_level_history as created by eddie_water_level.tasks, if it does not exist yet."""
    conn.execute(text(
        f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            station_id INTEGER NOT NULL,
            station_name TEXT NOT NULL,
            river_name TEXT NOT NULL,
            datetime TIMESTAMPTZ NOT NULL,
            water_level_cm DOUBLE PRECISION NOT NULL,
            trend TEXT,
            alert_level_1_cm DOUBLE PRECISION,
            alert_level_2_cm DOUBLE PRECISION,
            alert_status TEXT,
            source TEXT NOT NULL DEFAULT 'heichwaasser',
            geometry geometry(Point,4326) NOT NULL,
            PRIMARY KEY (station_id, datetime)
        )
        """
    ))
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_geometry ON {HISTORY_TABLE} USING GIST (geometry)"
    ))
    conn.execute(text(
        f"CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_datetime ON {HISTORY_TABLE} (datetime)"
    ))
    conn.commit()


def ensure_status_table(conn) -> None:
    """Table holding the time of the last successful collection, if it does not exist yet."""
    conn.execute(text(
        f"""
        CREATE TABLE IF NOT EXISTS {STATUS_TABLE} (
            job TEXT PRIMARY KEY,
            last_success TIMESTAMPTZ NOT NULL
        )
        """
    ))
    conn.commit()


def last_success(conn):
    """Time of the last successful collection, or None when never recorded."""
    ensure_status_table(conn)
    return conn.execute(
        text(f"SELECT last_success FROM {STATUS_TABLE} WHERE job = :job"), {"job": STATUS_JOB}
    ).scalar_one_or_none()


def _record_success(engine, when: datetime) -> None:
    with engine.connect() as conn:
        ensure_status_table(conn)
        conn.execute(text(
            f"""
            INSERT INTO {STATUS_TABLE} (job, last_success) VALUES (:job, :when)
            ON CONFLICT (job) DO UPDATE SET last_success = GREATEST({STATUS_TABLE}.last_success, EXCLUDED.last_success)
            """
        ), {"job": STATUS_JOB, "when": when})
        conn.commit()


def _succeeded(report: dict) -> bool:
    """A run succeeded when it reached the API for every station without an error."""
    stations = report.get("stations", [])
    return bool(stations) and not any(item.get("error") or item.get("errors") for item in stations)


def _last_stored(conn, station_id: int, before: datetime | None = None):
    """(datetime, water_level_cm) of the latest stored reading of a station, optionally before a time."""
    query = f"SELECT datetime, water_level_cm FROM {HISTORY_TABLE} WHERE station_id = :station_id"
    params = {"station_id": station_id}
    if before is not None:
        query += " AND datetime < :before"
        params["before"] = before
    row = conn.execute(text(query + " ORDER BY datetime DESC LIMIT 1"), params).first()
    return (row[0], row[1]) if row else (None, None)


def _count(conn, station_id: int) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {HISTORY_TABLE} WHERE station_id = :station_id"),
        {"station_id": station_id},
    ).scalar_one()


# ============================================================
# API
# ============================================================

def _get_json(path: str):
    response = requests.get(f"{API_ROOT}{path}", timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    time.sleep(REQUEST_PAUSE_S)
    payload = response.json()
    # The API answers with an object or a one-element list.
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload


def list_stations() -> list:
    """Every station of the configured rivers: id, city, river, longitude, latitude."""
    stations = []
    for river_id in RIVER_IDS:
        river = _get_json(f"/rivers/{river_id}")
        for station in river.get("stations", []):
            stations.append({
                "id": int(station["id"]),
                "city": station.get("city", f"station_{station['id']}"),
                "river": river.get("name", str(river_id)),
                "longitude": float(station["longitude"]),
                "latitude": float(station["latitude"]),
            })
    return stations


def _alert_levels(station_data: dict) -> tuple:
    """The two lowest alert thresholds (cm), as in eddie_water_level.tasks."""
    values = sorted(float(item["value"]) for item in station_data.get("alert_levels", []) if item.get("value") is not None)
    return (values[0] if values else None, values[1] if len(values) > 1 else None)


def _alert_status(value_cm: float, level_1, level_2) -> str:
    if level_2 is not None and value_cm >= level_2:
        return "ALERT_2"
    if level_1 is not None and value_cm >= level_1:
        return "ALERT_1"
    if level_1 is None and level_2 is None:
        return "NO_THRESHOLD"
    return "NORMAL"


def _as_utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def fetch_readings(station_id: int, start: datetime, end: datetime) -> tuple:
    """
    Readings of one station in [start, end].

    Returns
    -------
    tuple
        (station_data without measurements, sorted list of (datetime, value_cm)).
    """
    data = _get_json(f"/stations/{station_id}/start/{int(start.timestamp()) - 1}/end/{int(end.timestamp()) + 1}")
    readings = []
    for measurement in data.pop("measurements", None) or []:
        value, stamp = measurement.get("value"), measurement.get("timestamp")
        if value is None or stamp is None or float(value) < -1000:
            # -1000 and below are sentinel values, as in eddie_water_level.tasks.
            continue
        timestamp = _as_utc(stamp)
        if start <= timestamp <= end:
            readings.append((timestamp, float(value)))
    readings.sort()
    return data, readings


# ============================================================
# STORE
# ============================================================

def store_readings(conn, station: dict, station_data: dict, readings: list) -> int:
    """Insert readings, skipping the ones already stored. Returns the number of new rows."""
    if not readings:
        return 0
    level_1, level_2 = _alert_levels(station_data)
    river = station_data.get("river")
    river_name = river.get("name", station["river"]) if isinstance(river, dict) else station["river"]

    # Trend continues from the stored reading just before this batch.
    _, previous = _last_stored(conn, station["id"], before=readings[0][0])
    rows = []
    for timestamp, value in readings:
        if previous is None:
            trend = "unknown"
        elif value > previous:
            trend = "up"
        elif value < previous:
            trend = "down"
        else:
            trend = "rest"
        previous = value
        rows.append({
            "station_id": station["id"],
            "station_name": station_data.get("city", station["city"]),
            "river_name": river_name,
            "datetime": timestamp,
            "water_level_cm": value,
            "trend": trend,
            "alert_level_1_cm": level_1,
            "alert_level_2_cm": level_2,
            "alert_status": _alert_status(value, level_1, level_2),
            "longitude": float(station_data.get("longitude", station["longitude"])),
            "latitude": float(station_data.get("latitude", station["latitude"])),
        })

    before = _count(conn, station["id"])
    conn.execute(text(
        f"""
        INSERT INTO {HISTORY_TABLE} (
            station_id, station_name, river_name, datetime, water_level_cm, trend,
            alert_level_1_cm, alert_level_2_cm, alert_status, source, geometry
        )
        VALUES (
            :station_id, :station_name, :river_name, :datetime, :water_level_cm, :trend,
            :alert_level_1_cm, :alert_level_2_cm, :alert_status, 'heichwaasser',
            ST_SetSRID(ST_Point(:longitude, :latitude), 4326)
        )
        ON CONFLICT (station_id, datetime) DO NOTHING
        """
    ), rows)
    inserted = _count(conn, station["id"]) - before
    conn.commit()
    return inserted


# ============================================================
# RUNS
# ============================================================

def collect(engine=None, now: datetime | None = None) -> dict:
    """
    Incremental run: per station, from the last stored reading (minus OVERLAP) to now.

    Gaps heal themselves: however long since the last run, it continues from the last stored reading.
    """
    engine = engine or get_engine()
    now = now or datetime.now(timezone.utc)
    report = {"mode": "collect", "started": now.isoformat(), "stations": []}
    with engine.connect() as conn:
        ensure_table(conn)
        for station in list_stations():
            last, _ = _last_stored(conn, station["id"])
            start = last - OVERLAP if last else now - FIRST_RUN_WINDOW
            try:
                station_data, readings = fetch_readings(station["id"], start, now + END_MARGIN)
                inserted = store_readings(conn, station, station_data, readings)
                report["stations"].append({
                    "station_id": station["id"], "station": station["city"], "river": station["river"],
                    "fetched": len(readings), "inserted": inserted,
                })
            except requests.RequestException as exc:
                report["stations"].append({"station_id": station["id"], "station": station["city"], "error": str(exc)})
    report["inserted"] = sum(item.get("inserted", 0) for item in report["stations"])
    report["finished"] = datetime.now(timezone.utc).isoformat()
    if _succeeded(report):
        _record_success(engine, now)
    return report


def backfill(start: datetime | None = None, end: datetime | None = None, engine=None) -> dict:
    """
    Load the history in BACKFILL_CHUNK windows, by default everything the source keeps
    (HISTORY_START to now). Already stored readings are skipped, so it can be re-run or resumed.
    """
    engine = engine or get_engine()
    # Only a backfill up to now counts as a collection of the latest data.
    up_to_now = end is None
    started = datetime.now(timezone.utc)
    start = start or HISTORY_START
    end = end or datetime.now(timezone.utc) + END_MARGIN
    report = {"mode": "backfill", "start": start.isoformat(), "end": end.isoformat(), "stations": []}
    with engine.connect() as conn:
        ensure_table(conn)
        for station in list_stations():
            fetched = inserted = 0
            errors = []
            window_start = start
            while window_start < end:
                window_end = min(window_start + BACKFILL_CHUNK, end)
                try:
                    station_data, readings = fetch_readings(station["id"], window_start, window_end)
                    fetched += len(readings)
                    inserted += store_readings(conn, station, station_data, readings)
                except requests.RequestException as exc:
                    errors.append(f"{window_start.date()}..{window_end.date()}: {exc}")
                window_start = window_end
            print(f"[backfill] {station['river']} {station['city']}: {fetched} fetched, {inserted} new", flush=True)
            report["stations"].append({
                "station_id": station["id"], "station": station["city"], "river": station["river"],
                "fetched": fetched, "inserted": inserted, "errors": errors,
            })
    report["inserted"] = sum(item["inserted"] for item in report["stations"])
    if up_to_now and _succeeded(report):
        _record_success(engine, started)
    return report


def refresh_due(engine=None, now: datetime | None = None) -> bool:
    """True when the last successful collection is missing or older than REFRESH_AFTER."""
    engine = engine or get_engine()
    now = now or datetime.now(timezone.utc)
    with engine.connect() as conn:
        last = last_success(conn)
    return last is None or now - last > REFRESH_AFTER


def refresh_if_stale(engine=None, now: datetime | None = None) -> dict | None:
    """
    Weekly refresh, run whenever the gauge API or collector is used.

    When the last successful collection (STATUS_TABLE) is missing or older than REFRESH_AFTER,
    load everything the source keeps since then with backfill (duplicates are skipped; without
    a recorded date, the whole history). backfill records the new date only if every station
    succeeded, so a failed refresh is tried again on the next use.

    Returns
    -------
    dict | None
        The backfill report, or None when no refresh was due.
    """
    engine = engine or get_engine()
    now = now or datetime.now(timezone.utc)
    with engine.connect() as conn:
        last = last_success(conn)
    if last is not None and now - last <= REFRESH_AFTER:
        return None

    oldest = HISTORY_START
    if SOURCE_HISTORY_DAYS:
        oldest = max(oldest, now - timedelta(days=float(SOURCE_HISTORY_DAYS)))
    start = last - OVERLAP if last is not None else HISTORY_START
    if start < oldest:
        if last is not None:
            logger.warning(
                "Gauge data: the last successful collection was %s, but the source only keeps readings "
                "since %s: the readings from %s to %s could not be recovered.",
                last.isoformat(), oldest.isoformat(), last.date(), oldest.date(),
            )
        start = oldest

    logger.warning(
        "Gauge data: last successful collection %s; refreshing everything the source keeps since %s.",
        last.isoformat() if last is not None else "never recorded", start.date(),
    )
    report = backfill(start, None, engine)
    report["mode"] = "refresh"
    if _succeeded(report):
        print(f"[refresh] {report['inserted']} new readings since {start.date()}", flush=True)
    else:
        failed = [item for item in report["stations"] if item.get("errors")]
        logger.warning(
            "Gauge data refresh failed; the last collection date was not updated and the refresh "
            "runs again on the next use. Failed: %s",
            failed or "no station listed by the API",
        )
    return report


def status(engine=None) -> list:
    """Per stored station: number of readings, first and last reading."""
    engine = engine or get_engine()
    with engine.connect() as conn:
        ensure_table(conn)
        rows = conn.execute(text(
            f"""
            SELECT station_id, MIN(station_name) AS station_name, MIN(river_name) AS river_name,
                   COUNT(*) AS readings, MIN(datetime) AS first, MAX(datetime) AS last
            FROM {HISTORY_TABLE}
            GROUP BY station_id
            ORDER BY MIN(river_name), station_id
            """
        )).mappings().all()
    return [{**row, "first": row["first"].isoformat(), "last": row["last"].isoformat()} for row in rows]
