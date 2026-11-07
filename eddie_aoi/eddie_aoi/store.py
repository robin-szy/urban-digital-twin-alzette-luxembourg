"""Persisting and reusing reaches.

A reach is expensive to build - reading the marker and mapped-river sources,
splitting segments, routing, then validating every marker - and cheap to
reuse. This is where that saving lives; variant derivation off a cached
centerline is milliseconds by comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from shapely import wkt as shapely_wkt
from sqlalchemy.dialects.postgresql import insert as pg_insert

from eddie_aoi.db import (
    METRIC_SRID,
    STATUS_BUILDING,
    STATUS_FAILED,
    STATUS_READY,
    reach_cache,
)
from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import Reach
from eddie_aoi.reach import reach_id as make_reach_id


class ReachNotReady(PreparationError):
    """A reach exists in the cache but must not be served."""

    def __init__(self, reach_id: str, status: str, error: str | None = None):
        self.reach_id = reach_id
        self.status = status
        self.error = error
        if status == STATUS_FAILED:
            super().__init__(f"Reach {reach_id} failed to build: {error}")
        else:
            super().__init__(f"Reach {reach_id} is still building.")


@dataclass(frozen=True)
class CachedReach:
    """A reach cache row. ``reach`` is set only when the row is ready."""

    reach_id: str
    river: str
    km_start: float
    km_end: float
    status: str
    source_sha256: str | None
    pinned: bool
    error: str | None
    reach: Reach | None

    @property
    def is_ready(self) -> bool:
        return self.status == STATUS_READY and self.reach is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert_status(
    conn: sa.Connection,
    river: str,
    km_start: float,
    km_end: float,
    status: str,
    error: str | None,
) -> str:
    identifier = make_reach_id(river, km_start, km_end)
    now = _now()
    stmt = pg_insert(reach_cache).values(
        reach_id=identifier,
        river=river,
        km_start=km_start,
        km_end=km_end,
        status=status,
        error=error,
        last_used_at=now,
    )
    update = {"status": status, "error": error, "last_used_at": now}
    if status != STATUS_READY:
        # A reach that is building or failed must not keep serving old geometry.
        update["centerline"] = None
    conn.execute(stmt.on_conflict_do_update(index_elements=["reach_id"], set_=update))
    return identifier


def mark_building(
    conn: sa.Connection, river: str, km_start: float, km_end: float
) -> str:
    """Record that a reach is being built, before the work starts."""
    return _upsert_status(conn, river, km_start, km_end, STATUS_BUILDING, None)


def mark_failed(
    conn: sa.Connection, river: str, km_start: float, km_end: float, error: str
) -> str:
    """Record why a reach could not be built, so a repeat request says so."""
    return _upsert_status(conn, river, km_start, km_end, STATUS_FAILED, error)


def save_reach(conn: sa.Connection, reach: Reach, *, pinned: bool = False) -> str:
    """Persist a validated reach as ready."""
    provenance = dict(reach.provenance)
    now = _now()
    values = {
        "reach_id": reach.reach_id,
        "river": reach.river,
        "km_start": reach.km_start,
        "km_end": reach.km_end,
        "centerline": sa.func.ST_GeomFromText(reach.centerline.wkt, METRIC_SRID),
        "marker_km": list(reach.marker_km),
        "marker_positions": list(reach.marker_positions),
        "route_length_m": provenance.get("route_length_m"),
        "marker_max_offset_m": provenance.get("marker_max_offset_m"),
        "markers_in_order": provenance.get("markers_in_order"),
        "provenance": provenance,
        "source_sha256": provenance.get("source_sha256"),
        "status": STATUS_READY,
        "error": None,
        "built_at": now,
        "last_used_at": now,
    }
    update = {key: value for key, value in values.items() if key != "reach_id"}
    if pinned:
        values["pinned"] = True
        update["pinned"] = True

    stmt = pg_insert(reach_cache).values(**values)
    conn.execute(stmt.on_conflict_do_update(index_elements=["reach_id"], set_=update))
    return reach.reach_id


def read_cached(conn: sa.Connection, identifier: str) -> CachedReach | None:
    """The cache row for a reach identifier, geometry included when ready.

    Geometry comes back as WKT so the decode does not depend on how the
    driver happens to represent a PostGIS value.
    """
    row = conn.execute(
        sa.select(
            reach_cache.c.reach_id,
            reach_cache.c.river,
            reach_cache.c.km_start,
            reach_cache.c.km_end,
            reach_cache.c.marker_km,
            reach_cache.c.marker_positions,
            reach_cache.c.provenance,
            reach_cache.c.source_sha256,
            reach_cache.c.status,
            reach_cache.c.error,
            reach_cache.c.pinned,
            sa.func.ST_AsText(reach_cache.c.centerline).label("centerline_wkt"),
        ).where(reach_cache.c.reach_id == identifier)
    ).one_or_none()

    if row is None:
        return None

    reach = None
    if row.status == STATUS_READY and row.centerline_wkt:
        reach = Reach(
            river=row.river,
            km_start=float(row.km_start),
            km_end=float(row.km_end),
            centerline=shapely_wkt.loads(row.centerline_wkt),
            marker_km=tuple(row.marker_km or ()),
            marker_positions=tuple(row.marker_positions or ()),
            provenance=dict(row.provenance or {}),
        )

    return CachedReach(
        reach_id=row.reach_id,
        river=row.river,
        km_start=float(row.km_start),
        km_end=float(row.km_end),
        status=row.status,
        source_sha256=row.source_sha256,
        pinned=bool(row.pinned),
        error=row.error,
        reach=reach,
    )


def touch(conn: sa.Connection, identifier: str) -> None:
    """Record that a reach was used, for last-recently-used eviction."""
    conn.execute(
        sa.update(reach_cache)
        .where(reach_cache.c.reach_id == identifier)
        .values(last_used_at=_now())
    )


def set_pinned(conn: sa.Connection, identifier: str, pinned: bool = True) -> None:
    """Pin or unpin a reach. A pinned reach is never evicted."""
    conn.execute(
        sa.update(reach_cache)
        .where(reach_cache.c.reach_id == identifier)
        .values(pinned=pinned)
    )


def is_stale(cached: CachedReach, current_source_sha256: str | None) -> bool:
    """Whether a cached reach was built from superseded source data.

    A hash comparison rather than a timestamp: a re-downloaded but identical
    layer must not invalidate, and a layer edited in place with a preserved
    modification time must.
    """
    if not cached.is_ready:
        return False
    if current_source_sha256 is None or cached.source_sha256 is None:
        # Nothing to compare against; treat as current rather than rebuilding
        # on every request.
        return False
    return cached.source_sha256 != current_source_sha256


def get_or_build(
    conn: sa.Connection,
    river: str,
    km_start: float,
    km_end: float,
    build,
    *,
    current_source_sha256: str | None = None,
) -> Reach:
    """Serve a cached reach, or build and persist one.

    ``build`` is a zero-argument callable returning a validated ``Reach``.
    A cached row that is building or failed is never served as if it were
    ready - the recorded reason is raised instead.
    """
    identifier = make_reach_id(river, km_start, km_end)
    cached = read_cached(conn, identifier)

    if cached is not None:
        if cached.status in (STATUS_BUILDING, STATUS_FAILED):
            raise ReachNotReady(identifier, cached.status, cached.error)
        if cached.is_ready and not is_stale(cached, current_source_sha256):
            touch(conn, identifier)
            return cached.reach

    mark_building(conn, river, km_start, km_end)
    try:
        reach = build()
    except Exception as exc:
        mark_failed(conn, river, km_start, km_end, str(exc))
        raise
    save_reach(conn, reach)
    return reach
