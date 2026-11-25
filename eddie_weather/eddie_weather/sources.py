"""Where the weather comes from: the Open-Meteo archive (ERA5, ERA5-Land) and forecast (DWD ICON).

One rain for the whole twin:

* **Final rain** comes from ERA5 (0.25 degree, about 28 km, hourly, 1940 to about 5 days
  ago): the same reanalysis rain as the ``tp`` column of CAMELS-LUX, so the river model is
  tuned on the rain it receives in operation. (Open-Meteo's ERA5-Land has no precipitation:
  it returns ``null``; checked on the twin on 24 November 2026.)
* **Final temperature** comes from ERA5-Land (0.1 degree, about 11 km), for the evaporation.
* **Provisional values** cover the last days and the coming ones: DWD ICON-D2 (2 km, native
  15-minute values, 2 days ahead) and the ICON models behind it up to 7 days. When the
  reanalysis reaches those days, its values replace them.

All times are requested and returned in UTC (``timezone=GMT``); local days are cut later.
Only the standard library is used for the web requests; ``fetch`` can be replaced in tests.

Limits of the free API (non-commercial, CC BY 4.0 attribution to Open-Meteo): 600 calls a
minute, 5,000 an hour, 10,000 a day. A request counts as one call per location for each two
weeks of data (and each 10 variables). A year costs about 26 calls per grid cell: about
30 ERA5-Land cells for the temperature and about 10 ERA5 cells for the rain, so about
1,000 calls. :class:`CallBudget` keeps the backfill inside these limits.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/dwd-icon"
# variable -> (Open-Meteo archive model, its grid step in degrees, source label)
ARCHIVE_MODELS: Dict[str, Tuple[str, float, str]] = {
    "rain": ("era5", 0.25, "ERA5 (Open-Meteo archive)"),
    "temperature": ("era5_land", 0.1, "ERA5-Land (Open-Meteo archive)"),
}
ARCHIVE_SOURCES: Dict[str, str] = {name: label for name, (_, _, label) in ARCHIVE_MODELS.items()}
FORECAST_SOURCE = "DWD ICON-D2 / ICON (Open-Meteo)"
ARCHIVE_LAG_DAYS = 5
ARCHIVE_FIRST_DAY = dt.date(1950, 1, 1)

# variable in the twin -> (Open-Meteo name, unit)
VARIABLES: Dict[str, Tuple[str, str]] = {
    "rain": ("precipitation", "mm"),
    "temperature": ("temperature_2m", "degC"),
}

# Variables that are sums over the step before their time label (shifted to the start).
SUMS = {"rain"}

# The heaviest single request sent (weighted calls), well under the 600 a minute allowed.
MAX_REQUEST_COST = 100.0

Fetcher = Callable[[str, float], object]
Point = Tuple[float, float]  # latitude, longitude


class SourceError(RuntimeError):
    """The weather service did not give a usable answer."""


def _fetch_json(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "eddie-weather (Alzette digital twin, student project)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class CallBudget:
    """Counts the weighted API calls and waits, or stops, before a limit is reached.

    ``per_day`` is kept below the service's 10,000 so the live updates (forecast, catch-up)
    always have room left when a long backfill runs.
    """

    per_minute: float = 500.0
    per_hour: float = 4000.0
    per_day: float = 8000.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    spent: List[Tuple[float, float]] = field(default_factory=list)

    @staticmethod
    def cost(locations: int, days: float, variables: int) -> float:
        return locations * max(1.0, days / 14.0) * max(1.0, variables / 10.0)

    def _used(self, now: float, window: float) -> float:
        return sum(c for t, c in self.spent if now - t < window)

    def left_today(self) -> float:
        return self.per_day - self._used(self.clock(), 86400.0)

    def take(self, cost: float, wait: bool = True) -> bool:
        """Book ``cost`` calls. Waits for the minute or hour window; refuses past the day's.

        A request must cost less than the minute's limit: :func:`archive_hourly` splits long
        periods into months for that reason.
        """
        if cost > min(self.per_minute, self.per_hour):
            raise ValueError(f"one request of {cost:.0f} calls is above the per-minute limit: split it")
        now = self.clock()
        self.spent = [(t, c) for t, c in self.spent if now - t < 86400.0]
        if self._used(now, 86400.0) + cost > self.per_day:
            return False
        for window, limit in ((60.0, self.per_minute), (3600.0, self.per_hour)):
            while self._used(self.clock(), window) + cost > limit:
                if not wait:
                    return False
                recent = [t for t, _ in self.spent if self.clock() - t < window]
                if not recent:
                    break
                self.sleep(max(1.0, window - (self.clock() - min(recent))))
        self.spent.append((self.clock(), cost))
        return True


def _query(base: str, params: Dict[str, str]) -> str:
    return base + "?" + urllib.parse.urlencode(params, safe=",")


def _coords(points: Sequence[Point]) -> Dict[str, str]:
    return {"latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
            "longitude": ",".join(f"{lon:.4f}" for _, lon in points)}


RETRY_PAUSES = (5.0, 15.0, 30.0, 60.0)  # seconds between tries: dropped connections are common


def _get(url: str, fetch: Optional[Fetcher], timeout: float, tries: int = len(RETRY_PAUSES) + 1,
         pause: Optional[float] = None, sleep: Optional[Callable[[float], None]] = None) -> List[dict]:
    sleep = sleep or time.sleep
    last = None
    for attempt in range(tries):
        try:
            answer = (fetch or _fetch_json)(url, timeout)
            if isinstance(answer, dict) and answer.get("error"):
                raise SourceError(str(answer.get("reason", "error from the weather service")))
            return answer if isinstance(answer, list) else [answer]
        except urllib.error.HTTPError as error:
            last = error
            if error.code == 429 and attempt == tries - 1:
                raise SourceError("call limit of the weather service reached (budget): retry later") from error
            if error.code not in (429, 500, 502, 503, 504):
                raise SourceError(f"weather service refused the request: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
        if attempt < tries - 1:
            sleep(pause * (attempt + 1) if pause is not None else RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)])
    raise SourceError(f"weather service unreachable after {tries} tries: {last}")


def _frames(locations: List[dict], block: str, variables: Sequence[str], n_points: int) -> Dict[str, pd.DataFrame]:
    """Answer of the service -> one table per variable, times (UTC) x points."""
    if len(locations) != n_points:
        raise SourceError(f"expected {n_points} locations, got {len(locations)}")
    out = {}
    times = None
    for name in variables:
        api_name = VARIABLES[name][0]
        columns = []
        for location in locations:
            data = location.get(block) or {}
            if times is None:
                times = pd.to_datetime(data.get("time", []), utc=True)
            values = data.get(api_name)
            if values is None:
                raise SourceError(f"{api_name} missing from the answer")
            if len(values) != len(times):
                raise SourceError("the locations do not have the same times")
            columns.append(np.array([np.nan if v is None else float(v) for v in values]))
        out[name] = pd.DataFrame(np.column_stack(columns) if columns else np.empty((0, 0)), index=times)
    return out


def archive_hourly(points: Sequence[Point], start: dt.date, end: dt.date,
                   variables: Sequence[str] = ("rain", "temperature"), fetch: Optional[Fetcher] = None,
                   budget: Optional[CallBudget] = None, timeout: float = 120.0,
                   today: Optional[dt.date] = None) -> Dict[str, pd.DataFrame]:
    """Hourly reanalysis values (UTC) for the points, ``start`` to ``end`` included.

    Rain comes from ERA5 and temperature from ERA5-Land (see :data:`ARCHIVE_MODELS`). Each
    point takes the value of the model cell it lies in: the points are snapped to the model's
    grid and each grid cell is asked for once. The hourly precipitation is the rain of the
    hour *ending* at the time given; the tables are labelled by the start of that hour, as
    everywhere in the twin. Raises :class:`SourceError` if the daily budget is spent.
    """
    latest = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=ARCHIVE_LAG_DAYS)
    start, end = max(start, ARCHIVE_FIRST_DAY), min(end, latest)
    if end < start:
        return {name: pd.DataFrame() for name in variables}
    by_model: Dict[Tuple[str, float], List[str]] = {}
    for name in variables:
        model, step, _ = ARCHIVE_MODELS[name]
        by_model.setdefault((model, step), []).append(name)
    out: Dict[str, pd.DataFrame] = {}
    for (model, step), names in by_model.items():
        cells, which = snap_to_grid(points, step)
        frames = _archive_cells(cells, start, end, model, names, fetch, budget, timeout)
        for name in names:
            frame = frames[name].iloc[:, which]
            frame.columns = range(len(points))
            out[name] = _label_by_start(frame, 60, name)
    return out


def snap_to_grid(points: Sequence[Point], step: float) -> Tuple[List[Point], List[int]]:
    """The model cells holding the points (each once), and the cell of each point."""
    cells: List[Point] = []
    which: List[int] = []
    for lat, lon in points:
        cell = (round(round(lat / step) * step, 4), round(round(lon / step) * step, 4))
        if cell not in cells:
            cells.append(cell)
        which.append(cells.index(cell))
    return cells, which


def _archive_cells(cells: Sequence[Point], start: dt.date, end: dt.date, model: str, variables: Sequence[str],
                   fetch: Optional[Fetcher], budget: Optional[CallBudget], timeout: float) -> Dict[str, pd.DataFrame]:
    # One request per calendar month and group of cells, so that no request is heavier than
    # about 100 calls (a year for 30 points would be 780, above the 600 a minute allowed).
    group = max(1, int(MAX_REQUEST_COST // CallBudget.cost(1, 31, len(variables))))
    pieces: Dict[str, List[pd.DataFrame]] = {name: [] for name in variables}
    for first, last in split_months(start, end):
        days = (last - first).days + 1
        columns: Dict[str, List[pd.DataFrame]] = {name: [] for name in variables}
        for g in range(0, len(cells), group):
            chunk = cells[g:g + group]
            if budget and not budget.take(budget.cost(len(chunk), days, len(variables))):
                raise SourceError("daily call budget spent: the rest waits for tomorrow")
            params = dict(_coords(chunk), start_date=first.isoformat(), end_date=last.isoformat(),
                          hourly=",".join(VARIABLES[v][0] for v in variables), models=model,
                          timezone="GMT", cell_selection="nearest")
            frames = _frames(_get(_query(ARCHIVE_URL, params), fetch, timeout), "hourly", variables, len(chunk))
            for name in variables:
                columns[name].append(frames[name])
        for name in variables:
            joined = pd.concat(columns[name], axis=1)
            joined.columns = range(len(cells))
            pieces[name].append(joined)
    return {name: pd.concat(pieces[name]).sort_index() for name in variables}


def forecast(points: Sequence[Point], past_days: int = 2, forecast_days: int = 7,
             variables: Sequence[str] = ("rain", "temperature"), fetch: Optional[Fetcher] = None,
             budget: Optional[CallBudget] = None, timeout: float = 60.0) -> Dict[str, Dict[str, pd.DataFrame]]:
    """Recent and coming values from DWD ICON: ``{"15min": {...}, "hour": {...}}``.

    The 15-minute values (ICON-D2, native in Central Europe) cover the past days and about
    two days ahead; the hourly ones go on to ``forecast_days``.
    """
    days = past_days + forecast_days
    if budget and not budget.take(budget.cost(len(points), days, 2 * len(variables)), wait=True):
        raise SourceError("daily call budget spent")
    names = ",".join(VARIABLES[v][0] for v in variables)
    params = dict(_coords(points), minutely_15=names, hourly=names, timezone="GMT",
                  past_days=str(past_days), forecast_days=str(forecast_days), cell_selection="nearest")
    locations = _get(_query(FORECAST_URL, params), fetch, timeout)
    fine = _frames(locations, "minutely_15", variables, len(points))
    coarse = _frames(locations, "hourly", variables, len(points))
    return {"15min": {k: _label_by_start(v, 15, k) for k, v in fine.items()},
            "hour": {k: _label_by_start(v, 60, k) for k, v in coarse.items()}}


def _label_by_start(frame: pd.DataFrame, minutes: int, variable: str) -> pd.DataFrame:
    """Open-Meteo labels a sum by the end of its interval; the twin labels periods by their start.

    Only the sums (rain) are shifted; for an instant value (temperature) the end label is
    kept, which is at most one step off for a mean over a long period.
    """
    if frame.empty or variable not in SUMS:
        return frame
    shifted = frame.copy()
    shifted.index = frame.index - pd.Timedelta(minutes=minutes)
    return shifted


def split_months(start: dt.date, end: dt.date) -> List[Tuple[dt.date, dt.date]]:
    """Calendar-month pieces of a period: the unit of one archive request."""
    pieces = []
    first = start
    while first <= end:
        following = dt.date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        last = min(end, following - dt.timedelta(days=1))
        pieces.append((first, last))
        first = following
    return pieces


def split_years(start: dt.date, end: dt.date) -> List[Tuple[dt.date, dt.date]]:
    """Calendar-year chunks of a period: the unit of the backfill and of its log."""
    chunks = []
    year = start.year
    while year <= end.year:
        first = max(start, dt.date(year, 1, 1))
        last = min(end, dt.date(year, 12, 31))
        chunks.append((first, last))
        year += 1
    return chunks
