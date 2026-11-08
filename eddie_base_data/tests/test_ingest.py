"""Source normalisation, without a database: every base table ends up the same shape."""
import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from eddie_base_data.datasets import Dataset
from eddie_base_data.ingest import load_table, load_vector, normalise, sha256_of

LUXEMBOURG_POINT_2169 = Point(77000, 75000)
LUXEMBOURG_POINT_3035 = gpd.GeoSeries([LUXEMBOURG_POINT_2169], crs=2169).to_crs(3035)[0]


def test_reprojects_to_2169_and_lowercases_columns():
    frame = gpd.GeoDataFrame({"OBJECTID": [1]}, geometry=[LUXEMBOURG_POINT_3035], crs=3035)
    result = normalise(frame, explode=False, name="f")
    assert result.crs.to_epsg() == 2169
    assert list(result.columns) == ["objectid", "geometry"]
    assert result.geometry[0].distance(LUXEMBOURG_POINT_2169) < 0.01


def test_drops_z():
    polygon = Polygon([(77000, 75000, 5), (77010, 75000, 5), (77010, 75010, 5)])
    result = normalise(gpd.GeoDataFrame(geometry=[polygon], crs=2169), explode=False, name="f")
    assert not result.geometry[0].has_z


def test_explode_gives_one_row_per_polygon():
    multi = MultiPolygon([box(77000, 75000, 77010, 75010), box(78000, 75000, 78010, 75010)])
    result = normalise(gpd.GeoDataFrame({"OBJECTID": [4]}, geometry=[multi], crs=2169), explode=True, name="f")
    assert result["part_id"].tolist() == [0, 1]
    assert result["objectid"].tolist() == [4, 4]
    assert set(result.geom_type) == {"Polygon"}


def test_coordinates_outside_luxembourg_are_refused():
    swapped = gpd.GeoSeries([LUXEMBOURG_POINT_3035], crs=3035).map(lambda p: Point(p.y, p.x)).set_crs(3035)
    with pytest.raises(ValueError, match="outside Luxembourg"):
        normalise(gpd.GeoDataFrame(geometry=swapped, crs=3035), explode=False, name="swapped.gml")


def test_declared_crs_overrides_a_prj_without_epsg_code(tmp_path):
    path = tmp_path / "communes.gpkg"
    gpd.GeoDataFrame({"LAU2": ["0304"]}, geometry=[box(77000, 75000, 77010, 75010)], crs=3857).to_file(path)
    result = load_vector(Dataset("t", path, declared_crs=2169))
    assert result.crs.to_epsg() == 2169
    assert result.total_bounds[0] == 77000


def test_csv_loses_its_bom_and_column_case(tmp_path):
    path = tmp_path / "population.csv"
    path.write_bytes("﻿COMMUNE_CODE,COMMUNE_NOM\n1,Luxembourg\n".encode("utf-8"))
    assert list(load_table(Dataset("t", path)).columns) == ["commune_code", "commune_nom"]


def test_hash_follows_content(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"a")
    before = sha256_of(path)
    assert sha256_of(path) == before
    path.write_bytes(b"b")
    assert sha256_of(path) != before
