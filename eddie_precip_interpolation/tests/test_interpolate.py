import math

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box

from eddie_precip_interpolation.interpolate import IDWConfig, build_grid, idw_grid


def _aoi():
    # Small metric AOI in EPSG:2169 for fast unit tests.
    return gpd.GeoDataFrame({"name": ["test"]}, geometry=[box(0, 0, 200, 200)], crs="EPSG:2169")


def _stations():
    # Four gauges surrounding the AOI, all within the search radius.
    return gpd.GeoDataFrame(
        {"station_id": ["A", "B", "C", "D"]},
        geometry=[Point(-100, -100), Point(300, -100), Point(-100, 300), Point(300, 300)],
        crs="EPSG:2169",
    )


def _observations(value_d=None):
    return pd.DataFrame(
        {
            "station_id": ["A", "B", "C", "D"],
            "datetime": ["2026-10-17T14:00:00Z"] * 4,
            "value": [1.0, 2.0, 3.0, value_d if value_d is not None else 4.0],
        }
    )


def test_build_grid_100m_over_200m_square_has_four_cells():
    grid = build_grid(_aoi(), resolution_m=100)
    assert len(grid) == 4
    assert set(grid.columns) >= {"grid_id", "center_x", "center_y", "geometry"}
    assert str(grid.crs).upper() == "EPSG:2169"


def test_idw_grid_returns_precipitation_and_quality_fields():
    cfg = IDWConfig(
        resolution_m=100,
        power=1,
        max_stations=4,
        search_radius_m=1_000,
        min_stations=3,
    )
    result = idw_grid(_stations(), _observations(), _aoi(), "2026-10-17T14:00:00Z", cfg)

    assert len(result) == 4
    expected = {
        "grid_id",
        "datetime",
        "precipitation_mm",
        "method",
        "power",
        "stations_used",
        "nearest_station_km",
        "inside_convex_hull",
        "spatial_status",
        "fallback_used",
        "quality_class",
        "geometry",
    }
    assert expected.issubset(result.columns)
    assert result["precipitation_mm"].notna().all()
    assert (result["stations_used"] == 4).all()
    assert (result["method"] == "IDW").all()
    assert (result["power"] == 1).all()
    assert result["inside_convex_hull"].all()
    assert (result["spatial_status"] == "interpolation").all()
    assert (result["quality_class"] == "HIGH").all()


def test_missing_value_is_excluded_not_zero_and_station_count_drops():
    obs = _observations()
    obs.loc[obs["station_id"] == "D", "value"] = None
    cfg = IDWConfig(
        resolution_m=100,
        power=1,
        max_stations=5,
        search_radius_m=1_000,
        min_stations=3,
    )
    result = idw_grid(_stations(), obs, _aoi(), "2026-10-17T14:00:00Z", cfg)

    assert result["precipitation_mm"].notna().all()
    assert (result["stations_used"] == 3).all()
    assert (result["missing_station_count_at_t"] == 1).all()
    # With one corner gauge missing, three cells remain inside the active
    # three-station hull (MODERATE) and the far corner becomes extrapolation (LOW).
    assert set(result["quality_class"]) == {"MODERATE", "LOW"}
    assert (result["quality_class"] == "MODERATE").sum() == 3
    assert (result["quality_class"] == "LOW").sum() == 1


def test_less_than_minimum_stations_returns_nodata_and_insufficient_quality():
    obs = _observations()
    obs.loc[obs["station_id"].isin(["C", "D"]), "value"] = None
    cfg = IDWConfig(
        resolution_m=100,
        power=1,
        max_stations=5,
        search_radius_m=1_000,
        min_stations=3,
    )
    result = idw_grid(_stations(), obs, _aoi(), "2026-10-17T14:00:00Z", cfg)

    assert result["precipitation_mm"].isna().all()
    assert (result["stations_used"] == 2).all()
    assert (result["quality_class"] == "INSUFFICIENT").all()


def test_outside_convex_hull_is_marked_as_extrapolation():
    # Three gauges all to the left of the AOI, but close enough to interpolate.
    stations = gpd.GeoDataFrame(
        {"station_id": ["A", "B", "C"]},
        geometry=[Point(-100, 0), Point(-100, 100), Point(-100, 200)],
        crs="EPSG:2169",
    )
    obs = pd.DataFrame(
        {
            "station_id": ["A", "B", "C"],
            "datetime": ["2026-10-17T14:00:00Z"] * 3,
            "value": [1.0, 2.0, 3.0],
        }
    )
    cfg = IDWConfig(
        resolution_m=100,
        power=1,
        max_stations=3,
        search_radius_m=1_000,
        min_stations=3,
    )
    result = idw_grid(stations, obs, _aoi(), "2026-10-17T14:00:00Z", cfg)

    assert (~result["inside_convex_hull"]).all()
    assert (result["spatial_status"] == "extrapolation").all()
    assert (result["quality_class"] == "LOW").all()


def test_exact_station_location_uses_observed_value_directly():
    aoi = gpd.GeoDataFrame({"name": ["tiny"]}, geometry=[box(0, 0, 100, 100)], crs="EPSG:2169")
    # Grid centre is (50, 50); station A is exactly there.
    stations = gpd.GeoDataFrame(
        {"station_id": ["A", "B", "C"]},
        geometry=[Point(50, 50), Point(-50, 50), Point(150, 50)],
        crs="EPSG:2169",
    )
    obs = pd.DataFrame(
        {
            "station_id": ["A", "B", "C"],
            "datetime": ["2026-10-17T14:00:00Z"] * 3,
            "value": [7.5, 1.0, 2.0],
        }
    )
    cfg = IDWConfig(
        resolution_m=100,
        power=1,
        max_stations=3,
        search_radius_m=1_000,
        min_stations=3,
    )
    result = idw_grid(stations, obs, aoi, "2026-10-17T14:00:00Z", cfg)

    assert len(result) == 1
    assert math.isclose(result.iloc[0]["precipitation_mm"], 7.5)
