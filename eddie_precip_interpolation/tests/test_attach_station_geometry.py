import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from eddie_precip_interpolation.db import attach_station_geometry


def test_attach_station_geometry_merges_by_station_id():
    observations_df = pd.DataFrame({
        "station_id": ["AGM_013", "AGM_028"],
        "datetime": ["2026-09-25T01:00:00Z", "2026-09-29T09:00:00Z"],
        "value": [1.5, None],
    })
    stations_gdf = gpd.GeoDataFrame(
        {"station_id": ["AGM_013", "AGM_028"]},
        geometry=[Point(6.13, 49.58), Point(6.09, 49.61)],
        crs="EPSG:4326",
    )

    result = attach_station_geometry(observations_df, stations_gdf)

    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs == stations_gdf.crs
    by_station = result.set_index("station_id")
    assert by_station.loc["AGM_013", "geometry"] == Point(6.13, 49.58)
    assert by_station.loc["AGM_028", "geometry"] == Point(6.09, 49.61)


def test_attach_station_geometry_drops_observations_with_unknown_station():
    observations_df = pd.DataFrame({
        "station_id": ["UNKNOWN"],
        "datetime": ["2026-09-25T01:00:00Z"],
        "value": [1.5],
    })
    stations_gdf = gpd.GeoDataFrame(
        {"station_id": ["AGM_013"]},
        geometry=[Point(6.13, 49.58)],
        crs="EPSG:4326",
    )

    result = attach_station_geometry(observations_df, stations_gdf)

    assert result.empty
