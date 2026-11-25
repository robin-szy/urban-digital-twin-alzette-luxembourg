"""Tests of the rain per sub-catchment: areas, grid weights, averages and levels of detail."""

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from eddie_weather import catchment as c


# --- areas --------------------------------------------------------------------------------

def test_area_of_a_tenth_of_a_degree_cell_in_luxembourg():
    # 0.1 deg of latitude is 11.12 km; 0.1 deg of longitude at 49.6 N is 11.12 x cos(49.6) = 7.20 km.
    cell = box(6.05, 49.55, 6.15, 49.65)
    assert c.area_km2(cell) == pytest.approx(11.119 * 11.119 * np.cos(np.radians(49.6)), rel=1e-3)


def test_sub_catchments_remove_the_upstream_area():
    up = box(6.0, 49.5, 6.1, 49.6)
    down = box(6.0, 49.5, 6.2, 49.6)  # contains `up`
    subs = c.sub_catchments({"up": up, "down": down}, {"up": [], "down": ["up"]})
    assert c.area_km2(subs["up"]) == pytest.approx(c.area_km2(up))
    assert c.area_km2(subs["down"]) == pytest.approx(c.area_km2(down) - c.area_km2(up), rel=1e-6)
    assert not subs["down"].intersection(subs["up"]).area > 1e-12


def test_slivers_from_slightly_different_outlines_are_dropped():
    up = box(6.0, 49.5, 6.1, 49.6)
    down = box(5.9999, 49.5, 6.2, 49.6)  # 7 m wider on the west side than the upstream outline
    subs = c.sub_catchments({"up": up, "down": down}, {"down": ["up"]})
    assert subs["down"].geom_type == "Polygon"
    assert subs["down"].bounds[0] == pytest.approx(6.1)


def test_an_empty_sub_catchment_is_an_error():
    same = box(6.0, 49.5, 6.1, 49.6)
    with pytest.raises(ValueError):
        c.sub_catchments({"a": same, "b": same}, {"b": ["a"]})


# --- grid and weights ---------------------------------------------------------------------

def test_grid_points_are_on_the_era5_land_grid():
    points = c.grid_points(box(6.02, 49.52, 6.18, 49.58))
    lats = sorted({p[0] for p in points})
    lons = sorted({p[1] for p in points})
    assert lats == [49.5, 49.6]
    assert lons == [6.0, 6.1, 6.2]


def test_an_area_inside_one_cell_takes_that_cell_only():
    area = box(6.07, 49.57, 6.13, 49.63)  # inside the cell of (49.6, 6.1)
    places = c.Places.build({"a": area})
    w = dict(zip(places.points, places.weights[0]))
    assert w[(49.6, 6.1)] == pytest.approx(1.0)


def test_an_area_on_four_cells_takes_a_quarter_of_each():
    area = box(6.0, 49.5, 6.1, 49.6)  # centred on (49.55, 6.05), the corner of four cells
    places = c.Places.build({"a": area})
    shares = sorted(w for w in places.weights[0] if w > 0)
    assert len(shares) == 4
    assert np.allclose(shares, 0.25, atol=2e-3)  # cells are not exactly equal: latitude


def test_rows_sum_to_one_and_a_too_small_grid_is_refused():
    areas = {"a": box(6.0, 49.5, 6.23, 49.71), "b": box(6.2, 49.6, 6.41, 49.8)}
    places = c.Places.build(areas)
    assert np.allclose(places.weights.sum(axis=1), 1.0)
    with pytest.raises(ValueError):
        c.cell_weights(areas, [(49.6, 6.1)])


def test_uniform_rain_stays_the_same_and_a_ramp_gives_the_mean():
    areas = {"a": box(6.05, 49.55, 6.35, 49.65)}
    places = c.Places.build(areas)
    assert places.average(np.full(len(places.points), 2.5))[0] == pytest.approx(2.5)
    # Rain rising with longitude: the box covers the cells 6.1, 6.2 and 6.3 exactly, so its
    # rain is their mean, the value at its centre.
    lon = np.array([p[1] for p in places.points])
    field = 10.0 * (lon - 6.0)
    assert places.average(field)[0] == pytest.approx(10.0 * (6.2 - 6.0), rel=1e-2)


def test_a_missing_point_is_filled_by_the_others_of_the_place():
    places = c.Places.build({"a": box(6.0, 49.5, 6.1, 49.6)})  # on four cells
    values = np.full((2, len(places.points)), 4.0)
    values[0, 0] = np.nan
    values[1, :] = np.nan
    result = places.average(values)
    assert result[0, 0] == pytest.approx(4.0)
    assert np.isnan(result[1, 0])


def test_combining_places_weights_them_by_area():
    areas = {"small": box(6.0, 49.5, 6.1, 49.6), "big": box(6.1, 49.5, 6.4, 49.6)}
    places = c.Places.build(areas)
    mean = places.combine(["small", "big"], [[1.0, 5.0]])
    assert mean[0] == pytest.approx(4.0, rel=1e-3)  # one quarter at 1 mm, three quarters at 5 mm


def test_geojson_of_the_places():
    areas = {"a": box(6.0, 49.5, 6.1, 49.6)}
    places = c.Places.build(areas)
    gj = places.to_geojson(areas, {"a": {"gauge": "Livange"}})
    feature = gj["features"][0]
    assert feature["properties"]["gauge"] == "Livange"
    assert feature["properties"]["area_km2"] == pytest.approx(c.area_km2(areas["a"]), abs=0.01)
    assert c.read_geojson(gj, "id")["a"].equals(areas["a"])


# --- volumes ------------------------------------------------------------------------------

def test_volume_one_mm_on_one_km2_is_a_thousand_m3():
    assert c.rain_volume_m3(1.0, 1.0) == pytest.approx(1000.0)
    assert np.allclose(c.rain_volume_m3([10.0, 2.0], [258.34, 59.4]), [2583400.0, 118800.0])


# --- levels of detail ---------------------------------------------------------------------

def hourly(start, end, value=1.0):
    index = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
    return pd.Series(value, index=index)


def test_days_are_cut_at_local_midnight_and_last_23_or_25_hours():
    # Clocks go forward on 31 March 2024 and back on 27 October 2024.
    rain = hourly("2024-03-29 23:00", "2024-04-02 22:00")
    days = c.aggregate(rain, "day")
    by_day = dict(zip(days["start"].dt.tz_convert("Europe/Luxembourg").dt.strftime("%m-%d"), days["value"]))
    assert by_day["03-30"] == 24 and by_day["03-31"] == 23 and by_day["04-01"] == 24
    assert days["complete"].all()
    autumn = c.aggregate(hourly("2024-10-25 22:00", "2024-10-28 23:00"), "day")
    assert list(autumn["value"]) == [24, 25, 24]


def test_a_day_with_a_missing_hour_is_marked_incomplete():
    rain = hourly("2024-06-30 22:00", "2024-07-02 22:00")
    rain = rain.drop(rain.index[5])
    days = c.aggregate(rain, "day")
    assert list(days["count"]) == [23, 24]
    assert list(days["complete"]) == [False, True]


def test_seasons_put_december_in_the_next_winter():
    rain = hourly("2023-11-30 23:00", "2024-03-01 23:00", 0.1)
    seasons = c.aggregate(rain, "season")
    starts = list(seasons["start"].dt.tz_convert("Europe/Luxembourg").dt.strftime("%Y-%m-%d"))
    assert starts == ["2023-12-01", "2024-03-01"]
    # Winter 2024: 91 days (leap year), no clock change, so 91 x 24 hours.
    assert seasons["value"].iloc[0] == pytest.approx(91 * 24 * 0.1)
    assert bool(seasons["complete"].iloc[0]) and not bool(seasons["complete"].iloc[1])


def test_months_years_quarter_hours_and_means():
    rain = hourly("2023-12-31 23:00", "2025-12-31 23:00")
    months = c.aggregate(rain, "month")
    assert len(months) == 24 and months["complete"].all()
    assert months["value"].iloc[1] == 29 * 24  # February 2024
    assert months["value"].iloc[2] == 31 * 24 - 1  # March 2024 loses an hour
    years = c.aggregate(rain, "year")
    assert list(years["value"]) == [366 * 24, 365 * 24]
    quarter = pd.Series([1.0, 2.0, 3.0, 4.0],
                        index=pd.date_range("2024-07-01 10:00", periods=4, freq="15min", tz="UTC"))
    hours = c.aggregate(quarter, "hour", how="mean")
    assert list(hours["value"]) == [2.5] and bool(hours["complete"].iloc[0])
    with pytest.raises(ValueError):
        c.aggregate(quarter.tz_localize(None), "hour")
    with pytest.raises(ValueError):
        c.aggregate(quarter, "week")


def test_model_days_run_on_utc_plus_one_all_year():
    rain = hourly("2024-03-30 23:00", "2024-04-01 23:00")  # 30 Mar 23:00 UTC = 31 Mar 00:00 UTC+1
    days = c.daily_for_model(rain)
    assert list(days.index.strftime("%m-%d")) == ["03-31", "04-01"]
    assert list(days) == [24.0, 24.0]  # no 23-hour day: the model clock never changes
    gap = rain.drop(rain.index[3])
    assert np.isnan(c.daily_for_model(gap).iloc[0])
