"""EDDIE Celery task: Pegah's building population and non-residential water-fee model.

base tables (PostGIS) -> AOI-filtered query -> pure model -> building_population (PostGIS) -> 4326 view -> GeoServer
"""
import geopandas as gpd
import numpy as np
import sqlalchemy as sa
from eddie.digitaltwin import setup_environment
from eddie.tasks import app
from eddie_base_data.publish import publish_view, write_derived
from eddie_base_data.query import AOI_SQL, aoi_params, read_table, resolve_aoi

from .population_model import calculate_population_and_heights
from .simple_economic_model import process_non_residential_impact

TABLE = "building_population"
TARGET_CRS = "EPSG:2169"
# The intermediate columns exporters.export_results() left out of its files.
INTERMEDIATE_COLUMNS = ["index_right", "index_left", "ext_height", "canton", "commune_code", "commune_nom", "commune_key",
                        "femmes_mineures", "hommes_mineurs", "femmes_majeures", "hommes_majeurs",
                        "population", "total_commune_volume_m3", "vol_weight", "in_aoi"]

# The communes holding the centroid of a building in the AOI. The model needs all of their buildings.
COMMUNES_SQL = f"""
    SELECT c.* FROM base_communes c
    WHERE EXISTS (SELECT 1 FROM base_buildings_inspire b
                  WHERE ST_Intersects(b.geometry, {AOI_SQL})
                    AND ST_Contains(c.geometry, ST_Centroid(b.geometry)))"""
BUILDINGS_SQL = f"""
    SELECT b.*, ST_Intersects(b.geometry, {AOI_SQL}) AS in_aoi
    FROM base_buildings_inspire b
    JOIN ({COMMUNES_SQL}) c ON ST_Contains(c.geometry, ST_Centroid(b.geometry))"""
# Within the 30 m the model's height join reaches.
HEIGHTS_SQL = f"""
    SELECT h.* FROM base_buildings_lu00 h
    WHERE EXISTS (SELECT 1 FROM ({COMMUNES_SQL}) c WHERE ST_DWithin(h.geometry, c.geometry, 30))"""


def read_model_inputs(conn, aoi) -> dict:
    params = aoi_params(aoi)

    def read(sql):
        return gpd.read_postgis(sa.text(sql), conn, geom_col="geometry", params=params)

    return {"raw_buildings": read(BUILDINGS_SQL), "commune_gdf": read(COMMUNES_SQL), "height_gpkg": read(HEIGHTS_SQL),
            "population_df": read_table(conn, "base_commune_population")}


def assert_commune_totals_conserved(buildings) -> None:
    """Population per commune must add up to its register total before filtering to the AOI."""
    totals = buildings.groupby("lau2").agg(estimated=("est_population", "sum"), register=("population", "first"))
    if not np.allclose(totals["estimated"], totals["register"], rtol=1e-9):
        raise RuntimeError(f"Commune population not conserved:\n{totals}")


@app.task(name="eddie_building_population.tasks.compute_building_population")
def compute_building_population(total_water_volume: float = 100000.0, percentage: float = 0.10,
                                unit_price: float = 7.0) -> dict:
    engine = setup_environment.get_database()
    with engine.connect() as conn:
        scope = resolve_aoi(conn, TABLE)
        inputs = read_model_inputs(conn, scope.geometry)

    # Whole communes go in, so a building's estimate does not depend on the AOI; the AOI only selects rows after.
    buildings = calculate_population_and_heights(**inputs, target_crs=TARGET_CRS)
    assert_commune_totals_conserved(buildings)
    in_aoi = buildings[buildings["in_aoi"]]
    in_aoi = process_non_residential_impact(in_aoi, total_water_volume, percentage, unit_price)
    in_aoi = in_aoi.drop(columns=INTERMEDIATE_COLUMNS, errors="ignore")

    write_derived(engine, TABLE, in_aoi, scope)
    layer = publish_view(engine, TABLE)
    by_commune = in_aoi.groupby("commune")["est_population"].sum().round(1)
    return {"table": TABLE, "layer": layer, "reach_id": scope.reach_id, "variant_key": scope.variant_key,
            "buildings": len(in_aoi), "est_population": round(float(in_aoi["est_population"].sum()), 1),
            "est_population_by_commune": by_commune.to_dict()}
