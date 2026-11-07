import os
from src import config
from src.aoi_processor import generate_aoi_and_filter_buildings
from src.population_model import calculate_population_and_heights
#from src.osm_categorizer import enrich_with_osm_industrial_data
from src.simple_economic_model import process_non_residential_impact
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
    
    #print("Step 1.5: Enriching buildings with OpenStreetMap industrial data...")
    #filtered_buildings = enrich_with_osm_industrial_data(filtered_buildings, aoi_gdf=aoi_gdf)
    # Overwrite the intermediate GPKG so Step 2 reads the OSM-enriched buildings
    #filtered_buildings.to_file("outputs/alzette_aoi_buildings.gpkg", driver="GPKG")

    print("Step 2: Processing heights, volumes, and population...")
    buildings_joined = calculate_population_and_heights(
        buildings_path="outputs/alzette_aoi_buildings.gpkg",
        commune_path="data/LIMADM_COMMUNES.shp",
        population_path="data/rnpp_population_per_municipality.csv",
        height_gpkg_path="data/LU00.gpkg",
        target_crs=config.TARGET_CRS
    )
    
    print("Step 2.5: Applying simplified non-residential water and price model...")
    buildings_joined = process_non_residential_impact(
        buildings_joined,
        total_water_volume=100000.0,
        percentage=0.10,
        unit_price=7.0
    )

    print("Step 3: Exporting final files...")
    export_results(
        buildings_joined,
        csv_path="outputs/buildings_population_final.csv",
        geojson_2d_path="outputs/alzette_non_residential_water_costs.geojson",
    )
    print("Pipeline completed successfully!")
    
if __name__ == "__main__":
    main()