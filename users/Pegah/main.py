import os
from src import config
from src.aoi_processor import generate_aoi_and_filter_buildings
from src.population_model import calculate_population_and_heights

from src.osm_categorizer import enrich_with_osm_industrial_data

from src.simple_model_first_scenario import non_residential_water_allocation
from src.simple_model_second_scenario import process_rainwater_impact
from src.simple_model_third_scenario import process_buildings_flood_risk
from src.simple_model_fourth_scenario import process_water_quality_charge

from src.exporters import export_results

def main():
    # Ensure outputs directory exists
    os.makedirs("outputs", exist_ok=True)
    
    print("Step 1: Generating AOI and filtering buildings...")
    aoi_gdf, filtered_buildings = generate_aoi_and_filter_buildings(
        config=config,
        data_path="data/kilometrage-des-cours-deau.gpkg",
        gml_building_path="data/BU.Building.gml",
        output_gpkg_path="outputs/alzette_aoi_buildings.gpkg"
    )
    
    print("Step 2: Processing heights, volumes, and population...")
    buildings_joined = calculate_population_and_heights(
        buildings_path="outputs/alzette_aoi_buildings.gpkg",
        commune_path="data/LIMADM_COMMUNES.shp",
        population_path="data/rnpp_population_per_municipality.csv",
        height_gpkg_path="data/LU00.gpkg",
        target_crs=config.TARGET_CRS
    )
    
    print("Step 2.5: Enriching buildings with OpenStreetMap industrial data...")
    # Apply directly to buildings_joined in-memory
    buildings_joined = enrich_with_osm_industrial_data(buildings_joined, aoi_gdf=aoi_gdf)
    #print("ALL COLUMNS NOW IN MEMORY:", buildings_joined.columns.tolist())
    
    print("Step 3 (Scenario 1): Applying simplified non-residential water and price model...")
    buildings_joined = non_residential_water_allocation(
        buildings_joined,
        total_water_volume=100000.0,
        percentage=0.10,
        unit_price=7.0
    )
    
    print("Step 3 (Scenario 2): Applying rainwater impact and imperviousness fee model...")
    buildings_joined = process_rainwater_impact(
        buildings_joined,
        total_rainwater_volume=120000.0,
        unit_price=4.5
    )
    
    print("Step 3 (Scenario 3): Applying flood risk proximity levy model...")
    buildings_joined = process_buildings_flood_risk(
        buildings_joined,
        river_path="data/kilometrage-des-cours-deau.gpkg",
        distance_threshold=50.0,
        base_fee=150.0
    )
    
    print("Step 3 (Scenario 4): Applying urban runoff pollution and water quality surcharge...")
    buildings_joined = process_water_quality_charge(
        buildings_joined,
        quality_unit_price=2.5  # Unit price per m² adjusted for pollution factor
    )

    print("Step 4: Exporting final files...")
    export_results(
        buildings_joined,
        csv_path="outputs/buildings_population_final.csv",
        geojson_2d_path="outputs/alzette_non_residential_water_costs.geojson",
    )
    print("Pipeline completed successfully!")
    
if __name__ == "__main__":
    main()