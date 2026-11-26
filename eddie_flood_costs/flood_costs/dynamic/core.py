"""
Steps shared by the three models (moved from users/devika/dynamic_flood_cost_3models, unchanged).

Data layout
-----------
Depths over time are a "wide" table: one row per sample (index: buildingid, sample_index),
one column per timestep (sorted timestamps). Every per-sample quantity (maximum depth,
hours wet, damage fraction) uses the same layout.

Unknown values stay unknown
---------------------------
As in eddie_flood_costs, a missing depth is never treated as dry. NaN means unknown:
  - once a sample's depth has been unknown, its running maximum and hours wet are unknown
    from then on (we cannot know what happened during the gap);
  - a building's damage at a timestep is reported only when every sample with a positive
    Voronoi weight is known; otherwise it is NaN, written as an empty value / JSON null.
"""

import numpy as np
import pandas as pd

from ..damage_calculation import fraction as jrc_fraction

from . import parameters

SAMPLE_INDEX = ["buildingid", "sample_index"]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def timestep_hours(timestamps) -> pd.Series:
    """Length of each timestep (Delta t) in hours. The first step gets the length of the second."""
    t = pd.DatetimeIndex(timestamps)
    dt = np.diff(t.values).astype("timedelta64[s]").astype(float) / 3600.0
    first = dt[0] if len(dt) else 0.0
    return pd.Series(np.concatenate([[first], dt]), index=timestamps)


def _known_so_far(depth: pd.DataFrame) -> pd.DataFrame:
    """True while every depth of the sample up to and including t is known."""
    return depth.notna().astype(int).cummin(axis=1).astype(bool)


# ---------------------------------------------------------------------------
# Water quantities per sample
# ---------------------------------------------------------------------------

def running_max_depth(depth: pd.DataFrame) -> pd.DataFrame:
    """h_max(t): deepest the water has been at each sample up to t (NaN after an unknown depth)."""
    return depth.clip(lower=0.0).cummax(axis=1).where(_known_so_far(depth))


def is_wet(depth: pd.DataFrame, threshold_m: float | None = None) -> pd.DataFrame:
    """1.0 where depth > threshold (default WET_DEPTH_THRESHOLD_M), 0.0 where known and not, NaN where unknown."""
    threshold_m = parameters.WET_DEPTH_THRESHOLD_M if threshold_m is None else threshold_m
    wet = (depth > threshold_m).astype(float)
    return wet.where(depth.notna())


def hours_wet(depth: pd.DataFrame) -> pd.DataFrame:
    """d(t) = sum of Delta t over the wet timesteps up to t. Pauses when dry, never resets."""
    dt = timestep_hours(depth.columns)
    d = is_wet(depth).fillna(0.0).mul(dt, axis=1).cumsum(axis=1)
    return d.where(_known_so_far(depth))


# ---------------------------------------------------------------------------
# Depth-damage function, using the team's JRC implementation
# ---------------------------------------------------------------------------

def damage_fraction(depth: pd.DataFrame, sample_class: pd.Series, jrc: dict) -> pd.DataFrame:
    """Apply flood_costs.damage_calculation.fraction per class, cell by cell. NaN stays NaN.

    jrc is read_jrc()['parameters']: {class: {'depth_m', 'damage_fraction',
    'maximum_damage_eur2010_per_m2', ...}}.
    """
    out = pd.DataFrame(np.nan, index=depth.index, columns=depth.columns)
    for building_class in sample_class.unique():
        rows = sample_class.index[sample_class == building_class]
        values = depth.loc[rows].to_numpy(dtype=float)
        known = np.isfinite(values)
        result = np.full(values.shape, np.nan)
        result[known] = jrc_fraction(np.maximum(values[known], 0.0), jrc[building_class])
        out.loc[rows] = result
    return out


# ---------------------------------------------------------------------------
# From fractions to euros per building
# ---------------------------------------------------------------------------

def building_damage(fraction: pd.DataFrame, samples: pd.DataFrame, jrc: dict) -> pd.DataFrame:
    """D_i(t) = V_c * sum_j w_ij * fraction_ij(t), or NaN if any weighted sample is unknown.

    samples: indexed like fraction, columns weight_m2 and jrc_category.
    """
    weights = samples["weight_m2"]
    v = samples["jrc_category"].map(lambda c: jrc[c]["maximum_damage_eur2010_per_m2"])
    eur = fraction.mul(weights * v, axis=0)

    building = samples.index.get_level_values("buildingid")
    weighted = (weights > 0).to_numpy()
    total = eur[weighted].groupby(building[weighted]).sum(min_count=1)
    unknown = fraction[weighted].isna().groupby(building[weighted]).any()
    total = total.where(~unknown)
    total.index.name = "buildingid"
    return total


def building_is_wet(depth: pd.DataFrame, samples: pd.DataFrame) -> pd.DataFrame:
    """1.0 / 0.0 per building and timestep; NaN where the rule cannot be decided."""
    wet = is_wet(depth, parameters.DISRUPTION_DEPTH_M)   # flooded = deeper than the floor proxy
    building = samples.index.get_level_values("buildingid")
    weighted = (samples["weight_m2"] > 0).to_numpy()
    wet, building = wet[weighted], building[weighted]
    weights = samples["weight_m2"][weighted]

    if parameters.WET_RULE == "any_sample":
        any_wet = (wet == 1.0).groupby(building).any()
        all_known = wet.notna().groupby(building).all()
        # Wet as soon as one known sample is wet; dry only if every sample is known and dry.
        return any_wet.astype(float).where(any_wet | all_known)

    if parameters.WET_RULE == "area_share":
        wet_area = wet.mul(weights, axis=0).groupby(building).sum(min_count=1)
        all_known = wet.notna().groupby(building).all()
        total_area = weights.groupby(building).sum()
        share = wet_area.div(total_area, axis=0)
        return (share >= parameters.WET_AREA_SHARE).astype(float).where(all_known)

    raise ValueError(f"Unknown WET_RULE: {parameters.WET_RULE}")


def to_long(wide: pd.DataFrame, value_name: str) -> pd.DataFrame:
    """Wide (building x time) -> long, one row per building and timestep, NaN kept."""
    long = wide.stack(future_stack=True).rename(value_name).reset_index()
    long.columns = ["buildingid", "timestamp", value_name]
    return long
