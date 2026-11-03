from src import config
from src.aoi_processor import generate_aoi_and_filter_buildings
from src.population_model import calculate_population_and_heights
from src.exporters import export_results

def main():
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
    
    print("Step 3: Exporting final files...")
    export_results(
        buildings_joined,
        csv_path="outputs/buildings_population_final.csv",
        geojson_2d_path="outputs/buildings_polygons_eddie.geojson",
        geojson_3d_path="outputs/buildings_3d_polygons_eddie.geojson"
    )
    print("Pipeline completed successfully!")

if __name__ == "__main__":
    main()