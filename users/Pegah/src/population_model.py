import os
import re
import unicodedata

import geopandas as gpd
import pandas as pd


def _clean_columns(df):
    """Strip whitespace, byte-order mark and uppercase from column names."""
    return df.rename(columns=lambda c: str(c).replace("\ufeff", "").strip().lower())


def _normalize_name(value):
    """Accent-, case- and punctuation-insensitive commune name key."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


def _read_buildings(buildings_path, buildings_layer=None):
    """Read building footprints from any OGR source (.gpkg, .shp, ...).

    The pipeline runs on ``ACT2023v2_Buildings3d_Footprints.gpkg``: a single
    feature layer in EPSG:2169 with ``buildingid``, ``gml_id``, ``commune``,
    ``lau2``, ``area`` and ``zmin`` - the objects the digital twin displays.
    It carries no heights or floors: those still come from the LU00 register.
    """
    read_kwargs = {}
    if buildings_layer:
        read_kwargs["layer"] = buildings_layer
    return gpd.read_file(buildings_path, **read_kwargs)


def calculate_population_and_heights(
    buildings_path,
    commune_path,
    population_path,
    height_gpkg_path,
    target_crs,
    buildings_layer=None,
):
    os.environ["SHAPE_RESTORE_SHX"] = "YES"

    # Load Datasets
    commune_gdf = gpd.read_file(commune_path)
    population_df = pd.read_csv(population_path)
    raw_buildings = _read_buildings(buildings_path, buildings_layer)
    height_gpkg = gpd.read_file(height_gpkg_path)

    # Lowercase column names across datasets
    commune_gdf = _clean_columns(commune_gdf)
    population_df = _clean_columns(population_df)
    raw_buildings = _clean_columns(raw_buildings)
    height_gpkg = _clean_columns(height_gpkg)

    # Standardize Commune Identifiers
    def standardize_commune_id(df, name):
        possible_keys = ["lau2", "la_id", "commune_code", "id_commune", "id"]
        for key in possible_keys:
            if key in df.columns:
                if key != "lau2":
                    df = df.rename(columns={key: "lau2"})
                return df
        raise KeyError(f"Could not find a valid commune identifier column in {name}.")

    commune_gdf = standardize_commune_id(commune_gdf, os.path.basename(commune_path))
    population_df = standardize_commune_id(population_df, os.path.basename(population_path))

    commune_gdf["lau2"] = commune_gdf["lau2"].astype(str).str.strip().str.zfill(4)
    population_df["lau2"] = population_df["lau2"].astype(str).str.strip().str.zfill(4)

    # Keep only national buildings. The ACT2023 footprint file also holds
    # buildings in the neighbouring countries ("9999") and border ribbons
    # straddling two countries ("9999/9998"), which have no commune to join to.
    if "lau2" in raw_buildings.columns:
        building_lau2 = raw_buildings["lau2"].astype(str).str.strip()
        raw_buildings = raw_buildings.loc[
            building_lau2.str.fullmatch(r"\d{4}") & (building_lau2 != "9999")
        ].copy()

    # Prepare Building Geometries & Extract Physical Heights
    buildings = raw_buildings.explode(index_parts=False).reset_index(drop=True)

    # Drop the GeoPackage row identifier and keep the footprint area measured in
    # the source CRS as a separate column from the reprojected one.
    buildings = buildings.drop(columns=["id"], errors="ignore")
    buildings = buildings.rename(columns={"area": "source_area_m2"})

    buildings = buildings.to_crs(target_crs)
    buildings["geometry"] = buildings.geometry.force_2d() if hasattr(buildings.geometry, "force_2d") else buildings.geometry
    buildings["footprint_m2"] = buildings.geometry.area

    # Spatial Join with LU00.gpkg
    height_gpkg_clean = height_gpkg.drop(columns=["index_right", "index_left"], errors="ignore")
    buildings_in_gpkg_crs = buildings.to_crs(height_gpkg.crs)
    buildings_in_gpkg_crs["geometry"] = buildings_in_gpkg_crs.geometry.centroid

    merged_heights = gpd.sjoin_nearest(
        buildings_in_gpkg_crs,
        height_gpkg_clean[["height", "floors", "type", "geometry"]],
        how="left",
        max_distance=30,
        distance_col="dist_to_building_m",
    )
    merged_heights = merged_heights[~merged_heights.index.duplicated(keep="first")]
    merged_heights = merged_heights.reindex(buildings.index)

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
    pop_columns = [col for col in ["femmes_mineures", "hommes_mineurs", "femmes_majeures", "hommes_majeurs"] if col in population_df.columns]
    if pop_columns:
        population_df["population"] = population_df[pop_columns].sum(axis=1)

    # Join key for the population table. The RNPP file ships a "commune_code"
    # that does not follow the LAU2 coding of the commune layer (it labels
    # Luxembourg city "0001" instead of "0304"), which silently mismatches most
    # municipalities, so its commune names are used whenever both layers carry
    # them; the lau2 codes remain the fallback for files without a name column.
    pop_name_col = next(
        (col for col in ["commune_nom", "commune_name", "nom", "name"] if col in population_df.columns),
        None,
    )
    if pop_name_col and "commune" in commune_gdf.columns:
        commune_gdf = commune_gdf.assign(_name_key=commune_gdf["commune"].map(_normalize_name))
        population_df = population_df.assign(_name_key=population_df[pop_name_col].map(_normalize_name))
        # "lau2" here is the population file's own code column: dropped so the
        # commune layer stays the single source of the LAU2 identifier.
        population_df = population_df.drop(columns=["lau2"], errors="ignore")
        merge_on = "_name_key"
    else:
        merge_on = "lau2"

    communes_with_pop = (
        commune_gdf.merge(population_df, on=merge_on, how="left")
        .drop(columns=["_name_key"], errors="ignore")
        .to_crs(target_crs)
    )

    # The commune identifier and name are re-attached from the commune layer,
    # so they are dropped here to avoid duplicated columns in the join.
    centroids = buildings.drop(columns=["commune", "lau2"], errors="ignore").copy()
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
