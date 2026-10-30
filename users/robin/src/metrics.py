import geopandas as gpd
import pandas as pd


def area_by_attribute(gdf: gpd.GeoDataFrame, attribute: str) -> pd.Series:
    """Total geometry area per unique value of `attribute`.

    Units follow the GeoDataFrame's CRS (m^2 for EPSG:2169). Reusable
    wherever a metric needs "area broken down by some category" - e.g. land
    cover class here, later building type/depth class for A2/A3.
    """
    return gdf.geometry.area.groupby(gdf[attribute]).sum()
