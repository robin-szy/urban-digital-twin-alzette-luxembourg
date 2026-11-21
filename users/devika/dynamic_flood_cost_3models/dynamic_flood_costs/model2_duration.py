"""
Model 2 - Duration-dependent damage

    D_i(t) = V_c * sum_j  w_ij * min(1, f_c(h_max_ij(t)) * [1 + beta_c * (1 - exp(-d_ij(t) / tau_c))])

Water that stays does more damage. d_ij(t) is the hours sample j has been wet so far;
beta_c is the maximum extra damage and tau_c how fast it builds up (parameters.py,
placeholders). With beta = 0 this is exactly Model 1.
"""

import numpy as np
import pandas as pd

from . import core, parameters


def duration_multiplier(hours_wet: pd.DataFrame, sample_class: pd.Series) -> pd.DataFrame:
    """1 + beta_c * (1 - exp(-d / tau_c)): starts at 1, rises towards 1 + beta_c."""
    beta = sample_class.map(parameters.DURATION_BETA)
    tau = sample_class.map(parameters.DURATION_TAU_HOURS)
    if beta.isna().any() or tau.isna().any():
        raise ValueError("DURATION_BETA / DURATION_TAU_HOURS is missing a building class")
    build_up = 1.0 - np.exp(-hours_wet.div(tau, axis=0))
    return 1.0 + build_up.mul(beta, axis=0)


def compute(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Model 2 damage in EUR 2010, buildings x timesteps (NaN = unknown)."""
    classes = samples["jrc_category"]
    depth_fraction = core.damage_fraction(core.running_max_depth(depth), classes, jrc)
    multiplier = duration_multiplier(core.hours_wet(depth), classes)
    fraction = (depth_fraction * multiplier).clip(upper=1.0)
    return core.building_damage(fraction, samples, jrc)
