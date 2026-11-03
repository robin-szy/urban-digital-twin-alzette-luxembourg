import os
import numpy as np
import geopandas as gpd
import pandas as pd

def calculate_population_and_heights(buildings_path, commune_path, population_path, height_gpkg_path, target_crs):
    os.environ["SHAPE_RESTORE_SHX"] = "YES"

    # Load Datasets
    commune_gdf = gpd.read_file(commune_path)
    population_df = pd.read_csv(population_path)
    raw_buildings = gpd.read_file(buildings_path)
    height_gpkg = gpd.read_file(height_gpkg_path)

    # Lowercase column names across datasets
    commune_gdf.columns = commune_gdf.columns.str.strip().str.lower()
    population_df.columns = population_df.columns.str.strip().str.lower()
    raw_buildings.columns = raw_buildings.columns.str.strip().str.lower()
    height_gpkg.columns = height_gpkg.columns.str.strip().str.lower()

    # Standardize Commune Identifiers
    def standardize_commune_id(df, name):
        possible_keys = ["lau2", "la_id", "commune_code", "id_commune", "id"]
        for key in possible_keys:
            if key in df.columns:
                if key != "lau2":
                    df = df.rename(columns={key: "lau2"})
                return df
        raise KeyError(f"Could not find a valid commune identifier column in {name}.")

    commune_gdf = standardize_commune_id(commune_gdf, "LIMADM_COMMUNES.shp")
    population_df = standardize_commune_id(population_df, "rnpp_population_per_municipality.csv")

    commune_gdf["lau2"] = commune_gdf["lau2"].astype(str).str.strip().str.zfill(4)
    population_df["lau2"] = population_df["lau2"].astype(str).str.strip().str.zfill(4)

    # Prepare Building Geometries & Extract Physical Heights
    buildings = raw_buildings.explode(index_parts=False).reset_index(drop=True)

    buildings = buildings.to_crs(target_crs)
    buildings["geometry"] = buildings.geometry.force_2d() if hasattr(buildings.geometry, 'force_2d') else buildings.geometry
    buildings["footprint_m2"] = buildings.geometry.area

    # Spatial Join with LU00.gpkg
    height_gpkg_clean = height_gpkg.drop(columns=['index_right', 'index_left'], errors='ignore')
    buildings_in_gpkg_crs = buildings.to_crs(height_gpkg.crs)
    buildings_in_gpkg_crs["geometry"] = buildings_in_gpkg_crs.geometry.centroid

    merged_heights = gpd.sjoin_nearest(
        buildings_in_gpkg_crs,
        height_gpkg_clean[['height', 'floors', 'type', 'geometry']],
        how="left",
        max_distance=30,
        distance_col="dist_to_building_m"
    )
    merged_heights = merged_heights[~merged_heights.index.duplicated(keep='first')]

    buildings["ext_height"] = merged_heights["height"]
    buildings["floors"] = merged_heights["floors"]
    buildings["building_type"] = merged_heights["type"]

    # Fallback Height Logic
    def determine_final_height(row):
        if pd.notna(row["ext_height"]) and row["ext_height"] > 0:
            return row["ext_height"]
        area = row["footprint_m2"]
        if area < 60:
            return 5.0
        elif area < 180:
            return 9.0
        elif area < 400:
            return 12.0
        else:
            return 18.0

    buildings["final_height_m"] = buildings.apply(determine_final_height, axis=1)
    buildings["volume_m3"] = buildings["footprint_m2"] * buildings["final_height_m"]

    # Aggregate Commune Population Data
    pop_columns = [col for col in ["femmes_mineures", "hommes_mineurs", "femmes_majeures", "hommes_majeures"] if col in population_df.columns]
    if pop_columns:
        population_df["population"] = population_df[pop_columns].sum(axis=1)

    communes_with_pop = commune_gdf.merge(population_df, on="lau2", how="left").to_crs(target_crs)

    centroids = buildings.copy()
    centroids["geometry"] = centroids.geometry.centroid

    buildings_joined = gpd.sjoin(
        centroids,
        communes_with_pop,
        how="inner",
        predicate="intersects"
    )

    buildings_joined["geometry"] = buildings.loc[buildings_joined.index, "geometry"]

    # Dasymetric Population Disaggregation by Building Volume
    commune_totals = (
        buildings_joined.groupby("lau2")["volume_m3"]
        .sum()
        .reset_index(name="total_commune_volume_m3")
    )

    buildings_joined = buildings_joined.merge(commune_totals, on="lau2", how="left")
    buildings_joined["vol_weight"] = buildings_joined["volume_m3"] / buildings_joined["total_commune_volume_m3"]
    buildings_joined["est_population"] = buildings_joined["vol_weight"] * buildings_joined["population"]

    # Extract Coordinates
    buildings_joined["x_coord_2169"] = buildings_joined.geometry.centroid.x
    buildings_joined["y_coord_2169"] = buildings_joined.geometry.centroid.y

    centroids_wgs84 = buildings_joined.geometry.centroid.to_crs(epsg=4326)
    buildings_joined["longitude"] = centroids_wgs84.x
    buildings_joined["latitude"] = centroids_wgs84.y

    return buildings_joined