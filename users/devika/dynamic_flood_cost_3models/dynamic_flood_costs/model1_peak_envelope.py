"""
Model 1 - Peak-envelope JRC

    D_i(t) = V_c * sum_j  w_ij * f_c( max_{tau <= t} h_ij(tau) )

Flood damage is irreversible: each sample is charged for the deepest water it has
seen so far, not the current depth, so D_i(t) never decreases.
"""

import pandas as pd

from . import core


def compute(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Model 1 damage in EUR 2010, buildings x timesteps (NaN = unknown)."""
    h_max = core.running_max_depth(depth)
    fraction = core.damage_fraction(h_max, samples["jrc_category"], jrc)
    return core.building_damage(fraction, samples, jrc)


def compute_current_depth_version(depth: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """Same formula with the CURRENT depth h(t). For comparison only: exposure, not damage."""
    fraction = core.damage_fraction(depth.clip(lower=0.0), samples["jrc_category"], jrc)
    return core.building_damage(fraction, samples, jrc)
