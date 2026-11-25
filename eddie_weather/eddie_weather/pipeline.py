"""The work the scheduled tasks do, without Celery: set up, fill, keep up to date, model.

Every step reads what is already stored and continues from there, so a restart, a missed
night or a new deployment only makes the next run longer (catch-up), never wrong:

* :func:`setup_places` - sub-catchments from the gauge catchments, with their grid weights.
* :func:`backfill` - reanalysis (ERA5 rain, ERA5-Land temperature), one calendar year per chunk, logged, within the call budget.
* :func:`catch_up` - reanalysis from the last final hour to the newest day of the archive.
* :func:`update_forecast` - ICON values for the last week and the coming one (provisional).
* :func:`rollup` - day, month, season and year values from the hourly ones.
* :func:`calibrate_gauge` and :func:`update_flow` - GR4J river flow, final then provisional.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from sqlalchemy.engine import Connection

from eddie_weather import alzette, catchment, db, hydro, sources

log = logging.getLogger(__name__)

UNITS = {"rain": "mm", "temperature": "degC", "flow": "m3/s"}
HOW = {"rain": "sum", "temperature": "mean"}
ROLLUP_LEVELS = ("day", "month", "season", "year")
KEEP_15MIN_DAYS = 7
FLOW_SOURCE = "GR4J (tuned on CAMELS-LUX) on ERA5 rain"
FLOW_SOURCE_PROVISIONAL = "GR4J on ICON forecast"
BACKFILL_JOB = "archive"
FAR_FUTURE = pd.Timestamp("2200-01-01", tz="UTC")


# --- places -------------------------------------------------------------------------------

def setup_places(conn: Connection, catchments_by_gauge: Mapping[int, BaseGeometry],
                 step_deg: float = catchment.ERA5_LAND_STEP_DEG) -> catchment.Places:
    """Store the sub-catchments (with grid weights) and the gauge catchments of the Alzette."""
    wholes = {g.place_id: catchments_by_gauge[g.camels_id] for g in alzette.GAUGES}
    upstream = {g.place_id: [alzette.BY_ID[u].place_id for u in g.upstream] for g in alzette.GAUGES}
    subs = catchment.sub_catchments(wholes, upstream)
    places = catchment.Places.build(subs, step_deg)
    rows = []
    for i, gauge in enumerate(alzette.GAUGES):
        rows.append({"place_id": gauge.place_id, "name": f"{gauge.name} (between gauges)", "kind": "sub_catchment",
                     "gauge_id": gauge.camels_id, "area_km2": float(places.area_km2[i]),
                     "geojson": mapping(subs[gauge.place_id]),
                     "weights": {"points": [list(p) for p in places.points], "weights": places.weights[i].tolist()}})
        rows.append({"place_id": gauge.catchment_id, "name": f"Alzette at {gauge.name}", "kind": "gauge_catchment",
                     "gauge_id": gauge.camels_id, "area_km2": catchment.area_km2(wholes[gauge.place_id]),
                     "geojson": mapping(wholes[gauge.place_id]), "weights": None})
    db.upsert_places(conn, rows)
    return places


def load_places(conn: Connection) -> catchment.Places:
    """The sub-catchments and their weights, as stored by :func:`setup_places`."""
    rows = db.read_places(conn, "sub_catchment")
    if not rows:
        raise LookupError("no places yet: run the setup with the catchment outlines first")
    points = [tuple(p) for p in rows[0]["weights"]["points"]]
    return catchment.Places(names=[r["place_id"] for r in rows],
                            area_km2=np.array([r["area_km2"] for r in rows]),
                            points=points, weights=np.array([r["weights"]["weights"] for r in rows]))


def _sub_ids_above(gauge: alzette.Gauge) -> List[str]:
    return [alzette.BY_ID[g].place_id for g in alzette.upstream_of(gauge.camels_id)]


# --- storing and rolling up ---------------------------------------------------------------

def store(conn: Connection, places: catchment.Places, by_point: Mapping[str, pd.DataFrame], level: str,
          quality: str, source) -> Dict[str, int]:
    """Values per grid point -> values per sub-catchment, stored at ``level``.

    ``source`` is one label, or a label per variable.
    """
    written = {}
    for variable, frame in by_point.items():
        if frame.empty:
            continue
        averaged = pd.DataFrame(places.average(frame.to_numpy()), index=frame.index, columns=places.names)
        label = source[variable] if isinstance(source, Mapping) else source
        written[variable] = db.write_values(conn, averaged, variable, level, UNITS[variable], quality, label)
    return written


def _local_year_bounds(start: pd.Timestamp, end: pd.Timestamp) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """From the start of the (meteorological) year of ``start`` to the end of the year of ``end``.

    Winter starts on 1 December, so the window opens a month earlier than 1 January.
    """
    tz = catchment.LOCAL_TIME_ZONE
    first = pd.Timestamp(year=start.tz_convert(tz).year - 1, month=12, day=1).tz_localize(tz)
    last = pd.Timestamp(year=end.tz_convert(tz).year + 1, month=1, day=1).tz_localize(tz)
    return first.tz_convert("UTC"), last.tz_convert("UTC")


def rollup(conn: Connection, places: catchment.Places, variable: str, start, end) -> int:
    """Recompute day to year values of the periods touched by [start, end), from the hours.

    A period is final when all its hours are final, provisional otherwise. The gauge
    catchments get the area-weighted mean of the sub-catchments above them.
    """
    first, last = _local_year_bounds(pd.Timestamp(start), pd.Timestamp(end))
    long = db.read_values(conn, places.names, variable, "hour", first, last)
    if long.empty:
        return 0
    values = db.wide(long, places.names)
    final = long.assign(f=(long["quality"] == db.FINAL).astype(float)) \
        .pivot(index="time_utc", columns="place_id", values="f").reindex(columns=places.names)
    series_by_place = {name: (values[name], final[name]) for name in places.names}
    for gauge in alzette.GAUGES:
        subs = _sub_ids_above(gauge)
        if all(s in values for s in subs):
            block = values[subs]
            combined = pd.Series(places.combine(subs, block.to_numpy()), index=block.index)
            combined[block.isna().any(axis=1)] = np.nan
            series_by_place[gauge.catchment_id] = (combined, final[subs].min(axis=1))
    touched_from = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start).tz_localize("UTC")
    written = 0
    for level in ROLLUP_LEVELS:
        for quality in (db.FINAL, db.PROVISIONAL):
            table, flags = {}, {}
            for name, (series, is_final) in series_by_place.items():
                series = series.dropna()
                if series.empty:
                    continue
                agg = catchment.aggregate(series, level, HOW[variable], step_minutes=60)
                fin = catchment.aggregate(is_final.reindex(series.index), level, "mean", step_minutes=60)
                all_final = (fin["value"].to_numpy() >= 1.0 - 1e-9) & agg["complete"].to_numpy()
                keep = all_final if quality == db.FINAL else ~all_final
                ends = agg["start"].shift(-1).fillna(FAR_FUTURE)
                keep &= (ends > touched_from).to_numpy()
                part = agg[keep]
                table[name] = pd.Series(part["value"].to_numpy(), index=part["start"])
                flags[name] = pd.Series(part["complete"].to_numpy(), index=part["start"])
            frame = pd.DataFrame(table)
            if frame.empty:
                continue
            written += db.write_values(conn, frame, variable, level, UNITS[variable], quality,
                                       sources.ARCHIVE_SOURCES[variable] if quality == db.FINAL
                                       else f"{sources.ARCHIVE_SOURCES[variable]} and ICON",
                                       complete=pd.DataFrame(flags).reindex(frame.index).fillna(False))
    return written


# --- archive ------------------------------------------------------------------------------

def _years_in_order(first_year: int, last_year: int, model_from: int = 2002) -> List[int]:
    """Years the model needs first (from ``model_from`` up), then the older ones backwards."""
    recent = list(range(max(first_year, model_from), last_year + 1))
    older = list(range(min(model_from, last_year + 1) - 1, first_year - 1, -1))
    return recent + older


def fetch_archive(conn: Connection, places: catchment.Places, start: dt.date, end: dt.date,
                  fetch=None, budget: Optional[sources.CallBudget] = None, today: Optional[dt.date] = None) -> int:
    frames = sources.archive_hourly(places.points, start, end, fetch=fetch, budget=budget, today=today)
    written = store(conn, places, frames, "hour", db.FINAL, sources.ARCHIVE_SOURCES)
    for variable, frame in frames.items():
        if not frame.empty:
            rollup(conn, places, variable, frame.index.min(), frame.index.max() + pd.Timedelta(hours=1))
    return sum(written.values())


def _save(checkpoint: Optional[Callable[[], None]]) -> None:
    if checkpoint is not None:
        checkpoint()


def _fetch_by_month(conn: Connection, places: catchment.Places, start: dt.date, end: dt.date, fetch,
                    budget: Optional[sources.CallBudget], today: Optional[dt.date],
                    checkpoint: Optional[Callable[[], None]]) -> int:
    """Fetch and store month by month, saving after each: a dropped connection loses one month only."""
    rows = 0
    for first, last in sources.split_months(start, end):
        rows += fetch_archive(conn, places, first, last, fetch, budget, today)
        _save(checkpoint)
    return rows


def backfill(conn: Connection, places: catchment.Places, first_year: int = 1950, fetch=None,
             budget: Optional[sources.CallBudget] = None, max_chunks: Optional[int] = None,
             today: Optional[dt.date] = None, checkpoint: Optional[Callable[[], None]] = None) -> dict:
    """Fill the archive one calendar year at a time; stops cleanly when the day's budget is spent.

    The current year is logged as done up to the newest day of the archive; the days that
    follow are the catch-up's job. ``checkpoint`` (for example the connection's commit) is
    called after each month, so the months already fetched are kept if a later one fails.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    latest = today - dt.timedelta(days=sources.ARCHIVE_LAG_DAYS)
    done = db.done_chunks(conn, BACKFILL_JOB)
    report = {"done": [], "failed": [], "stopped": None}
    for year in _years_in_order(first_year, latest.year):
        if str(year) in done:
            continue
        if max_chunks is not None and len(report["done"]) + len(report["failed"]) >= max_chunks:
            report["stopped"] = "chunk limit"
            break
        start, end = dt.date(year, 1, 1), min(dt.date(year, 12, 31), latest)
        db.mark_job(conn, BACKFILL_JOB, str(year), "running")
        _save(checkpoint)
        try:
            rows = _fetch_by_month(conn, places, start, end, fetch, budget, today, checkpoint)
        except sources.SourceError as error:
            db.mark_job(conn, BACKFILL_JOB, str(year), "failed", message=str(error))
            _save(checkpoint)
            if "budget" in str(error):
                report["stopped"] = "budget"
                break
            report["failed"].append(year)
            continue
        db.mark_job(conn, BACKFILL_JOB, str(year), "done", rows=rows, message=f"{start} to {end}")
        _save(checkpoint)
        report["done"].append(year)
    return report


def catch_up(conn: Connection, places: catchment.Places, fetch=None, budget: Optional[sources.CallBudget] = None,
             today: Optional[dt.date] = None, max_days: int = 366,
             checkpoint: Optional[Callable[[], None]] = None) -> dict:
    """Reanalysis from the day of the last final hour to the newest day of the archive.

    Runs after every restart and once a day; the day of the last final hour is fetched again
    in case it was partial. With nothing stored yet, only the last ``max_days`` are fetched
    (the older years are the backfill's job).
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    latest = today - dt.timedelta(days=sources.ARCHIVE_LAG_DAYS)
    last = db.last_time(conn, "rain", "hour", db.FINAL)
    start = last.date() if last is not None else latest - dt.timedelta(days=max_days - 1)
    start = max(start, latest - dt.timedelta(days=max_days - 1))
    if start > latest:
        return {"from": None, "to": None, "rows": 0}
    rows = _fetch_by_month(conn, places, start, latest, fetch, budget, today, checkpoint)
    return {"from": start.isoformat(), "to": latest.isoformat(), "rows": rows}


# --- forecast -----------------------------------------------------------------------------

def update_forecast(conn: Connection, places: catchment.Places, fetch=None,
                    budget: Optional[sources.CallBudget] = None, now: Optional[pd.Timestamp] = None,
                    past_days: int = 7, forecast_days: int = 7) -> dict:
    """ICON values from a week ago to a week ahead, provisional (final values are kept).

    The week back covers the days the reanalysis has not reached yet, even after a restart.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    answer = sources.forecast(places.points, past_days, forecast_days, fetch=fetch, budget=budget)
    rows = store(conn, places, answer["15min"], "15min", db.PROVISIONAL, sources.FORECAST_SOURCE)
    hourly = store(conn, places, answer["hour"], "hour", db.PROVISIONAL, sources.FORECAST_SOURCE)
    for variable, frame in answer["hour"].items():
        if not frame.empty:
            rollup(conn, places, variable, frame.index.min(), frame.index.max() + pd.Timedelta(hours=1))
    dropped = db.delete_values(conn, "15min", now - pd.Timedelta(days=KEEP_15MIN_DAYS))
    return {"15min": rows, "hour": hourly, "dropped_15min": dropped}


# --- river flow ---------------------------------------------------------------------------

def model_inputs(conn: Connection, places: catchment.Places, gauge: alzette.Gauge, start, end) -> pd.DataFrame:
    """Daily rain, temperature, evaporation (mm) and the share of final hours over a gauge's catchment.

    Days run on UTC+1 all year, like the CAMELS-LUX flow the model is tuned on.
    """
    subs = _sub_ids_above(gauge)
    columns = {}
    final_share = None
    for variable in ("rain", "temperature"):
        long = db.read_values(conn, subs, variable, "hour", start, end)
        if long.empty:
            return pd.DataFrame(columns=["rain", "temperature", "pet", "final"])
        block = db.wide(long, subs)
        combined = pd.Series(places.combine(subs, block.to_numpy()), index=block.index)
        combined[block.isna().any(axis=1)] = np.nan
        columns[variable] = catchment.daily_for_model(combined, HOW[variable])
        if variable == "rain":
            flag = long.assign(f=(long["quality"] == db.FINAL).astype(float)) \
                .pivot(index="time_utc", columns="place_id", values="f").min(axis=1)
            final_share = catchment.daily_for_model(flag, "mean")
    frame = pd.DataFrame(columns)
    frame["final"] = final_share.reindex(frame.index)
    doy = frame.index.dayofyear.to_numpy()
    frame["pet"] = hydro.oudin_pet(frame["temperature"].to_numpy(), doy, alzette.LATITUDE_DEG)
    return frame


def calibrate_gauge(conn: Connection, places: catchment.Places, gauge_id: int, observed_m3s: pd.Series,
                    iterations: int = 1500, seed: int = 1) -> dict:
    """Tune GR4J for one gauge on CAMELS-LUX flow and the twin's own rain, and store it.

    ``observed_m3s``: daily flow in m3/s, indexed by day (UTC+1 days, as CAMELS-LUX).
    """
    gauge = alzette.BY_ID[gauge_id]
    warm, cal, val = alzette.WARMUP, alzette.CALIBRATION, alzette.VALIDATION
    start = pd.Timestamp(warm[0]).tz_localize("UTC") - pd.Timedelta(hours=1)
    end = pd.Timestamp(val[1]).tz_localize("UTC") + pd.Timedelta(hours=23)
    inputs = model_inputs(conn, places, gauge, start, end)
    days = pd.date_range(warm[0], val[1], freq="D")
    inputs = inputs.reindex(days)
    if inputs[["rain", "pet"]].isna().any().any():
        missing = int(inputs[["rain", "pet"]].isna().any(axis=1).sum())
        raise ValueError(f"{missing} days without rain or temperature for {gauge.name}: finish the backfill first")
    observed = hydro.m3s_to_mm_per_day(observed_m3s.reindex(days).to_numpy(dtype=float), gauge.area_km2)
    position = {d: i for i, d in enumerate(days)}
    cal_days = slice(position[pd.Timestamp(cal[0])], position[pd.Timestamp(cal[1])] + 1)
    val_days = slice(position[pd.Timestamp(val[0])], position[pd.Timestamp(val[1])] + 1)
    result = hydro.calibrate_gr4j(inputs["rain"].to_numpy(), inputs["pet"].to_numpy(), observed,
                                  cal_days, val_days, warmup_days=cal_days.start, iterations=iterations, seed=seed)
    scores = {"calibration": result.calibration, "validation": result.validation,
              "periods": {"warmup": warm, "calibration": cal, "validation": val}, "at_bound": result.at_bound}
    db.save_model(conn, gauge_id, result.params, scores)
    return {"gauge": gauge.name, "params": result.params, **scores}


def update_flow(conn: Connection, places: catchment.Places, gauge_ids: Optional[Sequence[int]] = None,
                horizon_days: int = 7, today: Optional[dt.date] = None) -> dict:
    """Run each tuned gauge forward: final days from its saved state, then the provisional ones.

    The state is saved only at the end of the final days, so each provisional run starts
    again from measured-quality rain, and the forecast never drifts into the model's memory.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    models = db.read_models(conn)
    report = {}
    for gauge_id in gauge_ids or [g.camels_id for g in alzette.GAUGES]:
        model = models.get(gauge_id)
        if model is None:
            report[gauge_id] = "not tuned yet"
            continue
        gauge = alzette.BY_ID[gauge_id]
        params = tuple(model["params"][k] for k in hydro.PARAMETER_NAMES)
        if model["state_day"] is not None:
            state = hydro.GR4JState.from_dict(model["state"])
            first_day = pd.Timestamp(model["state_day"]) + pd.Timedelta(days=1)
        else:
            state = None
            first_day = pd.Timestamp(alzette.WARMUP[0])
        last_day = pd.Timestamp(today + dt.timedelta(days=horizon_days))
        inputs = model_inputs(conn, places, gauge, first_day.tz_localize("UTC") - pd.Timedelta(hours=1),
                              last_day.tz_localize("UTC") + pd.Timedelta(hours=23))
        inputs = inputs.reindex(pd.date_range(first_day, last_day, freq="D"))
        usable = inputs[["rain", "pet"]].notna().all(axis=1)
        if not usable.any() or not usable.iloc[0]:
            report[gauge_id] = "no input yet"
            continue
        # Stop at the first gap: the model cannot jump over missing days.
        stop = int(np.argmin(usable.to_numpy())) if not usable.all() else len(usable)
        inputs = inputs.iloc[:stop]
        final = (inputs["final"] >= 1.0 - 1e-9).to_numpy()
        n_final = int(np.argmin(final)) if not final.all() else len(final)
        written = 0
        if n_final:
            run = hydro.gr4j(params, inputs["rain"].to_numpy()[:n_final], inputs["pet"].to_numpy()[:n_final], state)
            written += _write_flow(conn, gauge, inputs.index[:n_final], run.flow, db.FINAL)
            db.save_state(conn, gauge_id, run.state.to_dict(), inputs.index[n_final - 1].date())
            state = run.state
        if n_final < len(inputs):
            rest = inputs.iloc[n_final:]
            run = hydro.gr4j(params, rest["rain"].to_numpy(), rest["pet"].to_numpy(), state)
            written += _write_flow(conn, gauge, rest.index, run.flow, db.PROVISIONAL)
        report[gauge_id] = {"final_days": n_final, "provisional_days": len(inputs) - n_final, "rows": written}
    return report


def _write_flow(conn: Connection, gauge: alzette.Gauge, days: pd.DatetimeIndex, flow_mm, quality: str) -> int:
    # A model day runs from 00:00 UTC+1, that is 23:00 UTC the day before.
    times = days.tz_localize("UTC") - pd.Timedelta(hours=1)
    frame = pd.DataFrame({gauge.catchment_id: hydro.mm_per_day_to_m3s(np.asarray(flow_mm), gauge.area_km2)},
                         index=times)
    source = FLOW_SOURCE if quality == db.FINAL else FLOW_SOURCE_PROVISIONAL
    return db.write_values(conn, frame, "flow", "day", UNITS["flow"], quality, source)


# --- reading for the map ------------------------------------------------------------------

LEVEL_FOR_SPAN = ((dt.timedelta(days=3), "15min"), (dt.timedelta(days=31), "hour"), (dt.timedelta(days=731), "day"),
                  (dt.timedelta(days=366 * 15), "month"))


def choose_level(start: pd.Timestamp, end: pd.Timestamp, kind: str = "sub_catchment", variable: str = "rain") -> str:
    """The finest level that keeps an answer small (a few thousand values at most)."""
    span = end - start
    for limit, level in LEVEL_FOR_SPAN:
        if span <= limit:
            if variable == "flow" or kind == "gauge_catchment":
                return "day" if level in ("15min", "hour") else level
            return level
    return "year"


def series(conn: Connection, place_ids: Sequence[str], variable: str, start, end, level: str = "auto",
           kind: str = "sub_catchment", area_by_place: Optional[Mapping[str, float]] = None) -> dict:
    """What the map asks for: values of some places between two times, at a fitting level.

    The 15-minute level holds only the recent week; older periods fall back to hours.
    Rain also comes with its volume (m3) when the areas are given.
    """
    tz = catchment.LOCAL_TIME_ZONE  # a time without a zone is Luxembourg time
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize(tz).tz_convert("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize(tz).tz_convert("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if end <= start:
        raise ValueError("end must be after start")
    if level == "auto":
        level = choose_level(start, end, kind, variable)
    long = db.read_values(conn, place_ids, variable, level, start, end)
    if level == "15min" and long.empty:
        level = "hour"
        long = db.read_values(conn, place_ids, variable, level, start, end)
    out = {"variable": variable, "level": level, "unit": UNITS.get(variable, ""), "start": start.isoformat(),
           "end": end.isoformat(), "places": {}}
    for place in place_ids:
        part = long[long["place_id"] == place]
        values = [None if pd.isna(v) else round(float(v), 3) for v in part["value"]]
        entry = {"times": [t.isoformat() for t in part["time_utc"]],
                 "times_local": [t.tz_convert(tz).isoformat() for t in part["time_utc"]], "values": values,
                 "quality": part["quality"].tolist(), "complete": [bool(c) for c in part["complete"]]}
        if variable == "rain" and area_by_place and place in area_by_place:
            entry["volume_m3"] = [None if v is None else round(v * area_by_place[place] * 1000.0) for v in values]
        out["places"][place] = entry
    return out
