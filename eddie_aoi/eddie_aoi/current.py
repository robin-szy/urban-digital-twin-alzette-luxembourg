"""The current reach: which part of the river the whole twin is showing and computing for.

It is the last selection whose reach finished building successfully. Before any
selection has succeeded it is the configured fallback reach (``AOI_FALLBACK_REACH``,
Alzette km 35-50 by default).

It is deployment state, not per viewer: a selection by one person changes what
everyone sees after a reload. It lives in the one-row ``aoi_current`` table so it
survives a restart, and that table's foreign key refuses to let the current reach
be deleted from ``reach_cache``.

Only a successful build ever writes it (``set_current``). A refused or failed
selection never reaches that call, so the twin keeps showing the last good reach.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from eddie_aoi.config import fallback_reach_spec
from eddie_aoi.db import STATUS_READY, aoi_current, reach_cache
from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import reach_id as make_reach_id


@dataclass(frozen=True)
class CurrentReach:
    """Which reach is current, and whether that is only the fallback."""

    reach_id: str
    is_fallback: bool
    selected_at: datetime | None = None


def set_current(conn: sa.Connection, reach_id: str) -> None:
    """Make a built reach the current one.

    Refuses a reach that is not ready: the current reach must always be one the
    twin can actually show and compute for.
    """
    status = conn.execute(
        sa.select(reach_cache.c.status).where(reach_cache.c.reach_id == reach_id)
    ).scalar_one_or_none()
    if status != STATUS_READY:
        raise PreparationError(
            f"Reach {reach_id} cannot become the current reach: its status is {status!r}, "
            f"not {STATUS_READY!r}."
        )

    now = datetime.now(timezone.utc)
    stmt = pg_insert(aoi_current).values(id=1, reach_id=reach_id, selected_at=now)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"], set_={"reach_id": reach_id, "selected_at": now}
        )
    )


def current_reach(conn: sa.Connection, env: dict | None = None) -> CurrentReach:
    """The current reach, or the configured fallback when nothing has been selected.

    Read-only. Whether the reach is already built is the caller's concern - see
    ``eddie_aoi.resolver.ensure_reach``.
    """
    fallback = fallback_reach_spec(env)
    fallback_id = make_reach_id(fallback.river, fallback.km_start, fallback.km_end)

    row = conn.execute(
        sa.select(aoi_current.c.reach_id, aoi_current.c.selected_at).where(
            aoi_current.c.id == 1
        )
    ).one_or_none()

    if row is None:
        return CurrentReach(reach_id=fallback_id, is_fallback=True)
    # A deliberate selection is never "the fallback", even when it happens to be
    # the same km range: the user chose it, so the client shows no fallback notice.
    return CurrentReach(
        reach_id=row.reach_id, is_fallback=False, selected_at=row.selected_at
    )
