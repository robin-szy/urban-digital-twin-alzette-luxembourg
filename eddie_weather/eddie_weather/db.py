"""Storage of the weather module: four tables in the twin's PostGIS database.

* ``weather_places``: the areas (sub-catchments and gauge catchments) with their outline.
* ``weather_values``: every value in one shape: place, variable, level, time (UTC, start of
  the period), value, unit, quality (provisional or final), source, complete.
* ``weather_models``: the tuned GR4J of each gauge, its scores, and its saved state.
* ``weather_jobs``: what the backfill has done, chunk by chunk, so it resumes after a restart.

A final value is never overwritten by a provisional one. No ``eddie`` import: the tests run
on SQLite; only the map geometry column needs PostGIS.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Connection

PLACES = "weather_places"
VALUES = "weather_values"
MODELS = "weather_models"
JOBS = "weather_jobs"

PROVISIONAL, FINAL = "provisional", "final"

metadata = sa.MetaData()

places_table = sa.Table(
    PLACES, metadata,
    sa.Column("place_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),  # sub_catchment | gauge_catchment
    sa.Column("gauge_id", sa.Integer),
    sa.Column("area_km2", sa.Float, nullable=False),
    sa.Column("geojson", sa.Text, nullable=False),
    sa.Column("weights", sa.Text),  # JSON: {"points": [[lat, lon], ...], "weights": [...]}
)

values_table = sa.Table(
    VALUES, metadata,
    sa.Column("place_id", sa.Text, primary_key=True),
    sa.Column("variable", sa.Text, primary_key=True),
    sa.Column("level", sa.Text, primary_key=True),
    sa.Column("time_utc", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("value", sa.Float),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("quality", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("complete", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

models_table = sa.Table(
    MODELS, metadata,
    sa.Column("gauge_id", sa.Integer, primary_key=True),
    sa.Column("params", sa.Text, nullable=False),  # JSON {"x1":..}
    sa.Column("scores", sa.Text, nullable=False),  # JSON {"calibration": {...}, "validation": {...}}
    sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.Text),  # JSON GR4JState at the end of state_day
    sa.Column("state_day", sa.Date),
)

jobs_table = sa.Table(
    JOBS, metadata,
    sa.Column("job", sa.Text, primary_key=True),
    sa.Column("chunk", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),  # running | done | failed
    sa.Column("rows", sa.Integer, default=0),
    sa.Column("message", sa.Text),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _insert(conn: Connection):
    if conn.engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    return insert


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc(value) -> dt.datetime:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime()


def ensure_tables(conn: Connection) -> None:
    metadata.create_all(conn, checkfirst=True)
    if conn.engine.dialect.name == "postgresql":
        # The outline as a real geometry too, so GeoServer can publish the places as a layer.
        conn.execute(sa.text(
            f"ALTER TABLE {PLACES} ADD COLUMN IF NOT EXISTS geometry geometry(MultiPolygon, 4326)"))
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {VALUES}_by_variable ON {VALUES} (variable, level, time_utc)"))


# --- places -------------------------------------------------------------------------------

def upsert_places(conn: Connection, rows: Sequence[dict]) -> int:
    """rows: place_id, name, kind, gauge_id, area_km2, geojson (dict), weights (dict or None)."""
    if not rows:
        return 0
    records = [dict(r, geojson=json.dumps(r["geojson"]),
                    weights=None if r.get("weights") is None else json.dumps(r["weights"])) for r in rows]
    stmt = _insert(conn)(places_table).values(records)
    stmt = stmt.on_conflict_do_update(index_elements=["place_id"], set_={
        c: getattr(stmt.excluded, c) for c in ("name", "kind", "gauge_id", "area_km2", "geojson", "weights")})
    conn.execute(stmt)
    if conn.engine.dialect.name == "postgresql":
        conn.execute(sa.text(
            f"UPDATE {PLACES} SET geometry = ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(geojson), 4326))"))
    return len(records)


def read_places(conn: Connection, kind: Optional[str] = None) -> List[dict]:
    query = sa.select(places_table).order_by(places_table.c.gauge_id, places_table.c.place_id)
    if kind:
        query = query.where(places_table.c.kind == kind)
    out = []
    for row in conn.execute(query).mappings():
        item = dict(row)
        item["geojson"] = json.loads(item["geojson"])
        item["weights"] = json.loads(item["weights"]) if item["weights"] else None
        out.append(item)
    return out


# --- values -------------------------------------------------------------------------------

def write_values(conn: Connection, frame: pd.DataFrame, variable: str, level: str, unit: str,
                 quality: str, source: str, complete: Optional[pd.DataFrame] = None,
                 chunk_size: int = 5000) -> int:
    """Store a table of values (times UTC x places). NaN cells are not written.

    A final value replaces anything; a provisional value replaces only a provisional one.
    """
    if quality not in (PROVISIONAL, FINAL):
        raise ValueError("quality must be provisional or final")
    if frame.empty:
        return 0
    now = _now()
    records = []
    for place in frame.columns:
        column = frame[place]
        flags = complete[place] if complete is not None else None
        for time, value in column.items():
            if value is None or (isinstance(value, float) and np.isnan(value)):
                continue
            records.append({
                "place_id": str(place), "variable": variable, "level": level, "time_utc": _utc(time),
                "value": float(value), "unit": unit, "quality": quality, "source": source,
                "complete": True if flags is None else bool(flags.loc[time]), "updated_at": now,
            })
    stmt = _insert(conn)(values_table)
    replace = {c: getattr(stmt.excluded, c) for c in ("value", "unit", "quality", "source", "complete", "updated_at")}
    stmt = stmt.on_conflict_do_update(
        index_elements=["place_id", "variable", "level", "time_utc"], set_=replace,
        where=sa.or_(stmt.excluded.quality == FINAL, values_table.c.quality == PROVISIONAL))
    for i in range(0, len(records), chunk_size):
        conn.execute(stmt, records[i:i + chunk_size])  # executemany: batched by the driver
    return len(records)


def read_values(conn: Connection, places: Sequence[str], variable: str, level: str,
                start=None, end=None, quality: Optional[str] = None) -> pd.DataFrame:
    """Long table: place_id, time_utc (UTC), value, unit, quality, source, complete.

    ``start`` included, ``end`` excluded.
    """
    t = values_table
    query = sa.select(t.c.place_id, t.c.time_utc, t.c.value, t.c.unit, t.c.quality, t.c.source, t.c.complete) \
        .where(t.c.place_id.in_(list(places)), t.c.variable == variable, t.c.level == level) \
        .order_by(t.c.place_id, t.c.time_utc)
    if start is not None:
        query = query.where(t.c.time_utc >= _utc(start))
    if end is not None:
        query = query.where(t.c.time_utc < _utc(end))
    if quality:
        query = query.where(t.c.quality == quality)
    frame = pd.DataFrame(conn.execute(query).mappings().all(),
                         columns=["place_id", "time_utc", "value", "unit", "quality", "source", "complete"])
    frame["time_utc"] = pd.to_datetime(frame["time_utc"], utc=True)
    return frame


def wide(frame: pd.DataFrame, places: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Long table from :func:`read_values` -> times x places."""
    table = frame.pivot(index="time_utc", columns="place_id", values="value") if not frame.empty \
        else pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    return table.reindex(columns=list(places)) if places is not None else table


def last_time(conn: Connection, variable: str, level: str, quality: Optional[str] = None,
              place: Optional[str] = None) -> Optional[pd.Timestamp]:
    t = values_table
    query = sa.select(sa.func.max(t.c.time_utc)).where(t.c.variable == variable, t.c.level == level)
    if quality:
        query = query.where(t.c.quality == quality)
    if place:
        query = query.where(t.c.place_id == place)
    value = conn.execute(query).scalar()
    return None if value is None else pd.Timestamp(value).tz_localize("UTC") if pd.Timestamp(value).tzinfo is None \
        else pd.Timestamp(value).tz_convert("UTC")


def delete_values(conn: Connection, level: str, before, quality: Optional[str] = None) -> int:
    """Drop old values of a level (the 15-minute values are kept for a week only)."""
    t = values_table
    stmt = sa.delete(t).where(t.c.level == level, t.c.time_utc < _utc(before))
    if quality:
        stmt = stmt.where(t.c.quality == quality)
    return conn.execute(stmt).rowcount or 0


def coverage(conn: Connection) -> List[dict]:
    """For the status page: first and last time, and rows, per variable, level and quality."""
    t = values_table
    query = sa.select(t.c.variable, t.c.level, t.c.quality, sa.func.min(t.c.time_utc), sa.func.max(t.c.time_utc),
                      sa.func.count()).group_by(t.c.variable, t.c.level, t.c.quality) \
        .order_by(t.c.variable, t.c.level, t.c.quality)
    return [{"variable": v, "level": lv, "quality": q, "first": str(a), "last": str(b), "rows": n}
            for v, lv, q, a, b, n in conn.execute(query)]


# --- models -------------------------------------------------------------------------------

def save_model(conn: Connection, gauge_id: int, params: Dict[str, float], scores: dict) -> None:
    stmt = _insert(conn)(models_table).values(gauge_id=gauge_id, params=json.dumps(params),
                                              scores=json.dumps(scores), calibrated_at=_now(),
                                              state=None, state_day=None)
    stmt = stmt.on_conflict_do_update(index_elements=["gauge_id"], set_={
        "params": stmt.excluded.params, "scores": stmt.excluded.scores,
        "calibrated_at": stmt.excluded.calibrated_at, "state": None, "state_day": None})
    conn.execute(stmt)


def save_state(conn: Connection, gauge_id: int, state: dict, day: dt.date) -> None:
    conn.execute(sa.update(models_table).where(models_table.c.gauge_id == gauge_id)
                 .values(state=json.dumps(state), state_day=day))


def read_models(conn: Connection) -> Dict[int, dict]:
    out = {}
    for row in conn.execute(sa.select(models_table)).mappings():
        item = dict(row)
        item["params"] = json.loads(item["params"])
        item["scores"] = json.loads(item["scores"])
        item["state"] = json.loads(item["state"]) if item["state"] else None
        out[item["gauge_id"]] = item
    return out


# --- jobs ---------------------------------------------------------------------------------

def mark_job(conn: Connection, job: str, chunk: str, status: str, rows: int = 0, message: str = "") -> None:
    stmt = _insert(conn)(jobs_table).values(job=job, chunk=chunk, status=status, rows=rows,
                                            message=message[:2000], updated_at=_now())
    stmt = stmt.on_conflict_do_update(index_elements=["job", "chunk"], set_={
        "status": stmt.excluded.status, "rows": stmt.excluded.rows,
        "message": stmt.excluded.message, "updated_at": stmt.excluded.updated_at})
    conn.execute(stmt)


def done_chunks(conn: Connection, job: str) -> set:
    query = sa.select(jobs_table.c.chunk).where(jobs_table.c.job == job, jobs_table.c.status == "done")
    return {r[0] for r in conn.execute(query)}


def read_jobs(conn: Connection) -> List[dict]:
    query = sa.select(jobs_table).order_by(jobs_table.c.job, jobs_table.c.chunk)
    return [dict(r, updated_at=str(r["updated_at"])) for r in conn.execute(query).mappings()]
