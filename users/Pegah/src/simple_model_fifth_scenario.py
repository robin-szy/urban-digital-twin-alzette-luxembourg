import pandas as pd
import geopandas as gpd
import numpy as np

def process_building_population_water_consumption(
    buildings_gdf, 
    liters_per_person=160.0, 
    unit_price_per_liter=0.005, # ≈ 0.005 €/liter OR 5 €/mˆ3
    usable_space_pct=0.80
):
    '''
    Scenario 5: Calculates building-level water consumption volumes and costs based on 
    population (for residential) and scaled floor plate geometry (for non-residential).
    
    According to architectural and real estate standards (such as BOMA and standard 
    Gross-to-Net floor area ratios), roughly 15% to 20% of a commercial or non-residential 
    building's gross footprint is taken up by structural walls, elevator cores, stairwells, 
    and mechanical riser shafts. Therefore, the usable/active floor space ratio is standardly 
    modeled at 0.80 (80%).
    '''
    df = buildings_gdf.copy()
    
    # Ensure required columns exist
    if "footprint_m2" not in df.columns:
        df["footprint_m2"] = df.geometry.area
    if "floors" not in df.columns:
        df["floors"] = 1.0
    if "est_population" not in df.columns:
        df["est_population"] = 0.0
    if "building_type" not in df.columns:
        df["building_type"] = "residential"

    def calculate_consumption(row):
        b_type = str(row.get("building_type", "")).lower().strip()
        
        # Check if building is residential (or contains residential tags)
        is_residential = ('residential' in b_type or b_type == '' or b_type == 'nan')
        
        if is_residential:
            # Rule 1: Residential -> Ensure any positive population rounds up to at least 1
            est_pop = row.get("est_population", 0.0)
            if pd.isna(est_pop) or est_pop <= 0:
                rounded_pop = 0.0
            elif est_pop < 1.0:
                rounded_pop = 1.0  # Force fractional populations below 1 to round up to 1
            else:
                rounded_pop = round(float(est_pop))
                
            volume_liters = rounded_pop * liters_per_person
            return pd.Series([rounded_pop, volume_liters], index=["calc_population_or_units", "water_consumption_liters"])
        else:
            # Rule 2: Non-residential -> footprint * rounded_floors * percentage * (1/3 * 160)
            footprint = row.get("footprint_m2", 0.0)
            raw_floors = row.get("floors", 1.0)
            rounded_floors = round(float(raw_floors)) if pd.notna(raw_floors) and raw_floors > 0 else 1.0
            
            volume_liters = footprint * rounded_floors * usable_space_pct * (1.0 / 3.0 * liters_per_person)
            return pd.Series([rounded_floors, volume_liters], index=["calc_population_or_units", "water_consumption_liters"])
        
    # Apply calculations across the dataframe
    results = df.apply(calculate_consumption, axis=1)
    df["calc_population_or_units"] = results["calc_population_or_units"]
    df["water_consumption_liters"] = results["water_consumption_liters"]
    
    # Convert liters to cubic meters for macro-reporting if needed (1 m3 = 1000 liters)
    df["water_consumption_m3"] = df["water_consumption_liters"] / 1000.0
    
    # Calculate financial cost
    df["water_consumption_cost"] = df["water_consumption_liters"] * unit_price_per_liter
    
    print(f" -> Processed Scenario 5 water consumption for {len(df)} buildings.")
    
    return df