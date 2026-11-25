"""Rain that falls -> water that reaches the river: GR4J, Oudin evaporation, KGE, calibration.

Everything here is pure numpy and has no side effects, so it is tested on its own
(tests/test_hydro.py) and used by the pipeline without any change.

GR4J (Perrin, Michel and Andreassian, 2003, Journal of Hydrology 279) is a daily lumped
rainfall-runoff model with four parameters:

    X1  capacity of the production (soil) store, mm
    X2  groundwater exchange coefficient, mm/day (negative = water lost, positive = gained)
    X3  capacity of the routing store, mm
    X4  time base of the unit hydrograph, days

It takes daily rain P and potential evaporation E (mm/day) over a catchment and returns the
daily flow at its outlet, in mm/day over the catchment area. The implementation follows the
published equations and the order of operations of the reference code (airGR, frun_GR4J).

Oudin et al. (2005, Journal of Hydrology 303) potential evaporation needs only the air
temperature and the latitude, so the same formula serves the past, now, the forecast and
2050; it is also the formula CAMELS-LUX provides (PET_Oudin), which lets us check ours.

KGE (Gupta et al., 2009, Journal of Hydrology 377) scores a simulation against
measurements: 1 is perfect; it combines correlation, variability and bias.

Calibration uses DDS, Dynamically Dimensioned Search (Tolson and Shoemaker, 2007, Water
Resources Research 43), a simple global search widely used for hydrological models and
needing nothing beyond numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------------------

SECONDS_PER_DAY = 86400.0


def mm_per_day_to_m3s(q_mm_day, area_km2: float):
    """Flow in mm/day over ``area_km2`` -> m3/s."""
    return np.asarray(q_mm_day, dtype=float) * area_km2 * 1000.0 / SECONDS_PER_DAY


def m3s_to_mm_per_day(q_m3s, area_km2: float):
    """Flow in m3/s at an outlet -> mm/day over ``area_km2``."""
    return np.asarray(q_m3s, dtype=float) * SECONDS_PER_DAY / (area_km2 * 1000.0)


# ---------------------------------------------------------------------------------------
# Oudin potential evaporation
# ---------------------------------------------------------------------------------------

SOLAR_CONSTANT = 0.0820  # MJ m-2 min-1 (FAO-56)
LATENT_HEAT = 2.45  # MJ kg-1; 1 kg of water on 1 m2 = 1 mm


def extraterrestrial_radiation(day_of_year, latitude_deg: float):
    """Daily extraterrestrial radiation Ra (MJ m-2 day-1), FAO-56 equations 21 to 25."""
    j = np.asarray(day_of_year, dtype=float)
    phi = math.radians(latitude_deg)
    dr = 1.0 + 0.033 * np.cos(2.0 * math.pi / 365.0 * j)
    delta = 0.409 * np.sin(2.0 * math.pi / 365.0 * j - 1.39)
    ws = np.arccos(np.clip(-math.tan(phi) * np.tan(delta), -1.0, 1.0))
    return (24.0 * 60.0 / math.pi * SOLAR_CONSTANT * dr
            * (ws * math.sin(phi) * np.sin(delta) + math.cos(phi) * np.cos(delta) * np.sin(ws)))


def oudin_pet(mean_temp_c, day_of_year, latitude_deg: float):
    """Oudin potential evaporation (mm/day): Ra / lambda * (T + 5) / 100 when T > -5, else 0."""
    t = np.asarray(mean_temp_c, dtype=float)
    ra = extraterrestrial_radiation(day_of_year, latitude_deg)
    return np.where(t + 5.0 > 0.0, ra / LATENT_HEAT * (t + 5.0) / 100.0, 0.0)


# ---------------------------------------------------------------------------------------
# GR4J
# ---------------------------------------------------------------------------------------

# Typical values (median of many catchments, Perrin et al. 2003): where the search starts.
GR4J_TYPICAL = (350.0, 0.0, 90.0, 1.7)

# Parameter ranges searched by the calibration (wide, physically plausible ranges used for
# French and Luxembourg catchments; the calibration reports when a value ends on a limit).
GR4J_BOUNDS: Dict[str, Tuple[float, float]] = {
    "x1": (20.0, 2000.0),
    "x2": (-6.0, 3.0),
    "x3": (5.0, 600.0),
    "x4": (0.5, 5.0),
}
PARAMETER_NAMES = ("x1", "x2", "x3", "x4")


def unit_hydrographs(x4: float) -> Tuple[np.ndarray, np.ndarray]:
    """Ordinates of GR4J's two unit hydrographs for time base ``x4`` (each sums to 1)."""
    if x4 < 0.5:
        raise ValueError("x4 must be at least 0.5 day")

    def sh1(t: float) -> float:
        if t <= 0.0:
            return 0.0
        return (t / x4) ** 2.5 if t < x4 else 1.0

    def sh2(t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t <= x4:
            return 0.5 * (t / x4) ** 2.5
        if t < 2.0 * x4:
            return 1.0 - 0.5 * (2.0 - t / x4) ** 2.5
        return 1.0

    n1 = int(math.ceil(x4))
    n2 = int(math.ceil(2.0 * x4))
    uh1 = np.array([sh1(j) - sh1(j - 1) for j in range(1, n1 + 1)])
    uh2 = np.array([sh2(j) - sh2(j - 1) for j in range(1, n2 + 1)])
    return uh1, uh2


@dataclass
class GR4JState:
    """What GR4J remembers from one day to the next (saved daily by the pipeline)."""

    production: float  # S, mm
    routing: float  # R, mm
    uh1: list = field(default_factory=list)  # water still on its way through unit hydrograph 1
    uh2: list = field(default_factory=list)

    @classmethod
    def initial(cls, params: Sequence[float]) -> "GR4JState":
        """The usual starting point: soil 30 % full, routing store 50 % full, nothing in transit."""
        x1, _x2, x3, x4 = params
        uh1, uh2 = unit_hydrographs(x4)
        return cls(0.3 * x1, 0.5 * x3, [0.0] * len(uh1), [0.0] * len(uh2))

    def to_dict(self) -> dict:
        return {"production": self.production, "routing": self.routing, "uh1": list(self.uh1), "uh2": list(self.uh2)}

    @classmethod
    def from_dict(cls, value: dict) -> "GR4JState":
        return cls(float(value["production"]), float(value["routing"]), list(value["uh1"]), list(value["uh2"]))


@dataclass
class GR4JResult:
    """Daily outputs of a run, all in mm/day over the catchment."""

    flow: np.ndarray  # Q, at the outlet
    actual_evaporation: np.ndarray  # water returned to the air (from the rain and the soil)
    exchange: np.ndarray  # groundwater gained (+) or lost (-)
    production_store: np.ndarray  # S at the end of each day, mm
    routing_store: np.ndarray  # R at the end of each day, mm
    state: GR4JState  # at the end of the last day


def gr4j(params: Sequence[float], rain_mm, pet_mm, state: Optional[GR4JState] = None) -> GR4JResult:
    """Run GR4J day by day. ``params`` = (x1, x2, x3, x4); rain and pet in mm/day; NaN rain = 0."""
    x1, x2, x3, x4 = (float(v) for v in params)
    if x1 <= 0 or x3 <= 0:
        raise ValueError("x1 and x3 must be positive")
    rain = np.nan_to_num(np.asarray(rain_mm, dtype=float), nan=0.0)
    pet = np.nan_to_num(np.asarray(pet_mm, dtype=float), nan=0.0)
    if rain.shape != pet.shape:
        raise ValueError("rain and pet must have the same length")
    uh1, uh2 = unit_hydrographs(x4)
    if state is None:
        state = GR4JState.initial(params)
    if len(state.uh1) != len(uh1) or len(state.uh2) != len(uh2):
        raise ValueError("the saved state belongs to another x4")

    s, r = state.production, state.routing
    st1, st2 = list(state.uh1), list(state.uh2)
    n1, n2 = len(uh1), len(uh2)
    o1, o2 = uh1.tolist(), uh2.tolist()
    days = rain.shape[0]
    q_out = np.empty(days)
    ae_out = np.empty(days)
    f_out = np.empty(days)
    s_out = np.empty(days)
    r_out = np.empty(days)
    tanh = math.tanh

    for t in range(days):
        p, e = rain[t], pet[t]
        # 1. The rain first meets the evaporation demand; the rest goes to the soil or not.
        if p >= e:
            pn, en = p - e, 0.0
            ws = min(pn / x1, 13.0)
            th = tanh(ws)
            ps = x1 * (1.0 - (s / x1) ** 2) * th / (1.0 + s / x1 * th)
            es = 0.0
            ae = e
        else:
            pn, en = 0.0, e - p
            ws = min(en / x1, 13.0)
            th = tanh(ws)
            es = s * (2.0 - s / x1) * th / (1.0 + (1.0 - s / x1) * th)
            ps = 0.0
            ae = p + es
        s = s - es + ps
        # 2. Slow leakage from the soil.
        perc = s * (1.0 - (1.0 + (4.0 / 9.0 * s / x1) ** 4) ** -0.25)
        s -= perc
        pr = perc + (pn - ps)
        # 3. Travel time: 90 % through unit hydrograph 1, 10 % through unit hydrograph 2.
        p1, p2 = 0.9 * pr, 0.1 * pr
        for k in range(n1 - 1):
            st1[k] = st1[k + 1] + o1[k] * p1
        st1[n1 - 1] = o1[n1 - 1] * p1
        for k in range(n2 - 1):
            st2[k] = st2[k + 1] + o2[k] * p2
        st2[n2 - 1] = o2[n2 - 1] * p2
        q9, q1 = st1[0], st2[0]
        st1[0] = 0.0  # released today: no longer in transit (the next day refills this slot)
        st2[0] = 0.0
        # 4. Exchange with groundwater, routing store, direct flow.
        f = x2 * (r / x3) ** 3.5
        r_before = r
        r = max(0.0, r + q9 + f)
        f_routing = r - r_before - q9  # what the routing store really gained or lost
        qr = r * (1.0 - (1.0 + (r / x3) ** 4) ** -0.25)
        r -= qr
        qd = max(0.0, q1 + f)
        f_direct = qd - q1
        q_out[t] = qr + qd
        ae_out[t] = ae
        f_out[t] = f_routing + f_direct
        s_out[t] = s
        r_out[t] = r

    return GR4JResult(q_out, ae_out, f_out, s_out, r_out, GR4JState(s, r, st1, st2))


# ---------------------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------------------

def kge(simulated, observed) -> Dict[str, float]:
    """Kling-Gupta efficiency and its three parts, on the days where both values exist."""
    sim = np.asarray(simulated, dtype=float)
    obs = np.asarray(observed, dtype=float)
    ok = np.isfinite(sim) & np.isfinite(obs)
    sim, obs = sim[ok], obs[ok]
    if sim.size < 2 or obs.std() == 0 or obs.mean() == 0:
        return {"kge": float("nan"), "r": float("nan"), "alpha": float("nan"), "beta": float("nan"), "n": int(sim.size)}
    r = float(np.corrcoef(sim, obs)[0, 1]) if sim.std() > 0 else 0.0
    alpha = float(sim.std() / obs.std())
    beta = float(sim.mean() / obs.mean())
    value = 1.0 - math.sqrt((r - 1.0) ** 2 + (alpha - 1.0) ** 2 + (beta - 1.0) ** 2)
    return {"kge": value, "r": r, "alpha": alpha, "beta": beta, "n": int(sim.size)}


def nse(simulated, observed) -> float:
    """Nash-Sutcliffe efficiency (reported next to KGE, as hydrologists often expect it)."""
    sim = np.asarray(simulated, dtype=float)
    obs = np.asarray(observed, dtype=float)
    ok = np.isfinite(sim) & np.isfinite(obs)
    sim, obs = sim[ok], obs[ok]
    if sim.size < 2 or obs.var() == 0:
        return float("nan")
    return float(1.0 - ((sim - obs) ** 2).sum() / ((obs - obs.mean()) ** 2).sum())


# ---------------------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------------------

def dds(objective: Callable[[np.ndarray], float], lower: Sequence[float], upper: Sequence[float],
        iterations: int = 1500, perturbation: float = 0.2, seed: int = 1,
        start: Optional[Sequence[float]] = None) -> Tuple[np.ndarray, float, int]:
    """Dynamically Dimensioned Search: minimise ``objective`` inside the bounds.

    Returns (best x, best value, evaluations). Deterministic for a given seed.
    """
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    span = hi - lo
    rng = np.random.default_rng(seed)
    best = np.asarray(start, dtype=float) if start is not None else lo + rng.random(lo.size) * span
    best = np.clip(best, lo, hi)
    best_value = objective(best)
    evaluations = 1
    dims = lo.size
    for i in range(1, iterations):
        p = 1.0 - math.log(i) / math.log(iterations)
        chosen = rng.random(dims) < p
        if not chosen.any():
            chosen[rng.integers(dims)] = True
        trial = best.copy()
        step = perturbation * span * rng.standard_normal(dims)
        trial[chosen] += step[chosen]
        # Reflect at the bounds; a value still outside goes to the bound.
        low_out = trial < lo
        trial[low_out] = lo[low_out] + (lo[low_out] - trial[low_out])
        high_out = trial > hi
        trial[high_out] = hi[high_out] - (trial[high_out] - hi[high_out])
        trial = np.clip(trial, lo, hi)
        value = objective(trial)
        evaluations += 1
        if value <= best_value:
            best, best_value = trial, value
    return best, best_value, evaluations


@dataclass
class Calibration:
    """Outcome of tuning GR4J on one catchment."""

    params: Dict[str, float]
    calibration: Dict[str, float]  # KGE and parts on the tuning period
    validation: Dict[str, float]  # KGE and parts on the test period
    nse_calibration: float
    nse_validation: float
    at_bound: list  # parameter names that ended on a limit of the search


def _to_search(params: Sequence[float]) -> np.ndarray:
    """The search works on log(x1) and log(x3): store sizes matter by ratio, not by difference."""
    x1, x2, x3, x4 = params
    return np.array([math.log(x1), x2, math.log(x3), x4])


def _from_search(z: Sequence[float]) -> Tuple[float, float, float, float]:
    return (math.exp(z[0]), float(z[1]), math.exp(z[2]), float(z[3]))


def calibrate_gr4j(rain_mm, pet_mm, observed_mm, calibration_days: slice, validation_days: slice,
                   warmup_days: int = 365, iterations: int = 1500, seed: int = 1, restarts: int = 3,
                   bounds: Optional[Dict[str, Tuple[float, float]]] = None) -> Calibration:
    """Tune GR4J for the best KGE on ``calibration_days`` and score it on ``validation_days``.

    The model always runs from the first day of the series, so the ``warmup_days`` before
    each period fill its stores; those days are never scored. Observed flow is in mm/day
    over the catchment; missing values (NaN) are skipped by the score.

    The search runs ``restarts`` times (the first from typical values, the others from random
    points), each with ``iterations`` evaluations, then refines the best for half as many:
    GR4J's parameters partly compensate each other, and one search can stop on a lesser optimum.
    """
    bounds = bounds or GR4J_BOUNDS
    lower = _to_search([bounds[name][0] for name in PARAMETER_NAMES])
    upper = _to_search([bounds[name][1] for name in PARAMETER_NAMES])
    rain = np.asarray(rain_mm, dtype=float)
    pet = np.asarray(pet_mm, dtype=float)
    obs = np.asarray(observed_mm, dtype=float)
    end = calibration_days.stop
    first_scored = max(calibration_days.start or 0, warmup_days)
    scored = slice(first_scored, end)

    def objective(x: np.ndarray) -> float:
        flow = gr4j(x, rain[:end], pet[:end]).flow
        value = kge(flow[scored], obs[scored])["kge"]
        return 1.0 - value if math.isfinite(value) else 10.0

    def search_objective(z: np.ndarray) -> float:
        return objective(_from_search(z))

    best_z, best_value = None, float("inf")
    for k in range(max(1, restarts)):
        start = _to_search(GR4J_TYPICAL) if k == 0 else None
        z, value, _n = dds(search_objective, lower, upper, iterations=iterations, seed=seed + k, start=start)
        if value < best_value:
            best_z, best_value = z, value
    best_z, best_value, _n = dds(search_objective, lower, upper, iterations=max(2, iterations // 2),
                                 perturbation=0.05, seed=seed + 100, start=best_z)
    best = _from_search(best_z)
    flow = gr4j(best, rain, pet).flow
    at_bound = [name for name, v, lo, hi in zip(PARAMETER_NAMES, best_z, lower, upper)
                if abs(v - lo) <= 1e-6 * (hi - lo) or abs(v - hi) <= 1e-6 * (hi - lo)]
    return Calibration(
        params={name: float(v) for name, v in zip(PARAMETER_NAMES, best)},
        calibration=kge(flow[scored], obs[scored]),
        validation=kge(flow[validation_days], obs[validation_days]),
        nse_calibration=nse(flow[scored], obs[scored]),
        nse_validation=nse(flow[validation_days], obs[validation_days]),
        at_bound=at_bound,
    )
