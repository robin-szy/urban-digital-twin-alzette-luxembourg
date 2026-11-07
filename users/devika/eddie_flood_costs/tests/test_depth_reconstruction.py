"""Synthetic sample/cell checks and a two-building reconstruction run."""
import csv
import gzip
import json

import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.geometry import mapping

from flood_costs import depth_reconstruction as depth


def test_cell_assignment_retains_ambiguous_and_missing_samples():
    cells = gpd.GeoDataFrame({"chainage_m": [35000, 35100]},
                            geometry=[shapely.box(0, 0, 1, 1), shapely.box(1, 0, 2, 1)], crs=2169)
    assigned, kinds = depth.assign_cells(np.array([[.5, .5], [1., .5], [0., .5], [3., 3.]]), cells)
    assert assigned.tolist() == [35000, -1, 35000, -1]
    assert kinds.tolist() == ["within", "ambiguous_chainage", "unique_chainage_boundary", "outside_water_cells"]


@pytest.mark.parametrize("scenario,wet_depth", [("hq10", 1.), ("hq100", 2.)])
def test_partial_flooding_summaries_include_dry_and_missing_samples(scenario, wet_depth):
    assert scenario in depth.SCENARIOS
    result = depth.summarize_depth(
        np.array([0., wet_depth, np.nan]), np.array([True, True, False]),
        np.array([False, True, True]), np.array([35000, 35000, -1]),
        np.array(["within", "within", "outside_water_cells"]), np.array([100., 100., np.nan]))
    assert result["min_m"] == result["mean_m"] == result["max_m"] == wet_depth
    assert result["all_valid_mean_m"] == pytest.approx(wet_depth / 2)
    assert result["wet_sample_fraction"] == .5
    assert result["samples_wet"] == result["samples_dry"] == result["samples_missing"] == 1
    assert result["sample_coverage_fraction"] == pytest.approx(2 / 3)
    assert "missing_ground" in result["quality_flags"]
    assert "incomplete_sample_coverage" in result["quality_flags"]


@pytest.mark.parametrize("valid,expected_mean", [(True, 0.), (False, None)])
def test_all_dry_is_distinct_from_unknown_depth(valid, expected_mean):
    result = depth.summarize_depth(
        np.array([0. if valid else np.nan]), np.array([valid]), np.array([False]),
        np.array([35000]), np.array(["within"]), np.array([100. if valid else np.nan]))
    assert result["all_valid_mean_m"] == expected_mean
    assert result["min_m"] is None and result["mean_m"] is None and result["max_m"] is None
    assert result["samples_dry"] == int(valid)
    assert result["samples_missing"] == int(not valid)


def test_in_memory_dem_sampler_preserves_missing_and_outside_pixels():
    dem = depth.WindowedDEM.__new__(depth.WindowedDEM)
    dem.gt = [0., 1., 0., 2., 0., -1.]
    dem.x0 = dem.y0 = 0
    dem.data = np.array([[100., np.nan], [101., 102.]])
    observed = dem.values([[.5, 1.5], [1.5, 1.5], [.5, .5], [3., 3.]])
    np.testing.assert_allclose(observed, [100., np.nan, 101., np.nan], equal_nan=True)


def test_reconstructs_both_scenarios_with_official_mask_and_nulls(tmp_path, monkeypatch):
    """Exercise the actual main-loop depth formula, using only 24 footprint samples."""
    monkeypatch.setenv("FLOOD_COSTS_PROJECT_ROOT", str(tmp_path))
    # Configure explicitly so embedded reference functions can be bound; all paths
    # refer to this test's temporary folder, never the local research project.
    depth.configure()
    namespace, hashes = depth.load_reference_functions()
    origin_x, origin_y = 80000., 75000.
    first = shapely.box(origin_x, origin_y, origin_x + 2, origin_y + 2)
    second = shapely.box(origin_x + 4, origin_y, origin_x + 6, origin_y + 2)
    study = shapely.box(origin_x - 1, origin_y - 1, origin_x + 7, origin_y + 3)
    footprints = gpd.GeoDataFrame({"buildingid": ["ACT_KNOWN", "ACT_MISSING"],
                                  "gml_id": ["synthetic-1", "synthetic-2"],
                                  "commune": ["Synthetic", "Synthetic"], "lau2": ["0", "0"]},
                                 geometry=[first, second], crs=2169)
    markers = gpd.GeoDataFrame({"KILO_KM": [35., 35.1]},
                              geometry=shapely.points([[origin_x, origin_y], [origin_x + 6, origin_y]]), crs=2169)
    cells = gpd.GeoDataFrame({"chainage_m": [35000]}, geometry=[study], crs=2169)
    namespace["study_area"] = lambda: study
    namespace["water_cells"] = lambda *args: cells.copy()
    namespace["scenario_wse"] = lambda scenario: {35000: {"hq10": 101., "hq100": 102.}[scenario]}
    monkeypatch.setattr(depth, "load_reference_functions", lambda: (namespace, hashes))
    monkeypatch.setattr(depth.pyogrio, "read_dataframe",
                        lambda path, **kwargs: markers.copy() if path == depth.MARKERS_PATH else footprints.copy())

    # HQ10 covers half the first building and all of the missing-ground building;
    # HQ100 covers both whole buildings. Use the real extent loader on tiny JSON.
    extents = {"hq10": shapely.union_all([
        shapely.box(origin_x - 1, origin_y - 1, origin_x + 1, origin_y + 3),
        shapely.box(origin_x + 3, origin_y - 1, origin_x + 7, origin_y + 3)]), "hq100": study}
    for scenario, geometry in extents.items():
        path = depth.HAZARD_PATHS[scenario]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "FeatureCollection",
                                   "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2169"}},
                                   "features": [{"type": "Feature", "properties": {}, "geometry": mapping(geometry)}]}))
    depth.DEM_PATH.parent.mkdir(parents=True)
    depth.DEM_PATH.touch()
    depth.BUILDINGS_PATH.parent.mkdir(parents=True)
    depth.BUILDINGS_PATH.touch()
    depth.MARKERS_PATH.parent.mkdir(parents=True)
    depth.MARKERS_PATH.touch()

    class SyntheticDEM:
        def __init__(self, path, bounds):
            self.data = np.array([[100., np.nan]])
            self.metadata = {"synthetic": True}

        def values(self, points):
            return np.where(points[:, 0] < origin_x + 3, 100., np.nan)

        def close(self):
            pass

    monkeypatch.setattr(depth, "WindowedDEM", SyntheticDEM)
    depth.main()
    features = json.loads(depth.PRODUCTS[0].read_text())["features"]
    by_id = {f["properties"]["buildingid"]: f["properties"] for f in features}
    known, missing = by_id["ACT_KNOWN"], by_id["ACT_MISSING"]
    for scenario, expected in (("hq10", 1.), ("hq100", 2.)):
        assert known[f"depth_{scenario}_min_m"] == known[f"depth_{scenario}_max_m"] == expected
        assert missing[f"depth_{scenario}_all_valid_mean_m"] is None
        assert missing[f"samples_valid_{scenario}"] == 0
    assert 0 < known["wet_sample_fraction_hq10"] < 1
    assert known["wet_sample_fraction_hq100"] == 1
    with gzip.open(depth.PRODUCTS[2], "rt") as handle:
        samples = list(csv.DictReader(handle))
    assert {r["scenario"] for r in samples} == {"hq10", "hq100"}
    assert any(r["official_masked_depth_m"] == "0.0" for r in samples if r["buildingid"] == "ACT_KNOWN")
    assert all(r["official_masked_depth_m"] == "" for r in samples if r["buildingid"] == "ACT_MISSING")
    assert all(r["official_extent_contains_sample"] == "True" for r in samples
               if r["official_masked_depth_m"] not in ("", "0.0"))
