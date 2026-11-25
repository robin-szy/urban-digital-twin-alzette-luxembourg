"""Celery tasks of the weather module and their schedule. The only file that imports ``eddie``.

Schedule (UTC):

* every hour at :20 - ICON forecast, last week to a week ahead (provisional);
* every hour at :35 - river flow of the tuned gauges;
* every hour at :45 - one year of the reanalysis backfill, while the daily budget allows;
* every day at 07:10 - reanalysis catch-up (final values replace provisional ones);
* when a worker starts - catch-up, then forecast and flow, so a restart fills its own gap.

Each task holds a database lock while it runs, so a slow run is never doubled by the next
one; it then simply skips. Each has a time limit, so a stuck request cannot block a worker.
"""

from __future__ import annotations

import contextlib
import logging
import zlib
from typing import Iterator, Optional

from celery import signals
from celery.schedules import crontab
import sqlalchemy as sa

from eddie.digitaltwin import setup_environment
from eddie.tasks import app

from eddie_weather import catchment, db, pipeline, sources

log = logging.getLogger(__name__)

# One budget per worker process. It stays below the service's limits; if several workers
# run these tasks, the service's own limit (HTTP 429) stops the backfill cleanly as well.
BUDGET = sources.CallBudget()


@contextlib.contextmanager
def _locked(name: str) -> Iterator[Optional[sa.engine.Connection]]:
    """A connection holding a lock named ``name``; ``None`` if another run holds it.

    The connection works in transactions: the task's writes are saved when it ends without an
    error, or earlier where it calls ``conn.commit()`` (the archive saves after each month).
    The lock is a session lock, so it lasts across those saves until the task ends.
    """
    engine = setup_environment.get_database()
    with engine.begin() as conn:
        db.ensure_tables(conn)
    key = zlib.crc32(f"eddie_weather.{name}".encode()) & 0x7FFFFFFF
    # EDDIE's engine runs in AUTOCOMMIT (every statement saved at once); this connection is
    # switched back to transactions, so a failed step leaves nothing half-written.
    with engine.connect() as raw:
        conn = raw.execution_options(isolation_level="READ COMMITTED")
        got = conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        conn.commit()
        if not got:
            log.info("eddie_weather %s already running: skipped", name)
            yield None
            return
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            try:
                conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
            except Exception:  # pylint: disable=broad-except
                pass  # a broken connection releases its session locks when it closes


def _skipped(name: str) -> dict:
    return {"status": "skipped", "reason": f"{name} already running"}


@app.task(name="eddie_weather.tasks.setup_places", soft_time_limit=600, time_limit=660)
def setup_places(path: str, id_field: Optional[str] = None, step_deg: float = catchment.ERA5_LAND_STEP_DEG) -> dict:
    """Read the gauge catchments (CAMELS-LUX shapefile or GeoJSON) and store the places.

    ``id_field`` is the column holding the CAMELS-LUX gauge number (``gauge_id``, ``ID``,
    ``ID_3``...); by default the first column whose name contains "id".
    """
    import re  # pylint: disable=import-outside-toplevel
    import geopandas as gpd  # pylint: disable=import-outside-toplevel

    frame = gpd.read_file(path)
    frame = frame.to_crs(4326) if frame.crs is not None else frame
    if id_field is None:
        id_field = next((c for c in frame.columns if "id" in c.lower() and c != "geometry"), None)
        if id_field is None:
            raise ValueError(f"no id column in {path}: give id_field (columns: {list(frame.columns)})")
    outlines = {}
    for _, row in frame.iterrows():
        digits = re.findall(r"\d+", str(row[id_field]))
        if digits:
            outlines[int(digits[-1])] = row.geometry
    with _locked("setup") as conn:
        if conn is None:
            return _skipped("setup")
        places = pipeline.setup_places(conn, outlines, step_deg)
        return {"status": "success", "places": places.names, "points": len(places.points)}


@app.task(name="eddie_weather.tasks.update_forecast", soft_time_limit=300, time_limit=360)
def update_forecast() -> dict:
    with _locked("forecast") as conn:
        if conn is None:
            return _skipped("forecast")
        try:
            report = pipeline.update_forecast(conn, pipeline.load_places(conn), budget=BUDGET)
        except sources.SourceError as error:
            conn.rollback()
            log.warning("eddie_weather forecast stopped: %s", error)
            return {"status": "stopped", "reason": str(error)}
        return {"status": "success", **report}


@app.task(name="eddie_weather.tasks.catch_up", soft_time_limit=1800, time_limit=1900)
def catch_up() -> dict:
    with _locked("archive") as conn:
        if conn is None:
            return _skipped("archive")
        try:
            report = pipeline.catch_up(conn, pipeline.load_places(conn), budget=BUDGET, checkpoint=conn.commit)
        except sources.SourceError as error:
            # The months already fetched are saved; the next run continues from them.
            conn.rollback()
            log.warning("eddie_weather catch-up stopped: %s", error)
            return {"status": "stopped", "reason": str(error)}
        return {"status": "success", **report}


@app.task(name="eddie_weather.tasks.backfill", soft_time_limit=1800, time_limit=1900)
def backfill(first_year: int = 1950, max_chunks: int = 1) -> dict:
    with _locked("archive") as conn:
        if conn is None:
            return _skipped("archive")
        return {"status": "success", **pipeline.backfill(conn, pipeline.load_places(conn), first_year,
                                                         budget=BUDGET, max_chunks=max_chunks,
                                                         checkpoint=conn.commit)}


@app.task(name="eddie_weather.tasks.update_flow", soft_time_limit=600, time_limit=660)
def update_flow() -> dict:
    with _locked("flow") as conn:
        if conn is None:
            return _skipped("flow")
        report = pipeline.update_flow(conn, pipeline.load_places(conn))
        return {"status": "success", "gauges": {str(k): v for k, v in report.items()}}


@app.task(name="eddie_weather.tasks.calibrate", soft_time_limit=3600, time_limit=3700)
def calibrate(gauge_id: int, flow_csv: str, iterations: int = 1500) -> dict:
    """Tune GR4J for one gauge on a CAMELS-LUX daily file (columns: date and Q in m3/s)."""
    from eddie_weather import camels  # pylint: disable=import-outside-toplevel

    observed = camels.read_daily_flow(flow_csv)
    with _locked(f"calibrate-{gauge_id}") as conn:
        if conn is None:
            return _skipped("calibration")
        return {"status": "success", **pipeline.calibrate_gauge(conn, pipeline.load_places(conn), gauge_id,
                                                                observed, iterations=iterations)}


def _has_places() -> bool:
    engine = setup_environment.get_database()
    with engine.begin() as conn:
        db.ensure_tables(conn)
        return bool(db.read_places(conn, "sub_catchment"))


@signals.worker_ready.connect
def catch_up_on_startup(sender, **_kwargs) -> None:
    """A restarted worker first fills the gap it left: archive, then forecast, then flow."""
    try:
        if not _has_places():
            log.info("eddie_weather: no places yet, nothing to catch up")
            return
    except Exception as error:  # pylint: disable=broad-except
        log.warning("eddie_weather: startup check failed: %s", error)
        return
    (catch_up.si() | update_forecast.si() | update_flow.si()).apply_async()


app.conf.beat_schedule.update({
    "eddie-weather-forecast-hourly": {
        "task": "eddie_weather.tasks.update_forecast",
        "schedule": crontab(minute=20),
        "options": {"expires": 1800},
    },
    "eddie-weather-flow-hourly": {
        "task": "eddie_weather.tasks.update_flow",
        "schedule": crontab(minute=35),
        "options": {"expires": 1800},
    },
    "eddie-weather-backfill-hourly": {
        "task": "eddie_weather.tasks.backfill",
        "schedule": crontab(minute=45),
        "kwargs": {"max_chunks": 1},
        "options": {"expires": 1800},
    },
    "eddie-weather-catch-up-daily": {
        "task": "eddie_weather.tasks.catch_up",
        "schedule": crontab(hour=7, minute=10),
        "options": {"expires": 3600 * 6},
    },
})
