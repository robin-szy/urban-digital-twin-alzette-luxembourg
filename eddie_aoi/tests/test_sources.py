"""Reading the marker and mapped-river sources, with the river as a parameter."""
import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.errors import PreparationError
from eddie_aoi.sources import available_rivers, read_markers, read_river_lines

from .conftest import write_river_sources


def test_lists_the_rivers_present(tmp_path):
    sources = write_river_sources(tmp_path)
    assert available_rivers(sources) == ["Alzette", "Sûre"]


def test_loads_a_non_alzette_river(tmp_path):
    # The river is a parameter, not a constant: this is what Devika's
    # build_corridor cannot do, since it embeds TOPONYME = 'Alzette'.
    sources = write_river_sources(tmp_path)

    markers = read_markers(sources, "Sûre", 35.0, 50.0)
    lines = read_river_lines(sources, "Sûre")

    assert len(markers) == 16
    assert set(markers["TOPONYME"]) == {"Sûre"}
    assert len(lines) == 1
    assert set(lines["FIRST_TOPO"]) == {"Sûre"}


def test_selects_only_the_requested_kilometre_range(tmp_path):
    sources = write_river_sources(tmp_path)
    markers = read_markers(sources, "Alzette", 40.0, 45.0)

    assert list(markers["KILO_KM"]) == [40.0, 41.0, 42.0, 43.0, 44.0, 45.0]


def test_tolerates_float_noise_in_the_kilometre_field(tmp_path):
    sources = write_river_sources(tmp_path)
    # 40.0004 must still find the marker at 40.0.
    markers = read_markers(sources, "Alzette", 40.0004, 45.0)
    assert float(markers["KILO_KM"].iloc[0]) == 40.0


def test_rejects_an_unknown_river_and_lists_the_available_ones(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError) as excinfo:
        read_markers(sources, "Thames", 35.0, 50.0)

    message = str(excinfo.value)
    assert "not found" in message and "Alzette" in message


def test_rejects_a_missing_start_marker(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError, match="requested start km"):
        read_markers(sources, "Alzette", 34.0, 45.0)


def test_rejects_a_missing_end_marker(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError, match="requested end km"):
        read_markers(sources, "Alzette", 40.0, 60.0)


def test_rejects_a_range_with_fewer_than_two_markers(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError, match="Fewer than two"):
        read_markers(sources, "Alzette", 40.4, 40.6)


def test_rejects_a_source_in_the_wrong_crs(tmp_path):
    sources = write_river_sources(tmp_path, crs="EPSG:4326")

    with pytest.raises(PreparationError, match="must declare EPSG:2169"):
        read_markers(sources, "Alzette", 35.0, 50.0)


def test_rejects_mapped_lines_without_a_name_field(tmp_path):
    sources = write_river_sources(tmp_path)
    lines = gpd.GeoDataFrame(
        [{"SOMETHING": "Alzette", "geometry": LineString([(0, 0), (1, 1)])}],
        crs=METRIC_CRS,
    )
    lines.to_file(sources.river_lines_path, driver="GPKG")

    with pytest.raises(PreparationError, match="none of the name fields"):
        read_river_lines(sources, "Alzette")


def test_rejects_a_river_absent_from_the_mapped_lines(tmp_path):
    sources = write_river_sources(tmp_path)

    with pytest.raises(PreparationError, match="No mapped river lines named"):
        read_river_lines(sources, "Thames")


# --- source hashing ----------------------------------------------------------


def test_source_hash_is_stable_for_unchanged_files(tmp_path):
    sources = write_river_sources(tmp_path)
    assert sources.source_sha256() == sources.source_sha256()


def test_source_hash_changes_when_an_input_changes(tmp_path):
    sources = write_river_sources(tmp_path)
    before = sources.source_sha256()

    # Republish the mapped-river layer with an extra feature.
    lines = gpd.GeoDataFrame(
        [
            {"FIRST_TOPO": "Alzette", "geometry": LineString([(0, 0), (15000, 0)])},
            {"FIRST_TOPO": "Alzette", "geometry": LineString([(0, 5), (100, 5)])},
        ],
        crs=METRIC_CRS,
    )
    lines.to_file(sources.river_lines_path, driver="GPKG")

    assert sources.source_sha256() != before


def test_source_hash_rejects_a_missing_file(tmp_path):
    sources = write_river_sources(tmp_path)
    sources.river_lines_path.unlink()

    with pytest.raises(PreparationError, match="does not exist"):
        sources.source_sha256()
