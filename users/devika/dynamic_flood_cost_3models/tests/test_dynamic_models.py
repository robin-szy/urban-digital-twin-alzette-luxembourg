"""Synthetic checks of Models 1-3. No datasets, services or EDDIE needed."""
import gzip
import math

import numpy as np
import pandas as pd
import pytest

from flood_costs import damage_calculation as damage

from dynamic_flood_costs import (core, inputs, model1_peak_envelope, model2_duration,
                                 model3_disruption, outputs, parameters)

# Synthetic curve, as in eddie_flood_costs/tests/conftest.py: not official coefficients.
CURVE = {"depth_m": [0, .5, 1, 1.5, 2, 3, 4, 5, 6],
         "damage_fraction": [0, .25, .4, .5, .6, .75, .85, .95, 1],
         "maximum_damage_eur2010_per_m2": 100.0}
JRC = {c: dict(CURVE) for c in ("Residential", "Commercial", "Industrial")}


def make(depths: dict, weights: dict, category="Residential", hours=None):
    """samples and wide depth table from {(building, sample): [depth per timestep]}."""
    index = pd.MultiIndex.from_tuples(list(depths), names=core.SAMPLE_INDEX)
    n = len(next(iter(depths.values())))
    times = pd.date_range("2021-07-14", periods=n, freq="h") if hours is None else \
        pd.to_datetime("2021-07-14") + pd.to_timedelta(hours, unit="h")
    depth = pd.DataFrame(list(depths.values()), index=index, columns=times, dtype=float)
    samples = pd.DataFrame({"weight_m2": [weights[k] for k in depths],
                            "jrc_category": category}, index=index)
    return samples, depth


def test_model1_matches_existing_weighted_estimate_at_one_timestep():
    samples, depth = make({("B1", 0): [1.0], ("B1", 1): [0.0]}, {("B1", 0): 25.0, ("B1", 1): 75.0})
    ours = model1_peak_envelope.compute(depth, samples, JRC).iloc[0, 0]
    theirs = damage.weighted_estimate(np.array([25., 75.]), np.array([True, True]), np.array([1., 0.]),
                                      0., 100., 100., CURVE)["monetary"]
    assert ours == pytest.approx(theirs)


def test_model1_holds_the_peak_and_current_depth_version_falls():
    samples, depth = make({("B1", 0): [0.2, 1.0, 0.3, 0.0]}, {("B1", 0): 50.0})
    m1 = model1_peak_envelope.compute(depth, samples, JRC).loc["B1"].to_numpy()
    now = model1_peak_envelope.compute_current_depth_version(depth, samples, JRC).loc["B1"].to_numpy()
    assert np.all(np.diff(m1) >= 0)
    assert m1[-1] == pytest.approx(50 * 100 * 0.4)
    assert now[-1] == 0.0


def test_hours_wet_pauses_and_does_not_reset():
    values = [0.0, .10, .40, .60, .30, .03, 0.0, .20, .15]
    samples, depth = make({("B1", 0): values}, {("B1", 0): 1.0})
    assert core.hours_wet(depth).iloc[0].tolist() == [0, 1, 2, 3, 4, 4, 4, 5, 6]


def test_model2_with_beta_zero_equals_model1(monkeypatch):
    monkeypatch.setattr(parameters, "DURATION_BETA", {c: 0.0 for c in JRC})
    samples, depth = make({("B1", 0): [0, .5, 1.0, 1.0, .2]}, {("B1", 0): 30.0})
    pd.testing.assert_frame_equal(model2_duration.compute(depth, samples, JRC),
                                  model1_peak_envelope.compute(depth, samples, JRC))


def test_model2_hand_example(monkeypatch):
    monkeypatch.setattr(parameters, "DURATION_BETA", {c: 0.3 for c in JRC})
    monkeypatch.setattr(parameters, "DURATION_TAU_HOURS", {c: 24.0 for c in JRC})
    # Wet at 1.0 m for 24 one-hour steps (the first step counts 1 h too).
    samples, depth = make({("B1", 0): [1.0] * 24}, {("B1", 0): 50.0})
    expected = 50 * 100 * 0.4 * (1 + 0.3 * (1 - math.exp(-1)))
    assert model2_duration.compute(depth, samples, JRC).iloc[0, -1] == pytest.approx(expected)


def test_model2_never_exceeds_full_damage(monkeypatch):
    monkeypatch.setattr(parameters, "DURATION_BETA", {c: 5.0 for c in JRC})
    samples, depth = make({("B1", 0): [6.0] * 100}, {("B1", 0): 10.0})
    assert model2_duration.compute(depth, samples, JRC).iloc[0, -1] == pytest.approx(10 * 100)


def test_missing_depth_makes_building_unknown_not_zero():
    samples, depth = make({("B1", 0): [0.5, np.nan, 0.5], ("B1", 1): [0.0, 0.0, 0.0],
                           ("B2", 0): [0.5, 0.5, 0.5]},
                          {("B1", 0): 10.0, ("B1", 1): 10.0, ("B2", 0): 10.0})
    m1 = model1_peak_envelope.compute(depth, samples, JRC)
    assert m1.loc["B1"].isna().tolist() == [False, True, True]   # unknown from the gap on
    assert m1.loc["B2"].notna().all()
    total = outputs.totals({"m1": m1})
    assert total["unknown_buildings"].tolist() == [0, 1, 1]
    assert total["m1"].iloc[-1] == pytest.approx(m1.loc["B2"].iloc[-1])   # B1 not imputed as 0


def test_zero_weight_samples_do_not_block_an_estimate():
    samples, depth = make({("B1", 0): [1.0], ("B1", 1): [np.nan]}, {("B1", 0): 10.0, ("B1", 1): 0.0})
    assert model1_peak_envelope.compute(depth, samples, JRC).iloc[0, 0] == pytest.approx(10 * 100 * .4)


def test_disruption_matches_hand_check():
    # 4 residents, EUR 80/person/day, wet for 50 h (first step counts 1 h)
    samples, depth = make({("B1", 0): [0.0] + [0.5] * 50 + [0.0] * 5}, {("B1", 0): 10.0})
    attributes = pd.DataFrame({"residents": [4], "floor_area_m2": [180.0], "jrc_category": ["Residential"]},
                              index=pd.Index(["B1"], name="buildingid"))
    disruption = model3_disruption.compute_disruption(depth, samples, attributes)
    assert disruption.iloc[0, -1] == pytest.approx(4 * 80 * 50 / 24)


def test_business_cost_only_for_business_classes():
    attributes = pd.DataFrame({"residents": [0, 0], "floor_area_m2": [500.0, 500.0],
                               "jrc_category": ["Residential", "Commercial"]}, index=["H", "S"])
    rate = model3_disruption.disruption_rate_eur_per_hour(attributes)
    assert rate["H"] == 0 and rate["S"] == pytest.approx(500 * 20 / 24)


def test_unknown_residents_leave_disruption_unknown():
    samples, depth = make({("B1", 0): [0.5, 0.5]}, {("B1", 0): 10.0})
    attributes = pd.DataFrame({"residents": [np.nan], "floor_area_m2": [np.nan], "jrc_category": ["Residential"]},
                              index=pd.Index(["B1"], name="buildingid"))
    assert model3_disruption.compute_disruption(depth, samples, attributes).isna().all().all()


def test_load_samples_deduplicates_scenarios_and_keeps_accepted_classes(tmp_path):
    audit = pd.DataFrame({
        "buildingid": ["A", "A", "A", "A", "X"], "sample_index": [0, 1, 0, 1, 0],
        "x_epsg2169": [1., 2., 1., 2., 5.], "y_epsg2169": [1., 1., 1., 1., 5.],
        "weight_m2": [3., 4., 3., 4., 9.], "scenario": ["hq10", "hq10", "hq100", "hq100", "hq10"]})
    with gzip.open(tmp_path / "w.csv.gz", "wt") as handle:
        audit.to_csv(handle, index=False)
    pd.DataFrame({"buildingid": ["A", "X"], "jrc_category": ["Commercial", None],
                  "accepted_provisional_classification": [True, False]}).to_csv(tmp_path / "b.csv", index=False)
    samples = inputs.load_samples(tmp_path / "w.csv.gz", tmp_path / "b.csv")
    assert list(samples.index) == [("A", 0), ("A", 1)]
    assert samples["jrc_category"].unique().tolist() == ["Commercial"]
