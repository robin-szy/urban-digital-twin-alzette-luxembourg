"""Synthetic checks of the beta/tau choice, fixed-duration Model 2 and the stage-damage curve."""
import gzip
import json
import math
import runpy
import sys

import numpy as np
import pandas as pd
import pytest

from dynamic_flood_costs import inputs, model1_peak_envelope, model2_duration, parameters, stage_curve

from test_dynamic_models import CURVE, JRC, make


def test_team_assumptions_beta_and_tau():
    assert set(parameters.DURATION_BETA.values()) == {0.3}
    assert set(parameters.DURATION_TAU_HOURS.values()) == {24.0}
    assert parameters.CURVE_STAGES_CM[0] == 0 and parameters.CURVE_STAGES_CM[-1] == 600


def test_fixed_duration_zero_hours_is_model1_at_that_level():
    samples, depth = make({("B1", 0): [0.0, 0.5, 1.0], ("B1", 1): [0.0, 0.0, 0.3]},
                          {("B1", 0): 20.0, ("B1", 1): 30.0})
    pd.testing.assert_frame_equal(model2_duration.compute_for_duration(depth, samples, JRC, 0.0),
                                  model1_peak_envelope.compute_current_depth_version(depth, samples, JRC))


def test_fixed_duration_uses_team_beta_and_tau():
    samples, depth = make({("B1", 0): [1.0]}, {("B1", 0): 50.0})
    for hours in (24.0, 72.0):
        expected = 50 * 100 * 0.4 * (1 + 0.3 * (1 - math.exp(-hours / 24)))
        assert model2_duration.compute_for_duration(depth, samples, JRC, hours).iloc[0, 0] == pytest.approx(expected)


def test_dry_samples_get_no_duration_effect():
    samples, depth = make({("B1", 0): [0.03]}, {("B1", 0): 10.0})   # below the wet threshold
    assert model2_duration.compute_for_duration(depth, samples, JRC, 72).iloc[0, 0] == pytest.approx(
        10 * 100 * float(np.interp(0.03, CURVE["depth_m"], CURVE["damage_fraction"])))


def _stage_depths():
    """Depth rises with the stage; building B2 stands higher, B3 has an unknown sample at 300 cm."""
    stages = [0, 100, 200, 300]
    rows = {("B1", 0): [0, .4, 1.2, 2.0], ("B1", 1): [0, .1, .8, 1.5],
            ("B2", 0): [0, 0, .2, .9],
            ("B3", 0): [0, .3, .6, np.nan]}
    samples, _ = make(rows, {k: 10.0 for k in rows})
    depth = pd.DataFrame(list(rows.values()), index=samples.index, columns=stages, dtype=float)
    return samples, depth


def test_stage_curve_rises_with_stage_and_model2_stays_within_beta():
    samples, depth = _stage_depths()
    results = stage_curve.curve(depth, samples, JRC, [6, 24, 72])
    table = stage_curve.totals(results)
    m1 = table["model1_eur2010"].to_numpy()
    assert m1[0] == 0 and np.all(np.diff(m1[:3]) > 0)
    for col in ("model2_6h_eur2010", "model2_24h_eur2010", "model2_72h_eur2010"):
        assert np.all(table[col] >= table["model1_eur2010"] - 1e-9)
        assert np.all(table[col] <= 1.3 * table["model1_eur2010"] + 1e-9)
    assert np.all(table["model2_6h_eur2010"] <= table["model2_72h_eur2010"])


def test_stage_curve_keeps_unknown_buildings_out_of_totals():
    samples, depth = _stage_depths()
    results = stage_curve.curve(depth, samples, JRC, [24])
    table = stage_curve.totals(results)
    assert table.loc[300, "unknown_buildings"] == 1
    known = results["model1"][300].drop("B3").sum()
    assert table.loc[300, "model1_eur2010"] == pytest.approx(known)
    long = stage_curve.per_building(results)
    assert long.loc[(long.buildingid == "B3") & (long.stage_cm == 300), "model1_eur2010"].isna().all()


def test_load_jrc_reads_the_run_parameters_json(tmp_path):
    (tmp_path / "jrc_parameters.json").write_text(json.dumps({"parameters": JRC}))
    assert inputs.load_jrc(tmp_path) == JRC


def test_stage_script_runs_offline_from_saved_depths(tmp_path, monkeypatch):
    samples, depth = _stage_depths()
    audit = samples.reset_index().assign(x_epsg2169=0.0, y_epsg2169=0.0, scenario="hq100")
    with gzip.open(tmp_path / "sample_damage_weights.csv.gz", "wt") as handle:
        audit.to_csv(handle, index=False)
    pd.DataFrame({"buildingid": ["B1", "B2", "B3"], "jrc_category": "Residential",
                  "accepted_provisional_classification": True}).to_csv(tmp_path / "building_flood_damage.csv", index=False)
    (tmp_path / "jrc_parameters.json").write_text(json.dumps({"parameters": JRC}))
    depth.stack().rename("depth_m").reset_index().rename(columns={"level_2": "stage_cm"}).to_csv(
        tmp_path / "depths.csv", index=False)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["x", "--monetary-dir", str(tmp_path), "--depths", str(tmp_path / "depths.csv"),
                                      "--stages", "0,100,200,300", "--durations", "24", "--out", str(out)])
    runpy.run_path("scripts/stage_damage_curve.py", run_name="__main__")
    table = pd.read_csv(out / "stage_damage_totals.csv", index_col="stage_cm")
    assert list(table.index) == [0, 100, 200, 300]
    assert (out / "stage_damage_buildings.csv.gz").is_file()


def test_equivalent_stage_interpolates_between_computed_stages():
    table = pd.DataFrame({"model1_eur2010": [0.0, 100.0, 300.0]}, index=pd.Index([0, 100, 200], name="stage_cm"))
    stages = stage_curve.equivalent_stages(table, {"A": 200.0, "B": 0.0, "C": 999.0})
    assert stages == {"A": 150.0, "B": 0.0, "C": None}


def test_scenario_totals_read_from_damage_summary(tmp_path):
    pd.DataFrame({"scenario": ["HQ10", "HQ10", "HQ100", "HQEXTREME"],
                  "group": ["Classified total", "Residential", "Classified total", "Classified total"],
                  "known_estimates_sum_eur2010": [100.0, 60.0, 1000.0, None]}).to_csv(tmp_path / "damage_summary.csv", index=False)
    assert inputs.load_scenario_totals(tmp_path) == {"HQ10": 100.0, "HQ100": 1000.0}
    assert inputs.load_scenario_totals(tmp_path / "missing") == {}
