"""Reading CAMELS-LUX files (LIST, v2.1, CC BY 4.0, https://doi.org/10.5281/zenodo.18776538).

The time series are given in UTC+1 all year. Only the observed river flow ``Q`` (m3/s) is
used: the twin's own ERA5-Land rain drives the model, so the model is tuned on the same rain
it will receive in operation. The reader accepts the usual spellings of the date column and
the usual missing-value markers, because only the dataset description was available when
this was written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

DATE_COLUMNS = ("date", "Date", "DATE", "time", "Time", "datetime", "timestamp", "Unnamed: 0")
MISSING = (-999, -9999, -999.0, -9999.0)


def read_daily_flow(path: Union[str, Path], column: str = "Q") -> pd.Series:
    """Daily flow (m3/s) indexed by day, from a CAMELS-LUX daily file (CSV)."""
    frame = pd.read_csv(path, sep=None, engine="python")
    date_column = next((c for c in DATE_COLUMNS if c in frame.columns), frame.columns[0])
    if column not in frame.columns:
        raise ValueError(f"no column {column!r} in {path}: found {', '.join(map(str, frame.columns))}")
    days = pd.to_datetime(frame[date_column]).dt.tz_localize(None).dt.normalize()
    flow = pd.to_numeric(frame[column], errors="coerce").replace(list(MISSING), np.nan)
    flow[flow < 0] = np.nan
    series = pd.Series(flow.to_numpy(dtype=float), index=pd.DatetimeIndex(days), name="Q")
    series = series[~series.index.duplicated(keep="first")].sort_index()
    return series
