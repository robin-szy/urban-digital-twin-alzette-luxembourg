"""The model on small synthetic frames: two communes side by side, with codes that disagree like the real ones."""
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from eddie_building_population.population_model import calculate_population_and_heights

CRS = "EPSG:2169"


def square(x, y, side=10):
    return box(x, y, x + side, y + side)


@pytest.fixture
def communes():
    return gpd.GeoDataFrame({"COMMUNE": ["Pétange", "Luxembourg"], "CANTON": ["A", "B"], "LAU2": ["0209", "0304"]},
                            geometry=[box(0, 0, 1000, 1000), box(1000, 0, 2000, 1000)], crs=CRS)


@pytest.fixture
def population():
    # COMMUNE_CODE is not LAU2: 304 here is another commune than LAU2 0304.
    return pd.DataFrame({"COMMUNE_CODE": [1, 209, 304], "COMMUNE_NOM": ["Luxembourg", "Petange", "Esch-sur-Alzette"],
                         "FEMMES_MINEURES": [1, 10, 100], "HOMMES_MINEURS": [2, 20, 200],
                         "FEMMES_MAJEURES": [3, 30, 300], "HOMMES_MAJEURS": [4, 40, 400]})


@pytest.fixture
def buildings():
    return gpd.GeoDataFrame({"gml_id": ["p1", "p2", "l1", "l2"]},
                            geometry=[square(100, 100), square(300, 300, 20), square(1100, 100), square(1500, 500)],
                            crs=CRS)


@pytest.fixture
def heights():
    return gpd.GeoDataFrame({"height": [10.0, 20.0], "floors": [3.0, 6.0], "type": ["residential", "non-residential"]},
                            geometry=[square(100, 100), square(1100, 100)], crs=CRS)


def calculate(buildings, communes, population, heights):
    return calculate_population_and_heights(buildings, communes, population, heights, CRS).set_index("gml_id")


def test_population_is_joined_by_commune_name_not_by_code(buildings, communes, population, heights):
    result = calculate(buildings, communes, population, heights)
    assert result.loc["l1", "commune"] == "Luxembourg"
    assert result.loc["l1", "population"] == 10  # Luxembourg's row, not Esch-sur-Alzette's under code 304
    assert result.loc["p1", "population"] == 100  # "Pétange" matches "Petange"


def test_every_population_column_counts(buildings, communes, population, heights):
    result = calculate(buildings, communes, population, heights)
    assert result.loc["l1", "population"] == 1 + 2 + 3 + 4


def test_commune_population_is_conserved_and_split_by_volume(buildings, communes, population, heights):
    result = calculate(buildings, communes, population, heights)
    assert result.groupby("lau2")["est_population"].sum().to_dict() == pytest.approx({"0209": 100.0, "0304": 10.0})
    # p1: 100 m2 x 10 m = 1000 m3. p2 has no height within 30 m: 400 m2 falls back to 18 m = 7200 m3.
    assert result.loc["p1", "final_height_m"] == 10.0
    assert result.loc["p2", "final_height_m"] == 18.0
    assert result.loc["p1", "est_population"] == pytest.approx(100 * 1000 / 8200)


def test_estimate_of_a_building_does_not_depend_on_the_aoi(buildings, communes, population, heights):
    """Filtering after the model keeps the whole commune as the denominator; an AOI only selects rows."""
    aoi = box(0, 0, 200, 200)
    first = calculate(buildings.assign(in_aoi=buildings.intersects(aoi)), communes, population, heights)
    other = box(0, 0, 2000, 2000)
    second = calculate(buildings.assign(in_aoi=buildings.intersects(other)), communes, population, heights)
    assert first.loc["p1", "in_aoi"] and not first.loc["p2", "in_aoi"]
    assert first.loc["p1", "est_population"] == second.loc["p1", "est_population"]
