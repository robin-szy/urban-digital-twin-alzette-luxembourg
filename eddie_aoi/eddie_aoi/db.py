"""PostGIS schema for the reach and AOI variant caches.

All geometry is stored in the project metric CRS (SRID 2169). Geographic
coordinates are produced only when publishing to a map client.
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from geoalchemy2 import Geometry

from eddie_aoi.constants import METRIC_CRS

METRIC_SRID = int(METRIC_CRS.split(":")[1])

metadata = sa.MetaData()

#: Reach build lifecycle.
STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUSES = (STATUS_BUILDING, STATUS_READY, STATUS_FAILED)


reach_cache = sa.Table(
    "reach_cache",
    metadata,
    sa.Column("reach_id", sa.Text, primary_key=True),
    sa.Column("river", sa.Text, nullable=False),
    sa.Column("km_start", sa.Numeric, nullable=False),
    sa.Column("km_end", sa.Numeric, nullable=False),
    # Nullable: a reach that is building or failed has no centerline yet.
    sa.Column("centerline", Geometry("LINESTRING", srid=METRIC_SRID), nullable=True),
    # Chainage, parallel arrays - needed to rebuild the Reach without
    # re-reading the marker source.
    sa.Column("marker_km", sa.JSON, nullable=True),
    sa.Column("marker_positions", sa.JSON, nullable=True),
    sa.Column("route_length_m", sa.Numeric, nullable=True),
    sa.Column("marker_max_offset_m", sa.Numeric, nullable=True),
    sa.Column("markers_in_order", sa.Boolean, nullable=True),
    sa.Column("provenance", sa.JSON, nullable=True),
    # The cache-invalidation trigger: a hash of the marker and river sources.
    sa.Column("source_sha256", sa.Text, nullable=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "last_used_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    # The configured fallback reach is pinned so eviction cannot remove it.
    sa.Column(
        "pinned", sa.Boolean, nullable=False, server_default=sa.text("false")
    ),
    sa.CheckConstraint(
        "status in ('building', 'ready', 'failed')", name="reach_cache_status_check"
    ),
    sa.CheckConstraint("km_start < km_end", name="reach_cache_range_check"),
)


aoi_variant_cache = sa.Table(
    "aoi_variant_cache",
    metadata,
    # Keyed on the REQUESTED variant, not the one actually used. A degraded
    # entry records both, so it can be recognised and rebuilt once the real
    # source appears.
    sa.Column(
        "reach_id",
        sa.Text,
        sa.ForeignKey("reach_cache.reach_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("variant_key", sa.Text, primary_key=True),
    # Lookup variants dissolve to a MultiPolygon, computed ones to a Polygon,
    # so the column has to accept either.
    sa.Column("geometry", Geometry("GEOMETRY", srid=METRIC_SRID), nullable=False),
    sa.Column("variant", sa.Text, nullable=False),
    sa.Column("requested_variant", sa.Text, nullable=False),
    sa.Column("params", sa.JSON, nullable=True),
    # Invalidation trigger: the source the geometry was built from.
    sa.Column("source_sha256", sa.Text, nullable=True),
    sa.Column(
        "degraded", sa.Boolean, nullable=False, server_default=sa.text("false")
    ),
    sa.Column("degraded_reason", sa.Text, nullable=True),
    sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "last_used_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
)


def database_url() -> str:
    """Connection URL from the EDDIE PostGIS environment variables."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DB", "db")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


def get_engine(url: str | None = None) -> sa.Engine:
    return sa.create_engine(url or database_url(), future=True)


def create_schema(conn: sa.Connection) -> None:
    """Create the AOI tables if they are absent.

    Additive only: it never drops or alters an existing table, so running it
    against a database that already has the tables is safe.
    """
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))
    metadata.create_all(conn, checkfirst=True)
