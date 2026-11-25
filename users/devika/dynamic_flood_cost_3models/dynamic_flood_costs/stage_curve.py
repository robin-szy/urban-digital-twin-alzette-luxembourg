"""
Stage-damage curve: how the flood cost changes as the water level rises.

For each gauge stage (the front-end slider sets the same stage, in cm, at every gauge) REM
gives the depth at every sample point. From those depths:

  Model 1   JRC damage at that level (a single level has no history, so its depth is the peak)
  Model 2   the same level held for a chosen time: f(h) * [1 + beta (1 - exp(-hours / tau))],
            one curve per duration in parameters.CURVE_DURATIONS_HOURS

Unknown samples keep a building unknown (NaN); totals add up known buildings only.
"""

import numpy as np
import pandas as pd

from . import model1_peak_envelope, model2_duration


def curve(depth_by_stage: pd.DataFrame, samples: pd.DataFrame, jrc: dict, durations_hours) -> dict:
    """depth_by_stage: samples x stages (cm). Returns {'model1': ..., 'model2_<h>h': ...},
    each a buildings x stages table of EUR 2010."""
    results = {"model1": model1_peak_envelope.compute_current_depth_version(depth_by_stage, samples, jrc)}
    for hours in durations_hours:
        results[f"model2_{hours:g}h"] = model2_duration.compute_for_duration(depth_by_stage, samples, jrc, hours)
    return results


def totals(results: dict) -> pd.DataFrame:
    """One row per stage: total EUR per model (known buildings), and counts."""
    model1 = results["model1"]
    out = pd.DataFrame({f"{name}_eur2010": table.sum(min_count=1) for name, table in results.items()})
    out["damaged_buildings"] = (model1 > 0).sum()
    out["unknown_buildings"] = model1.isna().sum()
    out.index.name = "stage_cm"
    return out


def per_building(results: dict) -> pd.DataFrame:
    """Long table: buildingid, stage_cm, one EUR column per model."""
    frames = []
    for name, table in results.items():
        long = table.stack(future_stack=True).rename(f"{name}_eur2010")
        frames.append(long)
    out = pd.concat(frames, axis=1).reset_index()
    out.columns = ["buildingid", "stage_cm", *out.columns[2:]]
    return out


def equivalent_stages(table: pd.DataFrame, references: dict, column: str = "model1_eur2010") -> dict:
    """Stage (cm) at which the curve first reaches each reference total, by linear interpolation
    between computed stages. None if the curve never reaches it. Matched on cost, not water level."""
    stages = table.index.to_numpy(dtype=float)
    values = table[column].to_numpy(dtype=float)
    out = {}
    for name, total in references.items():
        above = np.flatnonzero(values >= total)
        if len(above) == 0:
            out[name] = None
        elif above[0] == 0:
            out[name] = float(stages[0])
        else:
            k = above[0]
            out[name] = float(np.interp(total, values[k - 1:k + 1], stages[k - 1:k + 1]))
    return out
