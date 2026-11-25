"""Stand-ins shared by the tests: nested catchments and a fake Open-Meteo service."""

import urllib.parse

import numpy as np
import pandas as pd
import datetime as dt
from shapely.geometry import box

from eddie_weather import alzette

TODAY = dt.date(2026, 11, 20)


def nested_catchments():
    """Six nested boxes, west to east, each gauge's catchment containing the one upstream."""
    return {g.camels_id: box(5.95, 49.45, 6.05 + 0.08 * i, 49.75) for i, g in enumerate(alzette.GAUGES)}


def rain_at(times: pd.DatetimeIndex, point_index: int) -> np.ndarray:
    """Deterministic showers: the same hour always gets the same rain, whatever the request."""
    hours = (times.tz_convert("UTC").tz_localize(None) - pd.Timestamp("2000-01-01")) // pd.Timedelta(hours=1)
    hours = np.asarray(hours, dtype=np.int64)
    wet = ((hours * 2654435761) % 1000) < 120
    return np.where(wet, 0.4 + (hours % 7) * 0.3, 0.0) * (1.0 + 0.1 * point_index)


def temperature_at(times):
    doy = np.asarray(times.dayofyear)
    return 9.0 - 7.0 * np.cos(2 * np.pi * (doy - 15) / 365.0)


class FakeOpenMeteo:
    """Answers archive and forecast requests like the service: lists of locations, GMT times."""

    def __init__(self, now=pd.Timestamp("2026-11-20 10:00", tz="UTC"), forecast_bias=1.0):
        self.calls = []
        self.now = now
        self.forecast_bias = forecast_bias

    def __call__(self, url, timeout):
        self.calls.append(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        lats = q["latitude"].split(",")
        answer = []
        if "archive" in url:
            times = pd.date_range(q["start_date"], pd.Timestamp(q["end_date"]) + pd.Timedelta(hours=23), freq="h", tz="UTC")
            blocks = {"hourly": (times, 1.0)}
        else:
            day = self.now.floor("D")
            first = day - pd.Timedelta(days=int(q["past_days"]))
            last = day + pd.Timedelta(days=int(q["forecast_days"]))
            blocks = {"hourly": (pd.date_range(first, last, freq="h", tz="UTC", inclusive="left"), self.forecast_bias),
                      "minutely_15": (pd.date_range(first, last, freq="15min", tz="UTC", inclusive="left"),
                                      self.forecast_bias / 4.0)}
        for i in range(len(lats)):
            location = {}
            for block, (times, scale) in blocks.items():
                # The service labels a sum by the END of its interval.
                step = pd.Timedelta(hours=1) if block == "hourly" else pd.Timedelta(minutes=15)
                location[block] = {
                    "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
                    "precipitation": list(np.round(rain_at(times - step, i) * scale, 3)),
                    "temperature_2m": list(np.round(temperature_at(times), 2)),
                }
            answer.append(location)
        return answer if len(answer) > 1 else answer[0]
