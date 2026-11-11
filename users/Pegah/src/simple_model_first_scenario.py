import geopandas as gpd
import pandas as pd

def non_residential_water_allocation(buildings_gdf, total_water_volume=50000.0, percentage=0.10, unit_price=12.5):
    '''
    Processes water volume allocation and financial costs for non-residential 
    buildings. 
    Calculating proportional metrics based on footprint area for non-residential 
    Assigning zero values to residential and unclassified structures.
    '''
    
    df = buildings_gdf.copy()
    df.columns = df.columns.str.strip().str.lower()
    
    # 1. Identify non-residential structures using exact matching
    if "building_type" in df.columns:
        print(" -> Success: 'building_type' column found.")
        # Normalize text: lowercase and strip spaces (handles blanks/NaNs as 'nan')
        b_type = df["building_type"].astype(str).str.lower().str.strip()
        
        # Strict check: only true if it is explicitly 'non-residential'
        # 'residential', blanks, and NaNs will correctly evaluate to False (zeroed out)
        is_non_res = (b_type == 'non-residential')
    else:
        print(" -> Warning: 'building_type' column not found.")
        is_non_res = pd.Series(False, index=df.index)

    # 2. Ensure footprint_m2 exists
    if "footprint_m2" in df.columns:
        area_column = "footprint_m2"
    else:
        df["footprint_m2"] = df.geometry.area
        area_column = "footprint_m2"
    
    # 3. Calculate total non-residential area
    non_res_subset = df[is_non_res]
    total_non_res_area = non_res_subset[area_column].sum()
    
    # 4. Calculate extra water pool
    extra_water_pool = total_water_volume * percentage

    # 5. Initialize all economic columns with zeros for every building
    df["total_riverwater_volume"] = total_water_volume
    df["extra_riverwater_volume"] = extra_water_pool
    df["unit_riverwater_price"] = unit_price
    df["area_share"] = 0.0
    df["allocated_riverwater_volume"] = 0.0
    df["total_riverwater_cost"] = 0.0
    
    # 6. Populate values ONLY for the actual non-residential buildings
    if total_non_res_area > 0:
        df.loc[is_non_res, "area_share"] = df.loc[is_non_res, area_column] / total_non_res_area
        df.loc[is_non_res, "allocated_riverwater_volume"] = extra_water_pool * df.loc[is_non_res, "area_share"]
        df.loc[is_non_res, "total_riverwater_cost"] = df.loc[is_non_res, "total_riverwater_cost"] * unit_price
    
    print(f"Processed {len(df)} total buildings:")
    print(f" -> Non-residential buildings found: {is_non_res.sum()}")
    print(f" -> Residential / blank buildings (set to 0): {(~is_non_res).sum()}")
    print(f" -> Total Non-Residential Footprint Area: {total_non_res_area:.2f} m²")
    
    return df