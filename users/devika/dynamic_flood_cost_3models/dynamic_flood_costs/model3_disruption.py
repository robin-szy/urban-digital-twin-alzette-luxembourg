"""
Model 3 - Direct damage + disruption

    C_i(t) = D_i(t) + sum over timesteps up to t of
             Delta t * 1[building i wet] * (p_i * c_disp + F_i * c_bus) / 24

For every hour a building is wet, residents are displaced and businesses are closed.
D_i(t) comes from Model 1 or 2. p_i (residents) and F_i (floor area, business classes
only) come from the building attributes input; c_disp and c_bus are placeholders
in parameters.py (per day, hence / 24).

Note: eddie_flood_costs deliberately excludes indirect losses. This model adds them
as an experimental, clearly separated quantity.
"""

import pandas as pd

from . import core, parameters


def disruption_rate_eur_per_hour(attributes: pd.DataFrame) -> pd.Series:
    """residents * c_disp / 24 + business floor area * c_bus / 24. NaN if an input is missing."""
    displacement = attributes["residents"] * parameters.COST_DISPLACEMENT_EUR_PER_PERSON_DAY / 24.0
    is_business = attributes["jrc_category"].isin(parameters.BUSINESS_CLASSES)
    business_area = attributes["floor_area_m2"].where(is_business, 0.0)
    business = business_area * parameters.COST_BUSINESS_EUR_PER_M2_DAY / 24.0
    return displacement + business


def compute_disruption(depth: pd.DataFrame, samples: pd.DataFrame, attributes: pd.DataFrame) -> pd.DataFrame:
    """Accumulated disruption cost in EUR, buildings x timesteps (NaN after an undecidable step)."""
    wet = core.building_is_wet(depth, samples)
    dt = core.timestep_hours(depth.columns)
    rate = disruption_rate_eur_per_hour(attributes).reindex(wet.index)
    per_step = wet.mul(dt, axis=1).mul(rate, axis=0)
    known_so_far = per_step.notna().astype(int).cummin(axis=1).astype(bool)
    return per_step.fillna(0.0).cumsum(axis=1).where(known_so_far)


def compute(direct: pd.DataFrame, depth: pd.DataFrame, samples: pd.DataFrame,
            attributes: pd.DataFrame) -> dict:
    """Return {'direct', 'disruption', 'total'} in EUR, buildings x timesteps."""
    disruption = compute_disruption(depth, samples, attributes).reindex(direct.index)
    return {"direct": direct, "disruption": disruption, "total": direct + disruption}
