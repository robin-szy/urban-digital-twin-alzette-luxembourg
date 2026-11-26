"""
Database side of the dynamic models: their inputs and results, all in PostGIS.

Inputs (written by publish_inputs when a static run is published, see tasks.py):
  flood_cost_sample_weights   sample points and Voronoi weights of the published run
  flood_cost_jrc_parameters   the JRC curves and maximum damages that run used
read together with the static tables:
  flood_cost_lookup           which run is published (run_id, dataset_version)
  flood_cost_buildings        the run's accepted JRC category per building
  building_population         residents, footprint, floors (Model 3; Pegah's module, read only)

Results (written by the dynamic tasks, read by the API):
  flood_cost_dynamic_curve            cost per slider stage (totals)
  flood_cost_dynamic_curve_buildings  cost per building and stage
  flood_cost_dynamic_events           one row per computed period of gauge readings
  flood_cost_dynamic_event_totals     totals per timestep of an event
  flood_cost_dynamic_event_buildings  cost per building and timestep of an event

Every result row carries the run_id and dataset_version it was computed from, so a result
of an older static run is reported as stale, never shown as current.
Plain SQL only (no PostGIS functions): the same code runs on SQLite in the tests.
"""
import gzip
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import sqlalchemy as sa

from ..damage_calculation import CATEGORIES
from . import core, parameters

SAMPLES = "flood_cost_sample_weights"
JRC = "flood_cost_jrc_parameters"
CURVE = "flood_cost_dynamic_curve"
CURVE_BUILDINGS = "flood_cost_dynamic_curve_buildings"
EVENTS = "flood_cost_dynamic_events"
EVENT_TOTALS = "flood_cost_dynamic_event_totals"
EVENT_BUILDINGS = "flood_cost_dynamic_event_buildings"
LOOKUP = "flood_cost_lookup"
BUILDINGS = "flood_cost_buildings"
POPULATION = "building_population"
ACT_ID = re.compile(r"ACT_[A-Za-z0-9-]{6,}")


class DynamicUnavailable(RuntimeError):
    """A required input or result is not in the database (yet)."""


def _has_table(conn, table) -> bool:
    return sa.inspect(conn).has_table(table)


def _true(value) -> bool:
    return value is True or value == 1 or str(value).strip().lower() == "true"


# ----------------------------------------------------------------------- the published run


def published_run(conn) -> dict:
    """{'run_id', 'dataset_version'} of the static result currently in flood_cost_lookup."""
    if not _has_table(conn, LOOKUP):
        raise DynamicUnavailable("No static flood-cost result is published (flood_cost_lookup is missing).")
    rows = conn.execute(sa.text(f"SELECT run_id, dataset_version FROM {LOOKUP}")).mappings().all()
    if len(rows) != 1:
        raise DynamicUnavailable(f"Expected one published static result, found {len(rows)}.")
    return {"run_id": rows[0]["run_id"], "dataset_version": rows[0]["dataset_version"]}


# ----------------------------------------------------------------------- inputs: publish


def read_run_samples(monetary_dir) -> pd.DataFrame:
    """Unique sample points + weights of a run's sample_damage_weights.csv.gz (one row per sample)."""
    path = Path(monetary_dir) / "sample_damage_weights.csv.gz"
    if not path.is_file():
        raise DynamicUnavailable(f"{path} is missing: the run has no sample weights.")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        audit = pd.read_csv(handle, dtype={"buildingid": str})
    columns = [*core.SAMPLE_INDEX, "x_epsg2169", "y_epsg2169", "weight_m2"]
    unique = audit[columns].drop_duplicates()
    if unique.duplicated(core.SAMPLE_INDEX).any():
        raise ValueError("A sample has different coordinates or weights across scenarios")
    unique["weight_m2"] = unique["weight_m2"].fillna(0.0)
    return unique.sort_values(core.SAMPLE_INDEX).reset_index(drop=True)


def publish_inputs(conn, run_id: str, dataset_version: str, monetary_dir) -> dict:
    """Write the run's sample weights and JRC parameters (replacing the previous run's)."""
    samples = read_run_samples(monetary_dir)
    jrc_path = Path(monetary_dir) / "jrc_parameters.json"
    if not jrc_path.is_file():
        raise DynamicUnavailable(f"{jrc_path} is missing.")
    jrc = json.loads(jrc_path.read_text(encoding="utf-8"))
    parameters_by_class = jrc.get("parameters", jrc)
    missing = [c for c in CATEGORIES if c not in parameters_by_class]
    if missing:
        raise ValueError(f"jrc_parameters.json lacks {missing}")
    samples.assign(run_id=run_id, dataset_version=dataset_version).to_sql(
        SAMPLES, conn, if_exists="replace", index=False)
    pd.DataFrame([{"run_id": run_id, "dataset_version": dataset_version,
                   "parameters": json.dumps(parameters_by_class)}]).to_sql(JRC, conn, if_exists="replace", index=False)
    return {"table": SAMPLES, "samples": len(samples), "buildings": int(samples["buildingid"].nunique()),
            "jrc_table": JRC}


# ----------------------------------------------------------------------- inputs: read


def load_samples(conn, run: dict) -> pd.DataFrame:
    """Samples of the published run's classified buildings, indexed (buildingid, sample_index).

    Columns x_epsg2169, y_epsg2169, weight_m2, jrc_category. Order is fixed (sorted), so the
    backend's depths and the worker's models line up sample by sample.
    """
    if not _has_table(conn, SAMPLES):
        raise DynamicUnavailable("flood_cost_sample_weights is missing: run publish_dynamic_inputs.")
    samples = pd.read_sql(sa.text(
        f"SELECT buildingid, sample_index, x_epsg2169, y_epsg2169, weight_m2, dataset_version FROM {SAMPLES} "
        "WHERE run_id = :run"), conn, params={"run": run["run_id"]})
    if samples.empty or set(samples["dataset_version"]) != {run["dataset_version"]}:
        raise DynamicUnavailable(f"No sample weights for the published run {run['run_id']}: "
                                 "run publish_dynamic_inputs for it.")
    classes = pd.read_sql(sa.text(
        f"SELECT buildingid, jrc_category, accepted_provisional_classification FROM {BUILDINGS} "
        "WHERE run_id = :run"), conn, params={"run": run["run_id"]})
    accepted = classes["accepted_provisional_classification"].map(_true) & classes["jrc_category"].isin(CATEGORIES)
    classes = classes.loc[accepted, ["buildingid", "jrc_category"]].drop_duplicates("buildingid")
    out = samples.drop(columns="dataset_version").merge(classes, on="buildingid", how="inner")
    out["sample_index"] = out["sample_index"].astype(int)
    out["weight_m2"] = out["weight_m2"].astype(float).fillna(0.0)
    return out.set_index(core.SAMPLE_INDEX).sort_index()


def samples_signature(samples: pd.DataFrame) -> str:
    """Short fingerprint of the sample order, so depths from the backend can be checked to line up."""
    text = "\n".join(f"{b}:{s}" for b, s in samples.index)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_jrc(conn, run: dict) -> dict:
    if not _has_table(conn, JRC):
        raise DynamicUnavailable("flood_cost_jrc_parameters is missing: run publish_dynamic_inputs.")
    row = conn.execute(sa.text(f"SELECT parameters, dataset_version FROM {JRC} WHERE run_id = :run"),
                       {"run": run["run_id"]}).mappings().first()
    if row is None or row["dataset_version"] != run["dataset_version"]:
        raise DynamicUnavailable(f"No JRC parameters for the published run {run['run_id']}.")
    return json.loads(row["parameters"])


def _act_id(value):
    match = ACT_ID.search(str(value)) if value is not None else None
    return match.group(0) if match else None


def load_attributes(conn, samples: pd.DataFrame) -> pd.DataFrame | None:
    """Model 3 attributes per classified building: residents and floor area (NaN = unknown).

    From building_population (read only): est_population, and footprint_m2 x floors (or
    x height / FLOOR_HEIGHT_M when the floor count is missing). None when the table is absent.
    """
    classes = samples.reset_index().drop_duplicates("buildingid").set_index("buildingid")["jrc_category"]
    if not _has_table(conn, POPULATION):
        return None
    columns = {c["name"] for c in sa.inspect(conn).get_columns(POPULATION)}
    id_columns = [c for c in ("buildingid", "gml_id", "inspireid", "localid", "id") if c in columns]
    wanted = [c for c in ("est_population", "footprint_m2", "floors", "final_height_m") if c in columns]
    if not id_columns or "est_population" not in wanted:
        return None
    frame = pd.read_sql(sa.text(f"SELECT {', '.join(id_columns + wanted)} FROM {POPULATION}"), conn)
    frame["buildingid"] = None
    for column in id_columns:          # the first column that holds an ACT_ id
        frame["buildingid"] = frame["buildingid"].fillna(frame[column].map(_act_id))
    frame = frame.dropna(subset=["buildingid"]).drop_duplicates("buildingid").set_index("buildingid")
    for column in ("est_population", "footprint_m2", "floors", "final_height_m"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce") if column in frame.columns else np.nan
    floors = frame["floors"].where(frame["floors"] > 0, frame["final_height_m"] / parameters.FLOOR_HEIGHT_M)
    attributes = pd.DataFrame({"residents": frame["est_population"],
                               "floor_area_m2": frame["footprint_m2"] * floors}).reindex(classes.index)
    attributes["jrc_category"] = classes
    return attributes


def static_totals(conn) -> dict:
    """Static model totals per scenario ('Classified total' rows of flood_cost_summary), for reference lines."""
    if not _has_table(conn, "flood_cost_summary"):
        return {}
    rows = pd.read_sql(sa.text("SELECT * FROM flood_cost_summary"), conn)
    rows = rows[rows["group"] == "Classified total"]
    return {str(r.scenario).upper(): float(r.known_estimates_sum_eur2010)
            for r in rows.itertuples() if pd.notna(r.known_estimates_sum_eur2010)}


# ----------------------------------------------------------------------- results: write


def write_curve(conn, run: dict, totals: pd.DataFrame, buildings: pd.DataFrame, meta: dict) -> None:
    """Replace the stage curve with this run's."""
    stamp = {"run_id": run["run_id"], "dataset_version": run["dataset_version"], **meta}
    totals.reset_index().assign(**stamp).to_sql(CURVE, conn, if_exists="replace", index=False)
    buildings.assign(run_id=run["run_id"], dataset_version=run["dataset_version"]).to_sql(
        CURVE_BUILDINGS, conn, if_exists="replace", index=False)


def write_event(conn, event_id: str, run: dict, meta: dict, totals: pd.DataFrame, buildings: pd.DataFrame) -> None:
    """Add (or replace) one event."""
    for table in (EVENTS, EVENT_TOTALS, EVENT_BUILDINGS):
        if _has_table(conn, table):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE event_id = :e"), {"e": event_id})
    stamp = {"event_id": event_id, "run_id": run["run_id"], "dataset_version": run["dataset_version"]}
    pd.DataFrame([{**stamp, **{k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in meta.items()}}]
                 ).to_sql(EVENTS, conn, if_exists="append", index=False)
    _iso_times(totals).assign(event_id=event_id).to_sql(EVENT_TOTALS, conn, if_exists="append", index=False)
    _iso_times(buildings).assign(event_id=event_id).to_sql(EVENT_BUILDINGS, conn, if_exists="append", index=False)


def _iso_times(frame: pd.DataFrame) -> pd.DataFrame:
    """Timestamps as ISO-8601 UTC text ("2021-07-14T02:00:00Z"): sortable, same on every database."""
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


# ----------------------------------------------------------------------- results: read (API)


def _records(frame: pd.DataFrame) -> list:
    """JSON-ready rows: NaN -> None."""
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def read_curve(conn) -> pd.DataFrame:
    if not _has_table(conn, CURVE):
        raise DynamicUnavailable("The stage curve has not been computed yet (run_dynamic_curve).")
    return pd.read_sql(sa.text(f"SELECT * FROM {CURVE} ORDER BY stage_cm"), conn)


def read_curve_buildings(conn, buildingid: str | None = None, stage_cm: float | None = None) -> pd.DataFrame:
    if not _has_table(conn, CURVE_BUILDINGS):
        raise DynamicUnavailable("The stage curve has not been computed yet (run_dynamic_curve).")
    where, params = [], {}
    if buildingid is not None:
        where.append("buildingid = :b")
        params["b"] = buildingid
    if stage_cm is not None:
        where.append("stage_cm = :s")
        params["s"] = stage_cm
    sql = f"SELECT * FROM {CURVE_BUILDINGS}" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY stage_cm"
    return pd.read_sql(sa.text(sql), conn, params=params)


def read_events(conn) -> pd.DataFrame:
    if not _has_table(conn, EVENTS):
        return pd.DataFrame()
    return pd.read_sql(sa.text(f"SELECT * FROM {EVENTS} ORDER BY computed_utc DESC"), conn)


def read_event(conn, event_id: str) -> tuple:
    events = read_events(conn)
    row = events[events["event_id"] == event_id] if not events.empty else events
    if row.empty:
        raise DynamicUnavailable(f"No dynamic event {event_id!r}.")
    totals = pd.read_sql(sa.text(f"SELECT * FROM {EVENT_TOTALS} WHERE event_id = :e ORDER BY timestamp"),
                         conn, params={"e": event_id})
    return row.iloc[0].to_dict(), totals


def read_event_building(conn, event_id: str, buildingid: str) -> pd.DataFrame:
    if not _has_table(conn, EVENT_BUILDINGS):
        raise DynamicUnavailable("No dynamic event has been computed yet (run_dynamic_event).")
    return pd.read_sql(sa.text(f"SELECT * FROM {EVENT_BUILDINGS} WHERE event_id = :e AND buildingid = :b "
                               "ORDER BY timestamp"), conn, params={"e": event_id, "b": buildingid})
