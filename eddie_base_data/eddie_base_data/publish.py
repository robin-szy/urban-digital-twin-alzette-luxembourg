"""The write side: derived table with reach/variant attribution, published as a 4326 view.

Results stay in EPSG:2169. EPSG:4326 exists only in the <table>_4326 view GeoServer
serves, so nothing can read it back as the source of a measurement.
"""
from datetime import datetime, timezone

import geopandas as gpd
import sqlalchemy as sa

from .ingest import WORKING_SRID, transactional
from .query import AoiScope

PUBLISH_SRID = 4326


def view_name(table: str) -> str:
    return f"{table}_{PUBLISH_SRID}"


def write_derived(engine: sa.Engine, table: str, frame: gpd.GeoDataFrame, scope: AoiScope) -> None:
    """Make frame the result for this reach and variant; results of other scopes stay.

    A table from before attribution (no reach_id column) holds nothing that can be
    attributed, so it is replaced.
    """
    if frame.crs is None or frame.crs.to_epsg() != WORKING_SRID:
        raise ValueError(f"Derived results are stored in EPSG:{WORKING_SRID}; got {frame.crs}.")
    frame = frame.assign(reach_id=scope.reach_id, variant_key=scope.variant_key, degraded=scope.degraded,
                         degraded_reason=scope.degraded_reason, computed_utc=datetime.now(timezone.utc))
    view = view_name(table)
    with transactional(engine).begin() as conn:
        inspector = sa.inspect(conn)
        attributed = inspector.has_table(table) and "reach_id" in {c["name"] for c in inspector.get_columns(table)}
        conn.execute(sa.text(f'DROP VIEW IF EXISTS "{view}"'))
        if attributed:
            conn.execute(sa.text(f'DELETE FROM "{table}" WHERE reach_id = :reach AND variant_key = :variant'),
                         {"reach": scope.reach_id, "variant": scope.variant_key})
            frame.to_postgis(table, conn, if_exists="append", index=False)
        else:
            frame.to_postgis(table, conn, if_exists="replace", index=False)
            # Any polygon type, so a later scope with multipart results can be appended.
            conn.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN geometry '
                                 f'TYPE geometry(Geometry, {WORKING_SRID})'))
            conn.execute(sa.text(f'CREATE INDEX "idx_{table}_scope" ON "{table}" (reach_id, variant_key)'))
        columns = [c["name"] for c in sa.inspect(conn).get_columns(table) if c["name"] != "geometry"]
        selected = ", ".join(f'"{column}"' for column in columns)
        conn.execute(sa.text(f"""
            CREATE VIEW "{view}" AS
            SELECT {selected},
                   ST_Transform(geometry, {PUBLISH_SRID})::geometry(Geometry, {PUBLISH_SRID}) AS geometry
            FROM "{table}" """))


def publish_view(engine: sa.Engine, table: str) -> str:
    """Serve the table's 4326 view from GeoServer; Terria's catalog lists every layer of the workspace."""
    # pylint: disable=import-outside-toplevel
    import eddie.geoserver as gs

    view = view_name(table)
    workspace = gs.Workspaces.INPUT_LAYERS_WORKSPACE
    with engine.connect() as conn:
        gs.create_datastore_layer(conn, workspace, gs.create_main_db_store(workspace), view)
    return f"{workspace}:{view}"
