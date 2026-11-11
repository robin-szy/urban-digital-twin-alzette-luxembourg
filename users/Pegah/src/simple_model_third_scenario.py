import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

def process_buildings_flood_risk(buildings_gdf, river_path="data/kilometrage-des-cours-deau.gpkg", distance_threshold=50.0, base_fee=150.0):
    '''
    Calculates the shortest distance from each building to the river using 
    EPSG:2169 meter coordinates, then applies a proximity-based flood protection fee.
    
    At 0 meters away from the river: the distance is 0 -> that building pays maximum fee
    As the distance increases: the fee grows
    At the distance_threshold: the fraction becomes 1.0 -> the fee drops down to €0
    '''
    df = buildings_gdf.copy()
    
    # 1. Load the river dataset and ensure it shares the exact same CRS
    river_gdf = gpd.read_file(river_path)
    if river_gdf.crs != df.crs:
        river_gdf = river_gdf.to_crs(df.crs)
        
    # 2. Combine all river segments into a single geometric object for efficient calculation
    river_geom = unary_union(river_gdf.geometry)
    
    # 3. Calculate shortest distance in meters for every building polygon to the river line
    print("Calculating shortest distance from each building to the river...")
    df["distance_to_river"] = df.geometry.distance(river_geom)
    
    # 4. Initialize and calculate the flood risk levy
    df["flood_risk_fee"] = 0.0
    
    # Filter for buildings within the risk threshold distance (e.g., 50 meters)
    in_risk_zone = df["distance_to_river"] <= distance_threshold
    
    # Linear decay formula: closest buildings pay the maximum base_levy, 
    # scaling down to zero as they approach the threshold boundary.
    df.loc[in_risk_zone, "flood_risk_fee"] = base_fee * (1.0 - (df.loc[in_risk_zone, "distance_to_river"] / distance_threshold))
    
    print(f"Processed flood risk levy for {len(df)} buildings:")
    print(f" -> Buildings within {distance_threshold}m risk zone: {in_risk_zone.sum()}")
    print(f" -> Max distance to river observed: {df['distance_to_river'].max():.2f} meters")
    
    return df