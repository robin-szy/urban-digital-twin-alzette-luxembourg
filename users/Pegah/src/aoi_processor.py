import geopandas as gpd
from src.aoi import build_aoi  # Assuming aoi.py is available
import shapely

def generate_aoi_and_filter_buildings(config, data_path, gml_building_path, output_gpkg_path):
    # Generate AOI
    aoi_gdf, points_gdf = build_aoi(
        river=config.RIVER_NAME,
        km_start=config.KM_START,
        km_end=config.KM_END,
        margin_m=config.MARGIN_METERS,
        kilometrage_path=data_path
    )
    
    # Read and filter buildings
    buildings = gpd.read_file(gml_building_path)
    if buildings.crs != aoi_gdf.crs:
        buildings = buildings.to_crs(aoi_gdf.crs)
        
    aoi_polygon = aoi_gdf.unary_union
    filtered_buildings = buildings[buildings.intersects(aoi_polygon)].copy()
    filtered_buildings["geometry"] = shapely.force_2d(filtered_buildings.geometry)
    
    # Save intermediate result
    filtered_buildings.to_file(output_gpkg_path, driver="GPKG")
    return aoi_gdf, filtered_buildings