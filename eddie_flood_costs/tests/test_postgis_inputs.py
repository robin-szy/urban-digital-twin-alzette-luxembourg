"""PostgisInputs selects what the file inputs select; the frozen functions read markers through it."""
import geopandas as gpd
import pytest
from shapely.geometry import Point, box

pytest.importorskip("eddie_base_data")
pytest.importorskip("eddie")

from flood_costs import depth_reconstruction as depth  # noqa: E402
from flood_costs import postgis_inputs  # noqa: E402
from flood_costs.postgis_inputs import PostgisInputs  # noqa: E402

LAYER = "Kilometrierung primäre Gewässer"


@pytest.fixture
def stored_markers(monkeypatch):
    table = gpd.GeoDataFrame(
        {"objectid": [1, 2, 3, 4], "toponyme": ["Alzette", "Alzette", "Alzette", "Sûre"], "kilo_km": [36.0, 35.0, 60.0, 35.0]},
        geometry=[Point(77000, 75000), Point(77100, 75000), Point(77200, 75000), Point(60000, 110000)], crs=2169)
    monkeypatch.setattr(postgis_inputs, "read_all", lambda conn, name, order_by: table)
    return table


def test_markers_are_the_alzette_range_in_km_order(stored_markers):
    assert PostgisInputs(None).markers(LAYER)["KILO_KM"].tolist() == [35.0, 36.0]


def test_frozen_functions_read_markers_through_their_namespace(stored_markers):
    namespace = {"gpd": gpd, "KILOMETRAGE_GPKG": None}
    PostgisInputs(None).bind(namespace)
    assert namespace["KILOMETRAGE_GPKG"].is_file()
    assert sorted(namespace["gpd"].read_file(namespace["KILOMETRAGE_GPKG"], layer=LAYER)["TOPONYME"].unique()) == ["Alzette", "Sûre"]
    assert namespace["gpd"].GeoDataFrame is gpd.GeoDataFrame


def test_extent_is_clipped_to_the_marker_crop(stored_markers, monkeypatch):
    markers = PostgisInputs(None).markers(LAYER)
    crop = depth.crop_around_markers(markers)
    inside, straddling = box(77000, 75000, 77010, 75010), box(crop.bounds[2] - 5, 75000, crop.bounds[2] + 5, 75010)
    parts = gpd.GeoDataFrame({"part_id": [1, 0]}, geometry=[straddling, inside], crs=2169)
    monkeypatch.setattr(postgis_inputs, "read_in_aoi", lambda conn, table, aoi: parts)
    assert PostgisInputs(None).extent("hq10", markers).area == pytest.approx(100 + 50)


def test_empty_extent_is_refused(stored_markers, monkeypatch):
    markers = PostgisInputs(None).markers(LAYER)
    empty = gpd.GeoDataFrame({"part_id": []}, geometry=[], crs=2169)
    monkeypatch.setattr(postgis_inputs, "read_in_aoi", lambda conn, table, aoi: empty)
    with pytest.raises(RuntimeError, match="hq10"):
        PostgisInputs(None).extent("hq10", markers)
