"""Resolving the configured fallback reach at startup.

This replaces reading ``selected_polygon.geojson`` in the plugin's
``on_startup``. A deployment always has a usable AOI before anyone has
selected one, and that AOI is pinned so cache eviction cannot remove it.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from eddie_aoi.builder import build_reach
from eddie_aoi.config import ReachSpec, fallback_reach_spec
from eddie_aoi.db import create_schema
from eddie_aoi.reach import Reach
from eddie_aoi.reach import reach_id as make_reach_id
from eddie_aoi.sources import RiverSources
from eddie_aoi.store import get_or_build, read_cached, set_pinned

log = logging.getLogger(__name__)


def resolve_fallback_reach(
    conn: sa.Connection,
    sources: RiverSources,
    *,
    spec: ReachSpec | None = None,
    env: dict | None = None,
) -> Reach:
    """Build or reuse the configured fallback reach, and pin it.

    Any validation failure propagates: a deployment whose fallback does not
    validate must fail loudly at startup rather than serve an unvalidated
    area.
    """
    spec = spec or fallback_reach_spec(env)
    create_schema(conn)

    reach = get_or_build(
        conn,
        spec.river,
        spec.km_start,
        spec.km_end,
        lambda: build_reach(sources, spec.river, spec.km_start, spec.km_end),
        current_source_sha256=sources.source_sha256(),
    )

    set_pinned(conn, reach.reach_id, True)
    log.info(
        "AOI fallback reach resolved: %s (%s km %s-%s)",
        reach.reach_id,
        spec.river,
        spec.km_start,
        spec.km_end,
    )
    return reach


def fallback_reach_id(env: dict | None = None) -> str:
    """The identifier of the configured fallback reach, without building it."""
    spec = fallback_reach_spec(env)
    return make_reach_id(spec.river, spec.km_start, spec.km_end)


def is_fallback(conn: sa.Connection, reach_id: str, env: dict | None = None) -> bool:
    """Whether a reach is the configured, pinned fallback."""
    if reach_id != fallback_reach_id(env):
        return False
    cached = read_cached(conn, reach_id)
    return cached is not None and cached.pinned
