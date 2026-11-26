"""
The two dynamic computations, independent of where the depths come from.

  curve(...)   cost per slider stage: Model 1 at that level, Model 2 for levels held 6/24/72 h
  event(...)   Models 1, 2 and 3 over a period of stored gauge readings

`depths` is any object with at_stage(cm), at_key(level key text) and keyframes(start, end)
returning arrays aligned with `samples` (dynamic.depths.BackendDepths in EDDIE, a fake in
the tests).
"""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import core, models, parameters


def assumptions() -> dict:
    """The model assumptions a result was computed with (stored with every result)."""
    return {"duration_beta": parameters.DURATION_BETA, "duration_tau_hours": parameters.DURATION_TAU_HOURS,
            "wet_depth_threshold_m": parameters.WET_DEPTH_THRESHOLD_M, "wet_rule": parameters.WET_RULE,
            "flooded_building_rule": {"rule": parameters.WET_RULE, "area_share": parameters.WET_AREA_SHARE,
                                      "depth_m": parameters.DISRUPTION_DEPTH_M},
            "model3_direct": parameters.MODEL3_DIRECT,
            "placeholders": {"displacement_eur_per_person_day": parameters.COST_DISPLACEMENT_EUR_PER_PERSON_DAY,
                             "business_eur_per_m2_day": parameters.COST_BUSINESS_EUR_PER_M2_DAY,
                             "floor_height_m": parameters.FLOOR_HEIGHT_M}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------- stage curve


def curve(samples: pd.DataFrame, jrc: dict, depths, stages_cm=parameters.CURVE_STAGES_CM,
          durations_hours=parameters.CURVE_DURATIONS_HOURS) -> tuple:
    """(totals per stage, per building and stage, meta)."""
    columns = {float(stage): depths.at_stage(stage) for stage in stages_cm}
    depth_by_stage = pd.DataFrame(columns, index=samples.index)
    depth_by_stage.columns.name = "stage_cm"
    results = models.stage_curve(depth_by_stage, samples, jrc, durations_hours)
    totals = models.curve_totals(results)
    buildings = models.per_building(results, "stage_cm")
    meta = {"computed_utc": _now(), "durations_hours": ",".join(f"{h:g}" for h in durations_hours),
            "stages": len(columns), "buildings": int(results["model1"].shape[0]),
            "assumptions": json.dumps(assumptions())}
    return totals, buildings, meta


# ----------------------------------------------------------------------- an event


def parse_period(start_iso: str, end_iso: str) -> tuple:
    """Validated (start, end) as UTC timestamps; at most parameters.MAX_EVENT_DAYS long."""
    start, end = pd.Timestamp(start_iso), pd.Timestamp(end_iso)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise ValueError("The end must be after the start.")
    if end - start > pd.Timedelta(days=parameters.MAX_EVENT_DAYS):
        raise ValueError(f"A period may cover at most {parameters.MAX_EVENT_DAYS} days.")
    return start, end


def event(samples: pd.DataFrame, jrc: dict, attributes, depths, start_iso: str, end_iso: str) -> tuple:
    """(meta, totals per timestep, per building and timestep) for a period of gauge readings."""
    start, end = parse_period(start_iso, end_iso)
    frames = depths.keyframes(start.isoformat(), end.isoformat())
    times = pd.to_datetime(frames["times"], utc=True)
    keys = list(frames["keys"])
    if len(times) == 0:
        raise ValueError("No gauge readings are stored for that period.")
    order = pd.Series(range(len(times)), index=times)
    order = order[~order.index.duplicated(keep="first")].sort_index()

    by_key = {}
    for key in dict.fromkeys(keys):                   # each distinct level once
        by_key[key] = np.asarray(depths.at_key(key), dtype="float32")
    columns = pd.DatetimeIndex(order.index)
    key_of_column = [keys[i] for i in order.to_numpy()]

    # Buildings are independent, so they are computed in chunks: a week of 15-minute readings
    # for every sample at once would not fit in memory.
    parts = {name: [] for name in ("model1", "model2", "disruption", "model3", "wet")}
    for rows in _chunks(samples, CHUNK_SAMPLES):
        chunk = samples.iloc[rows]
        values = np.column_stack([by_key[k][rows] for k in key_of_column]).astype(float)
        depth = pd.DataFrame(values, index=chunk.index, columns=columns)
        chunk_attributes = None if attributes is None else attributes.reindex(
            chunk.index.get_level_values("buildingid").unique())
        results = models.event(depth, chunk, jrc, chunk_attributes)
        for name, table in results.items():
            parts[name].append(table)
        parts["wet"].append(core.building_is_wet(depth, chunk))
    results = {name: pd.concat(tables) for name, tables in parts.items()}
    wet = results.pop("wet")
    totals = pd.DataFrame({
        "timestamp": columns,
        "model1_eur2010": results["model1"].sum(min_count=1).to_numpy(),
        "model2_eur2010": results["model2"].sum(min_count=1).to_numpy(),
        "disruption_eur": results["disruption"].sum(min_count=1).to_numpy(),
        "model3_total_eur": results["model3"].sum(min_count=1).to_numpy(),
        "wet_buildings": (wet == 1.0).sum().to_numpy(),
        "unknown_buildings": results["model1"].isna().sum().to_numpy(),
        "unknown_disruption_buildings": results["disruption"].isna().sum().to_numpy(),
    })
    buildings = models.per_building(results, "timestamp").rename(columns={
        "disruption_eur2010": "disruption_eur", "model3_eur2010": "model3_total_eur"})
    last = totals.iloc[-1]
    meta = {"start_utc": start.isoformat(), "end_utc": end.isoformat(), "computed_utc": _now(),
            "keyframes": len(columns), "distinct_levels": len(by_key),
            "buildings": int(results["model1"].shape[0]),
            "model1_final_eur2010": _number(last["model1_eur2010"]),
            "model2_final_eur2010": _number(last["model2_eur2010"]),
            "disruption_final_eur": _number(last["disruption_eur"]),
            "model3_final_eur": _number(last["model3_total_eur"]),
            "attributes_available": attributes is not None,
            "assumptions": assumptions()}
    return meta, totals, buildings


# Samples per chunk of an event (whole buildings; about 5000 x a week of readings in memory).
CHUNK_SAMPLES = 5000


def _chunks(samples: pd.DataFrame, size: int):
    """Row positions of consecutive whole buildings, about `size` samples each."""
    buildings = samples.index.get_level_values("buildingid").to_numpy()
    start = 0
    while start < len(buildings):
        stop = min(start + size, len(buildings))
        while stop < len(buildings) and buildings[stop] == buildings[stop - 1]:
            stop += 1                                  # never split a building
        yield np.arange(start, stop)
        start = stop


def _number(value):
    return None if value is None or not np.isfinite(value) else float(value)
