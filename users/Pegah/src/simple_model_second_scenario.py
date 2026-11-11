import geopandas as gpd
import pandas as pd

def process_rainwater_impact(buildings_gdf, total_rainwater_volume=120000.0, unit_price=4.5):
    '''
    Processes rainwater volume allocation and stormwater fees/costs for 
    ALL buildings based on their footprint area (accounting for imperviousness).
    Applies proportionally to residential, non-residential, and other structures.
    '''
    
    df = buildings_gdf.copy()
    df.columns = df.columns.str.strip().str.lower()
    
    # 1. Ensure footprint_m2 exists
    if "footprint_m2" in df.columns:
        area_column = "footprint_m2"
    else:
        df["footprint_m2"] = df.geometry.area
        area_column = "footprint_m2"
    
    # 2. Calculate total building footprint area across all structures
    total_building_area = df[area_column].sum()
    
    # 3. Initialize stormwater economic columns for every building
    df["total_rainwater_volume"] = total_rainwater_volume
    df["unit_price_rainwater"] = unit_price
    df["building_area_share"] = 0.0
    df["allocated_rainwater_volume"] = 0.0
    df["rainwater_cost"] = 0.0
    
    # 4. Populate values proportionally for all buildings
    if total_building_area > 0:
        df["building_area_share"] = df[area_column] / total_building_area
        df["allocated_rainwater_volume"] = total_rainwater_volume * df["building_area_share"]
        df["rainwater_cost"] = df["allocated_rainwater_volume"] * unit_price
        
    print(f"Processed stormwater impact for {len(df)} total buildings:")
    print(f" -> Total Building Footprint Area: {total_building_area:.2f} m²")
    print(f" -> Total Rainwater Volume Allocated: {total_rainwater_volume:,.2f} m³")
    
    return df