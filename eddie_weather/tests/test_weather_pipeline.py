"""The pipeline end to end on SQLite, with a stand-in for the Open-Meteo service."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa
from eddie_weather import alzette, db, hydro, pipeline, sources
from weather_fakes import TODAY, FakeOpenMeteo, nested_catchments, rain_at

@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        db.ensure_tables(connection)
        yield connection


@pytest.fixture
def places(conn):
    return pipeline.setup_places(conn, nested_catchments())


def test_setup_stores_sub_and_gauge_catchments(conn, places):
    stored = db.read_places(conn)
    assert len(stored) == 12
    subs = [r for r in stored if r["kind"] == "sub_catchment"]
    assert [r["gauge_id"] for r in subs] == [1, 2, 3, 4, 5, 6]
    loaded = pipeline.load_places(conn)
    assert loaded.names == places.names and loaded.points == places.points
    assert np.allclose(loaded.weights, places.weights)
    whole = {r["gauge_id"]: r["area_km2"] for r in stored if r["kind"] == "gauge_catchment"}
    assert whole[6] == pytest.approx(sum(r["area_km2"] for r in subs), rel=1e-6)


def test_archive_hours_are_averaged_stored_and_rolled_up(conn, places):
    fake = FakeOpenMeteo()
    pipeline.fetch_archive(conn, places, dt.date(2024, 7, 1), dt.date(2024, 7, 3), fetch=fake, today=TODAY)
    hours = db.wide(db.read_values(conn, places.names, "rain", "hour"), places.names)
    # 72 hours asked; labelled by their start, so the first one is 30 June 23:00 UTC.
    assert len(hours) == 72 and hours.index[0] == pd.Timestamp("2024-06-30 23:00", tz="UTC")
    times = hours.index
    expected = places.average(np.column_stack([rain_at(times, i) for i in range(len(places.points))]))
    assert np.allclose(hours.to_numpy(), np.round(expected, 3), atol=2e-3)
    days = db.read_values(conn, [places.names[0]], "rain", "day")
    july2 = days[days["time_utc"] == pd.Timestamp("2024-07-01 22:00", tz="UTC")].iloc[0]  # local midnight
    local = hours[places.names[0]]
    local = local[(local.index >= "2024-07-01 22:00") & (local.index < "2024-07-02 22:00")]
    assert july2["value"] == pytest.approx(local.sum()) and july2["quality"] == db.FINAL and july2["complete"]
    # The gauge catchment gets days too, from the sub-catchments above it.
    gauge3 = db.read_values(conn, [alzette.BY_ID[3].catchment_id], "rain", "day")
    assert len(gauge3) >= 2


def test_forecast_is_provisional_and_never_overwrites_final(conn, places):
    pipeline.fetch_archive(conn, places, dt.date(2026, 11, 10), dt.date(2026, 11, 15), fetch=FakeOpenMeteo(), today=TODAY)
    final_before = db.read_values(conn, places.names, "rain", "hour", quality=db.FINAL)
    fake = FakeOpenMeteo(forecast_bias=3.0)  # the forecast says three times more rain
    pipeline.update_forecast(conn, places, fetch=fake, now=pd.Timestamp("2026-11-20 10:00", tz="UTC"))
    final_after = db.read_values(conn, places.names, "rain", "hour", quality=db.FINAL)
    pd.testing.assert_frame_equal(final_before.drop(columns="complete"), final_after.drop(columns="complete"))
    provisional = db.read_values(conn, places.names, "rain", "hour", quality=db.PROVISIONAL)
    assert provisional["time_utc"].min() > final_after["time_utc"].max()
    assert provisional["time_utc"].max() >= pd.Timestamp("2026-11-26", tz="UTC")
    quarter = db.read_values(conn, places.names, "rain", "15min")
    assert len(quarter) > 0 and (quarter["quality"] == db.PROVISIONAL).all()
    # A day half final, half provisional is provisional.
    days = db.read_values(conn, [places.names[0]], "rain", "day")
    nov16 = days[days["time_utc"] == pd.Timestamp("2026-11-15 23:00", tz="UTC")].iloc[0]
    assert nov16["quality"] == db.PROVISIONAL


def test_final_values_replace_provisional_ones_when_the_archive_arrives(conn, places):
    pipeline.update_forecast(conn, places, fetch=FakeOpenMeteo(forecast_bias=3.0),
                             now=pd.Timestamp("2026-11-20 10:00", tz="UTC"))
    hour = pd.Timestamp("2026-11-14 12:00", tz="UTC")
    before = db.read_values(conn, [places.names[0]], "rain", "hour", hour, hour + pd.Timedelta(hours=1))
    assert before["quality"].iloc[0] == db.PROVISIONAL
    later = dt.date(2026, 11, 22)  # the archive now reaches 17 November
    report = pipeline.catch_up(conn, places, fetch=FakeOpenMeteo(), today=later, max_days=10)
    assert report["to"] == "2026-11-17"
    after = db.read_values(conn, [places.names[0]], "rain", "hour", hour, hour + pd.Timedelta(hours=1))
    assert after["quality"].iloc[0] == db.FINAL
    assert after["value"].iloc[0] == pytest.approx(before["value"].iloc[0] / 3.0, abs=2e-3)
    days = db.read_values(conn, [places.names[0]], "rain", "day", "2026-11-13", "2026-11-16")
    assert (days["quality"] == db.FINAL).all()


def test_old_quarter_hours_are_dropped(conn, places):
    fake = FakeOpenMeteo()
    pipeline.update_forecast(conn, places, fetch=fake, now=pd.Timestamp("2026-11-20 10:00", tz="UTC"))
    quarter = db.read_values(conn, places.names, "rain", "15min")
    assert quarter["time_utc"].min() >= pd.Timestamp("2026-11-13 10:00", tz="UTC")


def test_catch_up_starts_from_the_last_final_hour(conn, places):
    fake = FakeOpenMeteo()
    pipeline.fetch_archive(conn, places, dt.date(2026, 11, 1), dt.date(2026, 11, 10), fetch=fake, today=TODAY)
    fake.calls.clear()
    report = pipeline.catch_up(conn, places, fetch=fake, today=TODAY)
    assert report["from"] == "2026-11-10" and report["to"] == "2026-11-15"
    assert len(fake.calls) == 1
    assert pipeline.catch_up(conn, places, fetch=fake, today=TODAY)["from"] == "2026-11-15"


def test_backfill_resumes_and_stops_when_the_budget_is_spent(conn, places):
    fake = FakeOpenMeteo()
    jan = dt.date(2026, 1, 12)  # 2026 has seven days in the archive
    report = pipeline.backfill(conn, places, first_year=2026, fetch=fake, today=jan)
    assert report["done"] == [2026]
    assert db.done_chunks(conn, pipeline.BACKFILL_JOB) == {"2026"}
    again = pipeline.backfill(conn, places, first_year=2026, fetch=fake, today=jan)
    assert again["done"] == [] and len(fake.calls) == 1
    broke = sources.CallBudget(per_day=1.0)
    stopped = pipeline.backfill(conn, places, first_year=2025, fetch=fake, budget=broke, today=jan)
    assert stopped["stopped"] == "budget" and stopped["done"] == []
    assert "2025" not in db.done_chunks(conn, pipeline.BACKFILL_JOB)


def test_the_order_of_the_backfill_years():
    assert pipeline._years_in_order(1999, 2004) == [2002, 2003, 2004, 2001, 2000, 1999]
    assert pipeline._years_in_order(2010, 2012) == [2010, 2011, 2012]


def test_call_budget_waits_per_minute_and_refuses_past_the_day():
    clock = {"t": 0.0}
    waits = []
    budget = sources.CallBudget(per_minute=10, per_hour=1000, per_day=25, clock=lambda: clock["t"],
                                sleep=lambda s: (waits.append(s), clock.__setitem__("t", clock["t"] + s)))
    assert budget.take(8) and budget.take(8)  # the second waits for the minute to pass
    assert waits and waits[0] >= 1
    assert budget.take(8)
    assert not budget.take(8)  # 32 > 25 a day
    assert sources.CallBudget.cost(30, 365, 2) == pytest.approx(30 * 365 / 14)
    assert sources.CallBudget.cost(30, 3, 2) == 30


def test_flow_runs_final_then_provisional_and_keeps_the_final_state(conn, places):
    pipeline.fetch_archive(conn, places, dt.date(2026, 9, 1), dt.date(2026, 11, 15), fetch=FakeOpenMeteo(), today=TODAY)
    pipeline.update_forecast(conn, places, fetch=FakeOpenMeteo(), now=pd.Timestamp("2026-11-20 10:00", tz="UTC"))
    params = {"x1": 350.0, "x2": -0.5, "x3": 90.0, "x4": 1.7}
    db.save_model(conn, 2, params, {"calibration": {}, "validation": {}})
    start = hydro.GR4JState.initial(tuple(params.values()))
    db.save_state(conn, 2, start.to_dict(), dt.date(2026, 9, 1))
    report = pipeline.update_flow(conn, places, [2], today=TODAY)[2]
    # The archive's hours of 15 November (UTC) are exactly the model day 15 November (UTC+1).
    assert report["final_days"] == (dt.date(2026, 11, 15) - dt.date(2026, 9, 2)).days + 1
    assert report["provisional_days"] >= 5
    model = db.read_models(conn)[2]
    assert model["state_day"] == dt.date(2026, 11, 15)
    flow = db.read_values(conn, [alzette.BY_ID[2].catchment_id], "flow", "day")
    assert (flow["value"] >= 0).all() and set(flow["quality"]) == {db.FINAL, db.PROVISIONAL}
    # Running again changes nothing final and starts from the saved state.
    again = pipeline.update_flow(conn, places, [2], today=TODAY)[2]
    assert again["final_days"] == 0 and again["provisional_days"] == report["provisional_days"]
    assert pipeline.update_flow(conn, places, [1], today=TODAY)[1] == "not tuned yet"


def test_calibration_on_the_twins_own_rain(conn, places, monkeypatch):
    monkeypatch.setattr(alzette, "WARMUP", ("2024-01-01", "2024-03-31"))
    monkeypatch.setattr(alzette, "CALIBRATION", ("2024-04-01", "2024-08-31"))
    monkeypatch.setattr(alzette, "VALIDATION", ("2024-09-01", "2024-10-31"))
    pipeline.fetch_archive(conn, places, dt.date(2023, 12, 31), dt.date(2024, 11, 1), fetch=FakeOpenMeteo(), today=TODAY)
    gauge = alzette.BY_ID[3]
    inputs = pipeline.model_inputs(conn, places, gauge, pd.Timestamp("2023-12-31 23:00", tz="UTC"),
                                   pd.Timestamp("2024-10-31 23:00", tz="UTC"))
    days = pd.date_range("2024-01-01", "2024-10-31", freq="D")
    inputs = inputs.reindex(days)
    truth = (300.0, -0.5, 70.0, 1.5)
    flow_mm = hydro.gr4j(truth, inputs["rain"].to_numpy(), inputs["pet"].to_numpy()).flow
    observed = pd.Series(hydro.mm_per_day_to_m3s(flow_mm, gauge.area_km2), index=days)
    result = pipeline.calibrate_gauge(conn, places, 3, observed, iterations=300)
    assert result["calibration"]["kge"] > 0.95 and result["validation"]["kge"] > 0.9
    assert db.read_models(conn)[3]["params"]["x4"] == pytest.approx(1.5, abs=0.5)


def test_series_picks_a_level_and_gives_volumes(conn, places):
    pipeline.fetch_archive(conn, places, dt.date(2026, 10, 1), dt.date(2026, 11, 15), fetch=FakeOpenMeteo(), today=TODAY)
    name = places.names[0]
    area = dict(zip(places.names, places.area_km2))
    week = pipeline.series(conn, [name], "rain", "2026-11-01", "2026-11-08", area_by_place=area)
    assert week["level"] == "hour" and len(week["places"][name]["values"]) == 7 * 24
    months = pipeline.series(conn, [name], "rain", "2026-10-01", "2026-11-14", area_by_place=area)
    assert months["level"] == "day"
    entry = months["places"][name]
    assert entry["volume_m3"][3] == pytest.approx(entry["values"][3] * area[name] * 1000.0, abs=1)
    recent = pipeline.series(conn, [name], "rain", "2026-11-14", "2026-11-15")
    assert recent["level"] == "hour"  # no 15-minute values stored yet: falls back to hours
    assert pipeline.choose_level(pd.Timestamp("2000-01-01"), pd.Timestamp("2026-01-01")) == "year"
    assert pipeline.choose_level(pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02"), "gauge_catchment") == "day"
    with pytest.raises(ValueError):
        pipeline.series(conn, [name], "rain", "2026-11-02", "2026-11-01")
