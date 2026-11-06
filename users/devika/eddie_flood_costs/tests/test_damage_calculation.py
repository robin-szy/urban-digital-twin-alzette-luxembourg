"""Analytic synthetic curve, area weighting, workbook and scenario checks."""
import numpy as np
import pandas as pd
import pytest
import shapely

from flood_costs import damage_calculation as damage
from flood_costs.cost_lookup import CostLookup, SCHEMA_VERSION, _totals


def test_jrc_knots_interpolation_and_above_curve_saturation(jrc_parameter):
    observed = damage.fraction([0, .25, .5, .75, 1, 2, 6, 7], jrc_parameter)
    np.testing.assert_allclose(observed, [0, .125, .25, .325, .4, .6, 1, 1], atol=1e-14)


@pytest.mark.parametrize("invalid", [-1., np.nan, np.inf])
def test_invalid_depth_is_rejected(invalid, jrc_parameter):
    with pytest.raises(AssertionError):
        damage.fraction([invalid], jrc_parameter)


def test_reads_luxembourg_structure_and_contents_from_synthetic_workbook(synthetic_jrc_workbook, monkeypatch):
    monkeypatch.setattr(damage, "WORKBOOK", synthetic_jrc_workbook, raising=False)
    result = damage.read_jrc()
    assert result["currency"] == "EUR" and result["price_reference_year"] == 2010
    assert result["inflation_adjustment"] is False
    for category, maximum in (("Residential", 150.), ("Commercial", 300.), ("Industrial", 450.)):
        parameters = result["parameters"][category]
        assert parameters["maximum_damage_eur2010_per_m2"] == maximum
        assert parameters["structure_eur2010_per_m2"] + parameters["contents_eur2010_per_m2"] == maximum
        assert parameters["maximum_cell"] == "D117"
        assert "B117+C117" in parameters["cached_total_cell"]["formula_xml"]


def test_partial_flooding_keeps_dry_area_and_applies_curve_before_average(jrc_parameter):
    estimate = damage.weighted_estimate(np.array([50., 50.]), np.array([True, True]),
                                       np.array([0., 2.]), 0., 100., 100., jrc_parameter)
    # Half of 100 m² is dry; half has fraction .6. f(mean depth=1) would wrongly give .4.
    assert estimate["complete"]
    assert estimate["full_fraction"] == pytest.approx(.3)
    assert estimate["monetary"] == pytest.approx(3000.)
    assert estimate["full_fraction"] != pytest.approx(float(damage.fraction([1.], jrc_parameter)[0]))


def test_known_dry_damage_is_zero_and_missing_area_is_null(jrc_parameter):
    dry = damage.weighted_estimate(np.array([75., 25.]), np.array([True, True]),
                                  np.array([0., 0.]), 0., 100., 100., jrc_parameter)
    partial = damage.weighted_estimate(np.array([75., 25.]), np.array([True, False]),
                                      np.array([1., np.nan]), 0., 100., 100., jrc_parameter)
    assert dry["complete"] and dry["monetary"] == 0.0
    assert partial["monetary"] is None and partial["full_fraction"] is None
    assert partial["missing_area"] == 25.
    assert partial["known_damage"] == pytest.approx(3000.)


def test_unsupported_footprint_part_does_not_inflate_known_damage(jrc_parameter):
    result = damage.weighted_estimate(np.array([50.]), np.array([True]), np.array([1.]),
                                      50., 100., 100., jrc_parameter)
    assert result["monetary"] is None and result["missing_area"] == 50.
    assert result["known_damage"] == pytest.approx(2000.)


def test_voronoi_weights_conserve_area_and_exclude_boundary_observations():
    footprint = shapely.box(0, 0, 2, 1)
    samples = pd.DataFrame({"x_epsg2169": [0., .5, 1.5, 2.], "y_epsg2169": [.5] * 4, "part_index": [0] * 4})
    weights, interior, unsupported, area, error = damage.build_weights(footprint, footprint, samples, 2.)
    np.testing.assert_allclose(weights, [0., 1., 1., 0.], atol=1e-12)
    assert interior.tolist() == [False, True, True, False]
    assert unsupported == 0. and area == 2. and error < 1e-12
    # A shared sliver already assigned to another building is removed once.
    clipped, _, _, area, _ = damage.build_weights(footprint, shapely.box(0, 0, 1, 1), samples, 2.)
    assert clipped.sum() == pytest.approx(1.) and area == pytest.approx(1.)


def test_multipart_footprint_without_interior_support_remains_unknown(jrc_parameter):
    footprint = shapely.MultiPolygon([shapely.box(0, 0, 1, 1), shapely.box(2, 0, 3, 1)])
    samples = pd.DataFrame({"x_epsg2169": [.5], "y_epsg2169": [.5], "part_index": [0]})
    weights, _, unsupported, area, _ = damage.build_weights(footprint, footprint, samples, 2.)
    assert unsupported == pytest.approx(1.)
    result = damage.weighted_estimate(weights, np.array([True]), np.array([1.]), unsupported, area, 2., jrc_parameter)
    assert result["monetary"] is None


def test_both_scenarios_aggregate_only_complete_building_estimates(jrc_parameter):
    def estimate(depths, valid=(True, True)):
        return damage.weighted_estimate(np.array([25., 75.]), np.array(valid), np.array(depths),
                                        0., 100., 100., jrc_parameter)["monetary"]

    observations = {
        "ACT_DRY": (estimate([0., 0.]), estimate([1., 1.])),
        "ACT_PARTIAL": (estimate([0., 1.]), estimate([1., 2.])),
        "ACT_UNKNOWN": (estimate([1., np.nan], [True, False]), estimate([2., np.nan], [True, False])),
    }
    buildings = {}
    for bid, amounts in observations.items():
        record = {"jrc_category": "Residential", "accepted_provisional_classification": True,
                  "classification_scope": "classified_subset", "classification_confidence": None,
                  "classification_source": "synthetic_fixture", "classification_basis": None,
                  "classification_review_decision": None}
        for scenario, amount in zip(("hq10", "hq100"), amounts):
            record[scenario] = {"damage_eur2010": amount, "quality_flags": [],
                                "status": "unknown_depth_coverage" if amount is None else
                                "calculated_zero" if amount == 0 else "calculated_positive"}
        buildings[bid] = record
    lookup = CostLookup({"schema_version": SCHEMA_VERSION, "dataset_version": "synthetic-calculated",
                         "metadata": {"currency": "EUR", "price_year": 2010, "provisional": True},
                         "buildings": buildings, "summaries": _totals(buildings)})
    for scenario, expected in (("hq10", 3000.), ("hq100", 9500.)):
        summary = lookup.summary(scenario)
        assert summary["total_eur2010"] == pytest.approx(expected)
        assert summary["calculated_damage_buildings"] == 2 and summary["unknown_damage_buildings"] == 1
        assert summary["complete_for_classified_subset"] is False
        assert lookup.damage("ACT_UNKNOWN", scenario) is None
    assert lookup.damage("ACT_DRY", "hq10") == 0.0
