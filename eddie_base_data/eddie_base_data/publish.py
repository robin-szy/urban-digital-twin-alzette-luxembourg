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
            FROM "{table}"
            WHERE {_current_scope_filter(conn, scope)}"""))


def _current_scope_filter(conn: sa.Connection, scope: AoiScope) -> str:
    """The view's WHERE clause: only the current reach, and only this dataset's bound variant.

    The table keeps rows for every reach and variant (switching back is then free); the
    published layer must show one area, or a second selection would draw both reaches and
    duplicate the buildings where they overlap. The current reach is looked up live from
    eddie_aoi's aoi_current table, so switching reach needs no view rebuild; before any
    selection it is the configured fallback. The variant is the binding at write time
    (scope.variant_key): rows of a superseded binding, like the old bbox_500, drop out.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_aoi.db import create_schema
    from eddie_aoi.startup import fallback_reach_id

    create_schema(conn)  # aoi_current must exist before a view can refer to it
    fallback = fallback_reach_id().replace("'", "''")
    variant = scope.variant_key.replace("'", "''")
    return (f"reach_id = COALESCE((SELECT reach_id FROM aoi_current WHERE id = 1), '{fallback}') "
            f"AND variant_key = '{variant}'")


def publish_view(engine: sa.Engine, table: str) -> str:
    """Serve the table's 4326 view from GeoServer; Terria's catalog lists every layer of the workspace."""
    # pylint: disable=import-outside-toplevel
    import eddie.geoserver as gs

    view = view_name(table)
    workspace = gs.Workspaces.INPUT_LAYERS_WORKSPACE
    store = gs.create_main_db_store(workspace)
    with engine.connect() as conn:
        gs.create_datastore_layer(conn, workspace, store, view)
    recalculate_bbox(workspace, store, view)
    return f"{workspace}:{view}"


def recalculate_bbox(workspace: str, store: str, layer: str) -> None:
    """Make GeoServer recompute a layer's bounding box from the rows its view shows now.

    GeoServer keeps the box from when the layer was created, so after the current reach
    changes, a layer would still be framed (and clipped) to the old reach. Called after
    every publish, i.e. right after a module wrote results for the new reach.
    """
    # pylint: disable=import-outside-toplevel
    import requests
    from eddie.config import EnvVariable
    from eddie.geoserver import get_geoserver_url

    response = requests.put(
        f"{get_geoserver_url()}/workspaces/{workspace}/datastores/{store}/featuretypes/{layer}",
        params={"recalculate": "nativebbox,latlonbbox"},
        headers={"Content-Type": "application/xml"},
        data=f"<featureType><name>{layer}</name><enabled>true</enabled></featureType>",
        auth=(EnvVariable.GEOSERVER_ADMIN_NAME, EnvVariable.GEOSERVER_ADMIN_PASSWORD),
        timeout=60,
    )
    response.raise_for_status()
