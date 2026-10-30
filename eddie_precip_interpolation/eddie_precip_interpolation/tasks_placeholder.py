import eddie.geoserver as gs
from eddie.digitaltwin import setup_environment
from eddie.tasks import app

from eddie_precip_interpolation import db
from eddie_precip_interpolation.transform import double_value

# This is the only file in the package that imports eddie.* - everything else
# (transform, db) runs and is tested without EDDIE or Docker at all, same
# split as eddie_alzette_ingest.

OBSERVATIONS_TABLE = "weather_observations"
STATIONS_TABLE = "weather_stations"
DERIVED_TABLE = "precipitation_doubled"


@app.task(name="eddie_precip_interpolation.tasks.derive_precipitation")
def derive_precipitation() -> dict:
    """Manually triggered smoke-test task: read every stored precipitation
    observation, apply the placeholder multiply-by-two transform, publish the
    result as a GeoServer layer. No AOI, no schedule - this proves the
    derive-and-publish path before the real interpolation algorithm replaces
    the transform (see proposal.md / design.md).
    """
    engine = setup_environment.get_database()
    with engine.connect() as conn:
        observations_df = db.read_precipitation_observations(conn, OBSERVATIONS_TABLE)
        doubled_df = double_value(observations_df)
        stations_gdf = db.read_station_geometries(conn, STATIONS_TABLE)
        derived_gdf = db.attach_station_geometry(doubled_df, stations_gdf)

        db.ensure_derived_table(conn, DERIVED_TABLE)
        rows_written = db.write_derived_table(conn, derived_gdf, DERIVED_TABLE)

        workspace_name = gs.Workspaces.INPUT_LAYERS_WORKSPACE
        data_store = gs.create_main_db_store(workspace_name)
        gs.create_datastore_layer(conn, workspace_name, data_store, DERIVED_TABLE)

    return {"rows_written": rows_written, "layer": DERIVED_TABLE}
