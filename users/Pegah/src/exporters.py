import pandas as pd
import geopandas as gpd
from shapely.ops import transform

def export_results(buildings_joined, csv_path, geojson_2d_path):
    # 1. Export CSV Table
    export_df = pd.DataFrame(buildings_joined.drop(columns=["geometry", "index_right", "index_left",
                                                            "ext_height", "canton", "commune_nom",
                                                            "femmes_mineures", "hommes_mineurs",
                                                            "femmes_majeures", "hommes_majeurs",
                                                            "population", "total_commune_volume_m3",
                                                            "vol_weight"], errors="ignore"))
    export_df.to_csv(csv_path, index=False)
    print(f"Saved CSV to {csv_path}")

    # 2. Export 2D GeoJSON Polygons
    polygons_wgs84 = buildings_joined.to_crs("EPSG:4326")
    polygons_wgs84 = polygons_wgs84.drop(
        columns=[
            "index_right", "index_left", "ext_height", "canton", "commune_nom",
            "femmes_mineures", "hommes_mineurs", "femmes_majeures", "hommes_majeures",
            "population", "total_commune_volume_m3","vol_weight"
        ], 
        errors="ignore"
    )
    polygons_wgs84.to_file(geojson_2d_path, driver="GeoJSON")
    print(f"Saved 2D GeoJSON to {geojson_2d_path}")