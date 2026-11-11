import pandas as pd
import geopandas as gpd

def process_water_quality_charge(buildings_gdf, quality_unit_price=2.5):
    '''
    Calculates a water quality and urban runoff pollution surcharge 
    based on OSM building classifications and footprint size.
    '''
    df = buildings_gdf.copy()
    
    # Ensure footprint_m2 exists
    if "footprint_m2" not in df.columns:
        df["footprint_m2"] = df.geometry.area
        
    def calculate_pollution_factor(row):
        osm_type = str(row.get("osm_building_type", "")).lower().strip()
        raw_tag = str(row.get("osm_raw_tag", "")).lower().strip()
        area = row.get("footprint_m2", 0.0)
        
        # Check for blanks, NaNs, or unclassified values
        is_missing = (
            osm_type == "" or 
            osm_type == "nan" or 
            osm_type == "none" or 
            osm_type == "unclassified_osm" or
            raw_tag == "" or
            raw_tag == "nan" or
            raw_tag == "none"
        )
        
        if is_missing:
            # Fallback for missing/blank data: treat as Medium-high commercial risk
            return 2.0
        
        # Assign pollution multiplier weights based on classified categories
        if 'industrial' in osm_type or 'industrial' in raw_tag:
            return 3.0  # High runoff pollution risk
        elif 'commercial' in osm_type or 'retail' in osm_type or 'commercial' in raw_tag:
            return 2.0  # Medium-high commercial runoff risk
        elif 'residential' in osm_type or 'residential' in raw_tag:
            return 1.0  # Baseline residential risk
        else:
            # Default fallback for any other unexpected text
            return 2.0
                
    # Apply multiplier
    df["pollution_multiplier"] = df.apply(calculate_pollution_factor, axis=1)
    
    # Calculate final water quality surcharge fee
    df["water_quality_charge"] = df["footprint_m2"] * df["pollution_multiplier"] * quality_unit_price
    
    print(f" -> Processed water quality surcharge for {len(df)} buildings (with commercial fallback for blanks).")
    return df