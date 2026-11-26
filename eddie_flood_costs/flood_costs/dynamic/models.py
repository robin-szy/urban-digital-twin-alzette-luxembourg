"""
The three dynamic models and the stage-damage curve.

Moved unchanged from users/devika/dynamic_flood_cost_3models (model1_peak_envelope.py,
model2_duration.py, model3_disruption.py, stage_curve.py); only the imports differ.

Model 1  Peak-envelope JRC     D_i(t) = V_c sum_j w_ij f_c(max_{tau<=t} h_ij(tau))
Model 2  Duration-dependent    D_i(t) = V_c sum_j w_ij min(1, f_c(h_max) [1 + beta_c (1 - e^(-d/tau_c))])
Model 3  Direct + disruption   C_i(t) = D_i(t) + sum dt 1[wet_i] (p_i c_disp + F_i c_bus) / 24

Unknown stays unknown: a building is NaN as soon as one weighted sample is unknown.
"""

import numpy as np
import pandas as pd

from . import core, parameters

# ----------------------------------------------------------------------- Model 1


def model1(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Model 1 damage in EUR 2010, buildings x timesteps (NaN = unknown)."""
    h_max = core.running_max_depth(depth)
    fraction = core.damage_fraction(h_max, samples["jrc_category"], jrc)
    return core.building_damage(fraction, samples, jrc)


def model1_current_depth(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Same formula with the CURRENT depth h(t): one level per column (stage curve)."""
    fraction = core.damage_fraction(depth.clip(lower=0.0), samples["jrc_category"], jrc)
    return core.building_damage(fraction, samples, jrc)


# ----------------------------------------------------------------------- Model 2


def duration_multiplier(hours_wet: pd.DataFrame, sample_class: pd.Series) -> pd.DataFrame:
    """1 + beta_c * (1 - exp(-d / tau_c)): starts at 1, rises towards 1 + beta_c."""
    beta = sample_class.map(parameters.DURATION_BETA)
    tau = sample_class.map(parameters.DURATION_TAU_HOURS)
    if beta.isna().any() or tau.isna().any():
        raise ValueError("DURATION_BETA / DURATION_TAU_HOURS is missing a building class")
    build_up = 1.0 - np.exp(-hours_wet.div(tau, axis=0))
    return 1.0 + build_up.mul(beta, axis=0)


def model2(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Model 2 damage in EUR 2010, buildings x timesteps (NaN = unknown)."""
    classes = samples["jrc_category"]
    depth_fraction = core.damage_fraction(core.running_max_depth(depth), classes, jrc)
    multiplier = duration_multiplier(core.hours_wet(depth), classes)
    fraction = (depth_fraction * multiplier).clip(upper=1.0)
    return core.building_damage(fraction, samples, jrc)


def model2_for_duration(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict, hours: float) -> pd.DataFrame:
    """Model 2 for levels held for `hours` (one level per column). hours = 0 is Model 1 at that level."""
    classes = samples["jrc_category"]
    depth_fraction = core.damage_fraction(depth.clip(lower=0.0), classes, jrc)
    hours_wet = core.is_wet(depth) * float(hours)          # 1/0 * hours; NaN stays NaN
    fraction = (depth_fraction * duration_multiplier(hours_wet, classes)).clip(upper=1.0)
    return core.building_damage(fraction, samples, jrc)


# ----------------------------------------------------------------------- Model 3


def disruption_rate_eur_per_hour(attributes: pd.DataFrame) -> pd.Series:
    """residents * c_disp / 24 + business floor area * c_bus / 24. NaN if an input is missing."""
    displacement = attributes["residents"] * parameters.COST_DISPLACEMENT_EUR_PER_PERSON_DAY / 24.0
    is_business = attributes["jrc_category"].isin(parameters.BUSINESS_CLASSES)
    business_area = attributes["floor_area_m2"].where(is_business, 0.0)
    business = business_area * parameters.COST_BUSINESS_EUR_PER_M2_DAY / 24.0
    return displacement + business


def disruption(depth: pd.DataFrame, samples: pd.DataFrame, attributes: pd.DataFrame) -> pd.DataFrame:
    """Accumulated disruption cost in EUR, buildings x timesteps (NaN after an undecidable step)."""
    wet = core.building_is_wet(depth, samples)
    dt = core.timestep_hours(depth.columns)
    rate = disruption_rate_eur_per_hour(attributes).reindex(wet.index)
    per_step = wet.mul(dt, axis=1).mul(rate, axis=0)
    known_so_far = per_step.notna().astype(int).cummin(axis=1).astype(bool)
    return per_step.fillna(0.0).cumsum(axis=1).where(known_so_far)


# ----------------------------------------------------------------------- stage curve


def stage_curve(depth_by_stage: pd.DataFrame, samples: pd.DataFrame, jrc: dict, durations_hours) -> dict:
    """depth_by_stage: samples x stages (cm). {'model1': ..., 'model2_<h>h': ...}, buildings x stages, EUR 2010."""
    results = {"model1": model1_current_depth(depth_by_stage, samples, jrc)}
    for hours in durations_hours:
        results[f"model2_{hours:g}h"] = model2_for_duration(depth_by_stage, samples, jrc, hours)
    return results


def curve_totals(results: dict) -> pd.DataFrame:
    """One row per stage: total EUR per model (known buildings only), and counts."""
    first = results["model1"]
    out = pd.DataFrame({f"{name}_eur2010": table.sum(min_count=1) for name, table in results.items()})
    out["damaged_buildings"] = (first > 0).sum()
    out["unknown_buildings"] = first.isna().sum()
    out["known_buildings"] = first.notna().sum()
    out.index.name = "stage_cm"
    return out


def per_building(results: dict, column: str) -> pd.DataFrame:
    """Long table: buildingid, <column>, one EUR column per result."""
    frames = [table.stack(future_stack=True).rename(f"{name}_eur2010") for name, table in results.items()]
    out = pd.concat(frames, axis=1).reset_index()
    out.columns = ["buildingid", column, *out.columns[2:]]
    return out


def equivalent_stages(table: pd.DataFrame, references: dict, column: str = "model1_eur2010") -> dict:
    """Stage (cm) at which the curve first reaches each reference total (linear between stages); None if never."""
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


# ----------------------------------------------------------------------- an event


def event(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict, attributes: pd.DataFrame | None) -> dict:
    """All three models over a period: {'model1', 'model2', 'disruption', 'model3'}, buildings x timesteps.

    Model 3 = the direct damage named by parameters.MODEL3_DIRECT + disruption. Without
    attributes the disruption (and so Model 3) is unknown for every building.
    """
    results = {"model1": model1(depth, samples, jrc), "model2": model2(depth, samples, jrc)}
    direct = results[parameters.MODEL3_DIRECT]
    if attributes is None:
        cost = pd.DataFrame(np.nan, index=direct.index, columns=direct.columns)
    else:
        cost = disruption(depth, samples, attributes).reindex(direct.index)
    results["disruption"] = cost
    results["model3"] = direct + cost
    return results
