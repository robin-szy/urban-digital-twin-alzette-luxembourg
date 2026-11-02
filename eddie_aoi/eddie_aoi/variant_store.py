"""Persisting and reusing AOI variant geometry.

A cached variant entry is keyed on the REQUESTED variant, and records which
variant actually produced it. That is what makes a degraded entry
recognisable: when the real source later appears, the entry is stale even
though its source hash has not changed - because it was never built from
that source in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import geopandas as gpd
import sqlalchemy as sa
from shapely import wkt as shapely_wkt
from sqlalchemy.dialects.postgresql import insert as pg_insert

from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.db import METRIC_SRID, aoi_variant_cache
from eddie_aoi.reach import Reach
from eddie_aoi.variants import VariantRegistry, VariantResolution, variant_key


@dataclass(frozen=True)
class CachedVariant:
    """One ``aoi_variant_cache`` row."""

    reach_id: str
    variant_key: str
    variant: str
    requested_variant: str
    geometry: object
    params: dict | None
    source_sha256: str | None
    degraded: bool
    degraded_reason: str | None

    def to_frame(self) -> gpd.GeoDataFrame:
        """The same single-row shape a variant provider returns."""
        import json

        return gpd.GeoDataFrame(
            {
                "variant": [self.variant],
                "variant_key": [self.variant_key],
                "params": [json.dumps(self.params or {}, sort_keys=True)],
                "reach_id": [self.reach_id],
                "source_sha256": [self.source_sha256],
                "requested_variant": [self.requested_variant],
                "degraded": [self.degraded],
                "degraded_reason": [self.degraded_reason],
            },
            geometry=[self.geometry],
            crs=METRIC_CRS,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_variant(
    conn: sa.Connection,
    reach: Reach,
    resolution: VariantResolution,
    *,
    requested_key: str | None = None,
) -> str:
    """Persist a resolved variant against its reach."""
    row = resolution.frame.iloc[0]
    key = requested_key or variant_key(resolution.requested, _params_of(row))
    now = _now()

    values = {
        "reach_id": reach.reach_id,
        "variant_key": key,
        "geometry": sa.func.ST_GeomFromText(
            resolution.frame.geometry.iloc[0].wkt, METRIC_SRID
        ),
        "variant": resolution.actual,
        "requested_variant": resolution.requested,
        "params": _params_of(row),
        "source_sha256": row.get("source_sha256"),
        "degraded": bool(resolution.degraded),
        "degraded_reason": resolution.reason,
        "built_at": now,
        "last_used_at": now,
    }
    update = {k: v for k, v in values.items() if k not in ("reach_id", "variant_key")}

    stmt = pg_insert(aoi_variant_cache).values(**values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["reach_id", "variant_key"], set_=update
        )
    )
    return key


def _params_of(row) -> dict:
    import json

    raw = row.get("params")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return {}


def read_variant(
    conn: sa.Connection, reach_id: str, key: str
) -> CachedVariant | None:
    """A cached variant entry, or None.

    Geometry comes back as WKT so decoding does not depend on how the driver
    represents a PostGIS value.
    """
    row = conn.execute(
        sa.select(
            aoi_variant_cache.c.reach_id,
            aoi_variant_cache.c.variant_key,
            aoi_variant_cache.c.variant,
            aoi_variant_cache.c.requested_variant,
            aoi_variant_cache.c.params,
            aoi_variant_cache.c.source_sha256,
            aoi_variant_cache.c.degraded,
            aoi_variant_cache.c.degraded_reason,
            sa.func.ST_AsText(aoi_variant_cache.c.geometry).label("geometry_wkt"),
        ).where(
            sa.and_(
                aoi_variant_cache.c.reach_id == reach_id,
                aoi_variant_cache.c.variant_key == key,
            )
        )
    ).one_or_none()

    if row is None:
        return None

    return CachedVariant(
        reach_id=row.reach_id,
        variant_key=row.variant_key,
        variant=row.variant,
        requested_variant=row.requested_variant,
        geometry=shapely_wkt.loads(row.geometry_wkt),
        params=row.params,
        source_sha256=row.source_sha256,
        degraded=bool(row.degraded),
        degraded_reason=row.degraded_reason,
    )


def touch_variant(conn: sa.Connection, reach_id: str, key: str) -> None:
    """Record that a cached variant was used, for last-recently-used eviction."""
    conn.execute(
        sa.update(aoi_variant_cache)
        .where(
            sa.and_(
                aoi_variant_cache.c.reach_id == reach_id,
                aoi_variant_cache.c.variant_key == key,
            )
        )
        .values(last_used_at=_now())
    )


def is_variant_stale(
    cached: CachedVariant,
    registry: VariantRegistry,
    current_source_sha256: str | None,
) -> bool:
    """Whether a cached variant must be rebuilt rather than served.

    Two separate reasons:

    * its source data changed since it was built, or
    * it is a degraded substitute and the requested variant has since become
      available - the entry's own source hash is unchanged in that case, so
      a hash comparison alone would keep serving the substitute forever.
    """
    if cached.degraded:
        provider = registry.providers.get(cached.requested_variant)
        if provider is not None and provider.availability().available:
            return True

    if current_source_sha256 is None or cached.source_sha256 is None:
        return False
    return cached.source_sha256 != current_source_sha256


def get_or_build_variant(
    conn: sa.Connection,
    reach: Reach,
    registry: VariantRegistry,
    name: str,
    **params,
) -> CachedVariant:
    """Serve a cached variant, or resolve and persist one.

    Parameters are part of the key, so a variant requested with different
    parameters is built separately rather than served from a neighbouring
    entry. Geometry is never derived between parameter values - eroding a
    cached wider corridor does not reproduce a narrower one.
    """
    key = variant_key(name, params)
    cached = read_variant(conn, reach.reach_id, key)

    if cached is not None:
        provider = registry.providers.get(cached.variant)
        current_hash = None
        if provider is not None and hasattr(provider, "source_sha256"):
            current_hash = provider.source_sha256()
        if not is_variant_stale(cached, registry, current_hash):
            touch_variant(conn, reach.reach_id, key)
            return cached

    resolution = registry.resolve_variant(name, reach, **params)
    save_variant(conn, reach, resolution, requested_key=key)
    return read_variant(conn, reach.reach_id, key)
