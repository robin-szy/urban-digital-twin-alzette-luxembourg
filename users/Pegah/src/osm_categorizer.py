import osmnx as ox
import geopandas as gpd

def enrich_with_osm_industrial_data(buildings_gdf, aoi_gdf):
    '''
    Fetches building tags from OpenStreetMap within the AOI polygon 
    and spatially joins them to enrich the building dataset with a dedicated OSM column.
    '''
    df = buildings_gdf.copy()
    
    # 1. Get the bounding polygon of your AOI (reprojected to WGS84 EPSG:4326 for OSM query)
    aoi_wgs84 = aoi_gdf.to_crs(epsg=4326)
    polygon = aoi_wgs84.unary_union
    
    print("Fetching building tags from OpenStreetMap...")
    tags = {"building": True}
    try:
        osm_buildings = ox.features_from_polygon(polygon, tags)
        
        if not osm_buildings.empty:
            cols_to_keep = ['geometry', 'building', 'amenity', 'shop', 'industrial', 'commercial']
            available_cols = [c for c in cols_to_keep if c in osm_buildings.columns]
            osm_subset = osm_buildings[available_cols].copy()
            
            # Reproject OSM data back to your target CRS (EPSG:2169)
            osm_subset = osm_subset.to_crs(df.crs)
            
            print(f"Successfully fetched {len(osm_subset)} features from OSM. Performing spatial join...")
            
            # Ensure valid geometries
            df['geometry'] = df['geometry'].buffer(0)
            osm_subset['geometry'] = osm_subset['geometry'].buffer(0)
            
            # <--- FIX: Drop conflicting columns from prior joins so sjoin won't crash --->
            df = df.drop(columns=['index_left', 'index_right'], errors='ignore')
            osm_subset = osm_subset.drop(columns=['index_left', 'index_right'], errors='ignore')
            
            # Spatial join
            joined = gpd.sjoin(df, osm_subset, how="left", predicate="intersects")
            
            # Drop duplicate building matches if a building intersects multiple OSM features
            joined = joined.loc[~joined.index.duplicated(keep='first')]
            
            # Extract OSM classification into a SEPARATE column
            def extract_osm_type(row):
                b_val = str(row.get('building_y', row.get('building_x', ''))).lower()
                ind_val = str(row.get('industrial', '')).lower()
                com_val = str(row.get('commercial', '')).lower()
                amenity_val = str(row.get('amenity', '')).lower()
                shop_val = str(row.get('shop', '')).lower()
                
                if 'industrial' in b_val or (ind_val != 'nan' and ind_val != ''):
                    return 'industrial'
                elif 'commercial' in b_val or 'retail' in b_val or (com_val != 'nan' and com_val != '') or (shop_val != 'nan' and shop_val != ''):
                    return 'commercial'
                elif 'residential' in b_val:
                    return 'residential'
                else:
                    return 'unclassified_osm'

            # Create brand new columns for OSM data
            joined['osm_building_type'] = joined.apply(extract_osm_type, axis=1)
            joined['osm_raw_tag'] = joined.get('building_y', joined.get('building_tag', 'none'))
            
            print(" -> Successfully added dedicated OSM columns to building dataset.")
            return joined
            
    except Exception as e:
        import traceback
        print(f" -> ERROR in OSM Categorizer: {e}")
        traceback.print_exc()
        
    return df