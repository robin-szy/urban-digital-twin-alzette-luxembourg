"""Tests of the hydrology core: GR4J, Oudin evaporation, KGE, DDS and the calibration."""

import math

import numpy as np
import pytest

from eddie_weather import hydro


def synthetic_weather(days=3 * 365, seed=3):
    """Rain on about a third of the days, and a seasonal evaporation, like a temperate climate."""
    rng = np.random.default_rng(seed)
    wet = rng.random(days) < 0.35
    rain = np.where(wet, rng.gamma(0.8, 7.0, days), 0.0)
    doy = np.arange(days) % 365 + 1
    pet = hydro.oudin_pet(10.0 - 8.0 * np.cos(2 * np.pi * (doy - 15) / 365.0), doy, 49.6)
    return rain, pet


# --- unit hydrographs -------------------------------------------------------------------

@pytest.mark.parametrize("x4", [0.5, 1.0, 1.7, 2.0, 3.4, 5.0])
def test_unit_hydrographs_sum_to_one_and_have_the_right_length(x4):
    uh1, uh2 = hydro.unit_hydrographs(x4)
    assert uh1.sum() == pytest.approx(1.0, abs=1e-12)
    assert uh2.sum() == pytest.approx(1.0, abs=1e-12)
    assert len(uh1) == math.ceil(x4)
    assert len(uh2) == math.ceil(2 * x4)
    assert (uh1 >= 0).all() and (uh2 >= 0).all()


def test_unit_hydrograph_values_for_x4_two_days():
    uh1, uh2 = hydro.unit_hydrographs(2.0)
    # SH1(1) = (1/2)^2.5, SH2(1) = 0.5 (1/2)^2.5, SH2(3) = 1 - 0.5 (1/2)^2.5
    assert uh1[0] == pytest.approx(0.5 ** 2.5)
    assert uh2[0] == pytest.approx(0.5 * 0.5 ** 2.5)
    assert uh2[2] == pytest.approx(0.5 - 0.5 * 0.5 ** 2.5)


# --- GR4J -------------------------------------------------------------------------------

def test_water_balance_closes_exactly():
    rain, pet = synthetic_weather()
    params = (350.0, -1.2, 90.0, 1.7)
    start = hydro.GR4JState.initial(params)
    run = hydro.gr4j(params, rain, pet, start)
    end = run.state
    stored_before = start.production + start.routing + sum(start.uh1) + sum(start.uh2)
    stored_after = end.production + end.routing + sum(end.uh1) + sum(end.uh2)
    water_in = rain.sum() + run.exchange.sum()
    water_out = run.actual_evaporation.sum() + run.flow.sum()
    assert water_in - water_out == pytest.approx(stored_after - stored_before, abs=1e-8)


def test_no_exchange_means_no_water_gained_or_lost():
    rain, pet = synthetic_weather()
    run = hydro.gr4j((300.0, 0.0, 80.0, 2.0), rain, pet)
    assert np.allclose(run.exchange, 0.0)


def test_flow_is_never_negative_and_stores_stay_within_capacity():
    rain, pet = synthetic_weather()
    params = (250.0, 2.5, 60.0, 1.2)
    run = hydro.gr4j(params, rain, pet)
    assert (run.flow >= 0).all()
    assert (run.production_store >= 0).all() and (run.production_store <= params[0] + 1e-9).all()
    assert (run.routing_store >= 0).all() and (run.routing_store <= params[2] + 1e-9).all()
    assert (run.actual_evaporation <= pet + rain + 1e-9).all()


def test_flow_recedes_without_rain():
    params = (300.0, 0.0, 100.0, 2.0)
    state = hydro.GR4JState(250.0, 80.0, [0.0, 0.0], [0.0] * 4)
    run = hydro.gr4j(params, np.zeros(60), np.zeros(60), state)
    assert (np.diff(run.flow) <= 1e-12).all()
    assert run.flow[-1] < run.flow[0]


def test_a_storm_comes_out_later_and_spread_over_days():
    params = (300.0, 0.0, 100.0, 3.0)
    rain = np.zeros(30)
    rain[5] = 50.0
    run = hydro.gr4j(params, rain, np.zeros(30), hydro.GR4JState(150.0, 30.0, [0.0] * 3, [0.0] * 6))
    peak_day = int(np.argmax(run.flow))
    assert peak_day > 5, "the peak comes after the rain"
    assert run.flow[5:].sum() > 0 and (run.flow[peak_day + 1:peak_day + 4] > 0).all()


def test_wetter_soil_gives_more_flow_from_the_same_rain():
    params = (300.0, 0.0, 100.0, 2.0)
    rain = np.zeros(20)
    rain[2] = 30.0
    dry = hydro.gr4j(params, rain, np.zeros(20), hydro.GR4JState(30.0, 20.0, [0.0] * 2, [0.0] * 4))
    wet = hydro.gr4j(params, rain, np.zeros(20), hydro.GR4JState(270.0, 20.0, [0.0] * 2, [0.0] * 4))
    assert wet.flow.sum() > 1.5 * dry.flow.sum()


def test_running_in_two_pieces_equals_running_at_once():
    rain, pet = synthetic_weather()
    params = (350.0, -0.8, 90.0, 2.3)
    whole = hydro.gr4j(params, rain, pet)
    first = hydro.gr4j(params, rain[:400], pet[:400])
    saved = hydro.GR4JState.from_dict(first.state.to_dict())  # as stored in the database
    second = hydro.gr4j(params, rain[400:], pet[400:], saved)
    assert np.allclose(np.concatenate([first.flow, second.flow]), whole.flow, atol=1e-12)


def test_a_state_of_another_x4_is_refused():
    state = hydro.GR4JState.initial((300.0, 0.0, 80.0, 2.0))
    with pytest.raises(ValueError):
        hydro.gr4j((300.0, 0.0, 80.0, 3.5), np.zeros(3), np.zeros(3), state)


# --- Oudin ------------------------------------------------------------------------------

def test_extraterrestrial_radiation_matches_fao56():
    # FAO-56 example 8: 20 degrees south, 3 September (day 246) -> 32.2 MJ m-2 day-1.
    assert hydro.extraterrestrial_radiation(246, -20.0) == pytest.approx(32.2, abs=0.1)


def test_oudin_values_for_luxembourg():
    # Mid-June at 49.6 N: Ra about 41.7 MJ; at 20 C, (20 + 5) / 100 of Ra / 2.45 -> about 4.25 mm.
    june = hydro.oudin_pet(20.0, 167, 49.6)
    assert 4.0 < june < 4.5
    # Mid-December at 2 C: well under 1 mm.
    assert 0.0 < hydro.oudin_pet(2.0, 350, 49.6) < 0.5
    # At or below -5 C: no evaporation.
    assert hydro.oudin_pet(-5.0, 20, 49.6) == 0.0
    assert hydro.oudin_pet(-12.0, 20, 49.6) == 0.0


# --- scores -----------------------------------------------------------------------------

def test_kge_perfect_biased_and_missing_values():
    obs = np.array([1.0, 2.0, 4.0, 3.0, 5.0])
    assert hydro.kge(obs, obs)["kge"] == pytest.approx(1.0)
    # 20 % too much water everywhere: r = 1, alpha = 1.2, beta = 1.2.
    biased = hydro.kge(1.2 * obs, obs)
    assert biased["r"] == pytest.approx(1.0)
    assert biased["alpha"] == pytest.approx(1.2)
    assert biased["beta"] == pytest.approx(1.2)
    assert biased["kge"] == pytest.approx(1.0 - math.sqrt(0.04 + 0.04))
    with_gap = hydro.kge([1.0, 2.0, np.nan, 3.0, 5.0], [1.0, 2.0, 4.0, np.nan, 5.0])
    assert with_gap["n"] == 3 and with_gap["kge"] == pytest.approx(1.0)
    assert math.isnan(hydro.kge([1.0, 1.0], [2.0, 2.0])["kge"])


def test_nse():
    obs = np.array([1.0, 2.0, 3.0])
    assert hydro.nse(obs, obs) == pytest.approx(1.0)
    assert hydro.nse(np.full(3, 2.0), obs) == pytest.approx(0.0)


def test_unit_conversion_round_trip():
    # 1 mm/day over 86.4 km2 is 1 m3/s.
    assert hydro.mm_per_day_to_m3s(1.0, 86.4) == pytest.approx(1.0)
    assert hydro.m3s_to_mm_per_day(2.5, 317.78) * 317.78 * 1000 / 86400 == pytest.approx(2.5)


# --- calibration ------------------------------------------------------------------------

def test_dds_finds_the_minimum_of_a_bowl():
    target = np.array([3.0, -1.0, 0.5])
    best, value, n = hydro.dds(lambda x: float(((x - target) ** 2).sum()), [-5, -5, -5], [5, 5, 5], iterations=800, seed=7)
    assert value < 1e-2
    assert np.allclose(best, target, atol=0.1)
    assert n == 800


def test_calibration_recovers_a_known_model():
    rain, pet = synthetic_weather(days=6 * 365, seed=11)
    truth = (420.0, -0.9, 85.0, 1.9)
    observed = hydro.gr4j(truth, rain, pet).flow
    result = hydro.calibrate_gr4j(rain, pet, observed, calibration_days=slice(0, 4 * 365),
                                  validation_days=slice(4 * 365, 6 * 365), iterations=600, seed=2)
    assert result.calibration["kge"] > 0.99
    assert result.validation["kge"] > 0.98, "a model tuned on 4 years predicts the next 2"
    # x1 and x3 partly compensate each other, so they are recovered roughly; x4 well.
    assert result.params["x1"] == pytest.approx(truth[0], rel=0.3)
    assert result.params["x3"] == pytest.approx(truth[2], rel=0.4)
    assert result.params["x4"] == pytest.approx(truth[3], abs=0.3)
    assert result.at_bound == []
