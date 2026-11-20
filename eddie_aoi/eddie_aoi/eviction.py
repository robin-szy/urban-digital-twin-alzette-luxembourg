"""Removing reaches nobody has used for a while (tasks 11.1, 11.2).

Runs ONLY when explicitly requested (``POST /aoi/evict``) - nothing schedules it.

A reach unused for longer than the requested age is removed together with:

* its AOI polygons (``aoi_variant_cache``, by the foreign key's ON DELETE CASCADE), and
* every derived dataset row computed for it - from every table carrying ``reach_id`` and
  ``variant_key`` (the columns ``eddie_base_data.publish.write_derived`` stamps), in the same
  transaction. With automatic recompute on selection every selected reach gets derived rows,
  so keeping "referenced" reaches would mean eviction removes almost nothing (team decision,
  2026-11-20). The rows in scope are recomputable from the base tables; revisit this before
  an expensive-to-recompute dataset (precipitation history) is attributed to reaches.

Never removed, and reported as retained with the reason:

* the pinned fallback reach, and
* the current reach (the database refuses it too: ``aoi_current`` references it with
  ON DELETE RESTRICT).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from eddie_aoi.current import current_reach
from eddie_aoi.db import reach_cache

#: The AOI module's own tables also carry reach_id; they are not derived datasets.
AOI_TABLES = frozenset({"reach_cache", "aoi_variant_cache", "aoi_current"})


def derived_tables(conn: sa.Connection) -> list[str]:
    """Every table holding rows attributed to a reach and variant."""
    rows = conn.execute(sa.text("""
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
          AND c.column_name IN ('reach_id', 'variant_key')
        GROUP BY c.table_name
        HAVING count(DISTINCT c.column_name) = 2
        ORDER BY c.table_name""")).scalars().all()
    return [name for name in rows if name not in AOI_TABLES]


def evict(
    conn: sa.Connection,
    older_than: timedelta,
    *,
    now: datetime | None = None,
    env: dict | None = None,
) -> dict:
    """Remove reaches unused for longer than ``older_than``; report what went and what stayed."""
    if older_than <= timedelta(0):
        raise ValueError("older_than must be a positive age.")
    now = now or datetime.now(timezone.utc)
    cutoff = now - older_than
    current = current_reach(conn, env).reach_id
    tables = derived_tables(conn)

    candidates = conn.execute(
        sa.select(reach_cache.c.reach_id, reach_cache.c.pinned, reach_cache.c.last_used_at)
        .where(reach_cache.c.last_used_at < cutoff)
        .order_by(reach_cache.c.last_used_at)
    ).all()

    removed, retained = [], []
    for row in candidates:
        if row.pinned:
            retained.append({"reach_id": row.reach_id, "reason": "pinned (the configured fallback reach)"})
            continue
        if row.reach_id == current:
            retained.append({"reach_id": row.reach_id, "reason": "the current reach"})
            continue
        derived = {}
        for table in tables:
            count = conn.execute(
                sa.text(f'DELETE FROM "{table}" WHERE reach_id = :reach'), {"reach": row.reach_id}
            ).rowcount
            if count:
                derived[table] = count
        conn.execute(sa.delete(reach_cache).where(reach_cache.c.reach_id == row.reach_id))
        removed.append({
            "reach_id": row.reach_id,
            "last_used_at": row.last_used_at.isoformat(),
            "derived_rows_removed": derived,
        })

    return {
        "cutoff": cutoff.isoformat(),
        "removed": removed,
        "retained": retained,
        "derived_tables_checked": tables,
    }
