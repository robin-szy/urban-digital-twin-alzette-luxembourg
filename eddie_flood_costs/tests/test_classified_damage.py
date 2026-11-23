"""General classified damage on a tiny synthetic run: no research data, no EDDIE."""
import csv
import gzip
import json

import geopandas as gpd
import pytest
import shapely

from flood_costs import SCENARIOS
from flood_costs import classified_damage as cd
from flood_costs.cost_lookup import build_lookup

X0, Y0 = 80000.0, 75000.0
DEPTHS = {"hq10": 0.5, "hq100": 1.0, "hqextreme": 2.0}
FRACTION = {"hq10": .25, "hq100": .4, "hqextreme": .6}       # synthetic curve in conftest
MAXIMUM = {"Residential": 150., "Commercial": 300., "Industrial": 450.}  # synthetic workbook


def _square(dx, size=10.0):
    return shapely.box(X0 + dx, Y0, X0 + dx + size, Y0 + size)


def _write_run(tmp_path, buildings, classification_rows, missing=()):
    """buildings: {id: polygon in EPSG:2169}. missing: ids whose samples are invalid."""
    depth = tmp_path / "depth_reconstruction"; depth.mkdir()
    frame = gpd.GeoDataFrame({"buildingid": list(buildings)}, geometry=list(buildings.values()), crs=2169)
    features = []
    for bid, geom in zip(frame.buildingid, frame.to_crs(4326).geometry):
        props = {"buildingid": bid, "footprint_area_m2": buildings[bid].area}
        for s in SCENARIOS:
            props.update({f"exposure_{s}_code": 2, f"samples_missing_{s}": 0, f"samples_wet_{s}": 1})
        features.append({"type": "Feature", "geometry": shapely.geometry.mapping(geom), "properties": props})
    (depth / "building_reconstructed_depths.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}))
    with gzip.open(depth / "building_depth_samples.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["buildingid", "scenario", "sample_index", "part_index",
                                                    "x_epsg2169", "y_epsg2169", "reconstruction_valid",
                                                    "official_masked_depth_m"])
        writer.writeheader()
        for bid, geom in buildings.items():
            minx, miny, maxx, maxy = geom.bounds
            points = [(minx + i + .5, miny + j + .5) for i in range(int(maxx - minx)) for j in range(int(maxy - miny))]
            for s in SCENARIOS:
                for k, (x, y) in enumerate(points):
                    ok = not (bid in missing and k == 0)
                    writer.writerow({"buildingid": bid, "scenario": s, "sample_index": k, "part_index": 0,
                                     "x_epsg2169": x, "y_epsg2169": y, "reconstruction_valid": ok,
                                     "official_masked_depth_m": DEPTHS[s] if ok else ""})
    classification = tmp_path / "classification"; classification.mkdir()
    path = classification / "building_classification.csv"
    fields = ["buildingid", "jrc_category", "classification_source", "classification_confidence",
              "reason_code", "classification_reason"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(classification_rows)
    return depth, path


def _row(bid, category, source, confidence=""):
    return {"buildingid": bid, "jrc_category": category, "classification_source": source,
            "classification_confidence": confidence, "reason_code": "test", "classification_reason": "synthetic"}


@pytest.fixture
def run_dir(tmp_path, synthetic_jrc_workbook):
    buildings = {"A": _square(0), "B": _square(20), "C": _square(40), "D": _square(60), "E": _square(80)}
    rows = [_row("A", "Residential", "landuse", "proxy"), _row("B", "Commercial", "osm", "high"),
            _row("C", "Unknown", "none"), _row("E", "Industrial", "reviewed", "medium")]
    depth, classification = _write_run(tmp_path, buildings, rows, missing={"E"})
    out = tmp_path / "monetary_damage"
    report = cd.run(depth, classification, synthetic_jrc_workbook, out)
    rows = {r["buildingid"]: r for r in csv.DictReader((out / "building_flood_damage.csv").open())}
    return out, report, rows


def test_every_classified_building_is_valued_for_every_scenario(run_dir):
    _, _, rows = run_dir
    for bid, category in (("A", "Residential"), ("B", "Commercial")):
        for s in SCENARIOS:
            expected = 100 * MAXIMUM[category] * FRACTION[s]
            assert float(rows[bid][f"damage_{s}_eur2010"]) == pytest.approx(expected, rel=1e-6)
            assert rows[bid][f"damage_{s}_status"] == "calculated_positive"


def test_unknown_is_null_never_zero(run_dir):
    _, _, rows = run_dir
    for s in SCENARIOS:
        assert rows["C"][f"damage_{s}_eur2010"] == ""
        assert rows["C"][f"damage_{s}_status"] == "unknown_classification"
        assert rows["C"]["classification_scope"] == cd.UNRESOLVED
        assert rows["C"]["classification_confidence"] == "unresolved"


def test_building_outside_classification_is_excluded_not_zero(run_dir):
    _, _, rows = run_dir
    assert rows["D"]["classification_scope"] == cd.EXCLUDED
    assert all(rows["D"][f"damage_{s}_eur2010"] == "" for s in SCENARIOS)
    assert all(rows["D"][f"damage_{s}_status"] == "excluded_outside_corridor" for s in SCENARIOS)


def test_missing_depth_sample_keeps_damage_null(run_dir):
    _, _, rows = run_dir
    for s in SCENARIOS:
        assert rows["E"][f"damage_{s}_eur2010"] == ""
        assert rows["E"][f"damage_{s}_status"] == "unknown_depth_coverage"


def test_confidence_mapping_fits_the_lookup(run_dir):
    _, _, rows = run_dir
    assert rows["A"]["classification_confidence"] == ""       # land-use proxy -> null
    assert rows["B"]["classification_confidence"] == "high"
    assert rows["E"]["classification_review_decision"] == "reviewed"


def test_lookup_accepts_the_output_and_totals_reconcile(run_dir):
    out, report, rows = run_dir
    lookup = build_lookup(out)
    assert report["dataset_version"] == lookup["dataset_version"]
    for s in SCENARIOS:
        expected = sum(float(r[f"damage_{s}_eur2010"]) for r in rows.values() if r[f"damage_{s}_eur2010"])
        assert lookup["summaries"][s]["total_eur2010"] == pytest.approx(expected)
        assert report["classified_totals_eur2010"][s.upper()] == pytest.approx(expected)
    assert report["counts"]["accepted_category_counts"] == {"Residential": 1, "Commercial": 1, "Industrial": 1}


def test_refuses_to_overwrite(run_dir, synthetic_jrc_workbook, tmp_path):
    out, _, _ = run_dir
    with pytest.raises(FileExistsError):
        cd.run(tmp_path / "depth_reconstruction", tmp_path / "classification/building_classification.csv",
               synthetic_jrc_workbook, out)


def test_shared_sliver_is_valued_once(tmp_path, synthetic_jrc_workbook):
    buildings = {"A": _square(0), "B": shapely.box(X0 + 9, Y0, X0 + 19, Y0 + 10)}   # 1 m x 10 m overlap
    rows = [_row("A", "Residential", "landuse", "proxy"), _row("B", "Residential", "landuse", "proxy")]
    depth, classification = _write_run(tmp_path, buildings, rows)
    out = tmp_path / "monetary"
    cd.run(depth, classification, synthetic_jrc_workbook, out)
    result = {r["buildingid"]: r for r in csv.DictReader((out / "building_flood_damage.csv").open())}
    assert float(result["A"]["shared_overlap_removed_from_valuation_m2"]) == pytest.approx(0, abs=1e-6)
    assert float(result["B"]["shared_overlap_removed_from_valuation_m2"]) == pytest.approx(10, rel=1e-3)
    total = sum(float(r["damage_hq100_eur2010"]) for r in result.values())
    assert total == pytest.approx(190 * 150 * .4, rel=1e-3)   # union area, not 200 m2


def test_unknown_category_names_become_unknown():
    fields = cd.classification_fields({"buildingid": "X", "jrc_category": "Agriculture",
                                       "classification_source": "osm"})
    assert fields["jrc_category"] is None and fields["classification_scope"] == cd.UNRESOLVED


def test_missing_inputs_are_reported(tmp_path, synthetic_jrc_workbook):
    with pytest.raises(FileNotFoundError):
        cd.run(tmp_path / "nope", tmp_path / "nope.csv", synthetic_jrc_workbook, tmp_path / "out")
