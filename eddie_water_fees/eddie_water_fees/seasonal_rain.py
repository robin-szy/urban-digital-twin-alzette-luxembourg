"""Seasonal rain over the Alzette reach, and the volume of water it brings onto an area.

Written by Lagloire (weather layer of the Alzette digital twin) for Pegah's
stormwater scenario. It gives the rain of a season in mm and turns it into a volume
for any area, for example the roofs of the buildings:

    volume (m3) = rain (mm) x area (m2) / 1000        (1 mm on 1 m2 = 1 litre)

Two sources:

* ``seasonal_rain_mm(year, season)``: the rain of one season of one year, from the
  ERA5 / ERA5-Land reanalysis (Open-Meteo archive API, free, no key), averaged over
  three points along the Alzette between Pfaffenthal and Hesperange. Available from
  1950 to about five days ago. Needs internet.
* ``normal_rain_mm(season)``: the 30-year average 1991-2020 measured at
  Luxembourg/Findel (MeteoLux climate normals). Works offline.

Seasons are the meteorological ones: winter = December to February, spring = March
to May, summer = June to August, autumn = September to November. Winter 2024 is
December 2023 to February 2024.

Limits to state with the numbers:

* This is the rain that falls, not what runs off into drains or the river.
* The reanalysis is modelled rain with a grid of about 9 km: it gives the rain of
  the region, not of one street.
* The normals come from one station (Findel, about 5 km east of the river).

Only the Python standard library is used. Example::

    from seasonal_rain import seasonal_rain_mm, rain_volume_m3
    summer = seasonal_rain_mm(2024, "summer")          # {"rain_mm": ..., ...}
    roofs_m2 = buildings.geometry.area.sum()           # in metres, e.g. EPSG:2169
    volume = rain_volume_m3(summer["rain_mm"], roofs_m2)

From the command line::

    python seasonal_rain.py --year 2024 --area-m2 150000
    python seasonal_rain.py --normals --area-m2 150000
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIME_ZONE = "Europe/Luxembourg"

# Three points along the Alzette reach (latitude, longitude): Pfaffenthal, between,
# Hesperange. The rain of a season is the mean of their totals.
DEFAULT_POINTS: Tuple[Tuple[float, float], ...] = (
    (49.620, 6.132),
    (49.596, 6.145),
    (49.572, 6.158),
)

# The reanalysis archive is complete up to about this many days before today.
ARCHIVE_LAG_DAYS = 5

SEASONS: Dict[str, Tuple[int, int, int]] = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "autumn": (9, 10, 11),
}

# Monthly rain, mm, Luxembourg/Findel, climate normals 1991-2020 (MeteoLux,
# NM_RR06_06), January to December. Source:
# https://data.public.lu/en/datasets/monthly-climate-normal-parameters-luxembourg-findel-airport-wmo-id-06590/
NORMALS_1991_2020_MM: Tuple[float, ...] = (
    71.5, 59.5, 56.6, 49.4, 73.3, 73.0, 72.1, 71.9, 66.6, 76.2, 71.7, 89.5,
)

Fetcher = Callable[[str, float], dict]


def _check_season(season: str) -> str:
    key = season.strip().lower()
    if key not in SEASONS:
        raise ValueError(f"unknown season {season!r}: use one of {', '.join(SEASONS)}")
    return key


def season_dates(year: int, season: str) -> Tuple[dt.date, dt.date]:
    """First and last day of a season. Winter ``year`` starts in December of ``year - 1``."""
    key = _check_season(season)
    first_month, _, last_month = SEASONS[key]
    first_year = year - 1 if key == "winter" else year
    start = dt.date(first_year, first_month, 1)
    end = dt.date(year, last_month, calendar.monthrange(year, last_month)[1])
    return start, end


def normal_rain_mm(season: str) -> float:
    """30-year average rain of a season (1991-2020, Findel), in mm. ``"year"`` gives the year."""
    key = season.strip().lower()
    if key == "year":
        return round(sum(NORMALS_1991_2020_MM), 1)
    months = SEASONS[_check_season(key)]
    return round(sum(NORMALS_1991_2020_MM[m - 1] for m in months), 1)


def rain_volume_m3(rain_mm: float, area_m2: float) -> float:
    """Volume of water (m3) that ``rain_mm`` of rain brings onto ``area_m2`` square metres."""
    if rain_mm < 0 or area_m2 < 0:
        raise ValueError("rain and area must not be negative")
    return rain_mm * area_m2 / 1000.0


def _fetch_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def seasonal_rain_mm(
    year: int,
    season: str,
    points: Sequence[Tuple[float, float]] = DEFAULT_POINTS,
    fetch: Optional[Fetcher] = None,
    timeout: float = 60.0,
    today: Optional[dt.date] = None,
) -> dict:
    """Rain of one season of one year over the reach, from the reanalysis archive.

    Returns a dict: ``season``, ``year``, ``start``, ``end`` (ISO dates), ``rain_mm``
    (mean of the points' season totals, rounded to 0.1 mm), ``days``, ``missing_days``
    (days without a value at any point; their rain is counted as 0), ``points`` and
    ``source``. Raises ``ValueError`` for a season that has not ended in the archive.
    ``fetch`` (url, timeout) -> dict can replace the web request, for tests.
    """
    key = _check_season(season)
    start, end = season_dates(year, key)
    if start < dt.date(1950, 1, 1):
        raise ValueError("the archive starts on 1950-01-01")
    latest = (today or dt.date.today()) - dt.timedelta(days=ARCHIVE_LAG_DAYS)
    if end > latest:
        raise ValueError(
            f"{key} {year} ends on {end.isoformat()}: the archive is complete up to about "
            f"{latest.isoformat()}. Choose an earlier season, or use normal_rain_mm()."
        )
    if not points:
        raise ValueError("at least one point is needed")
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "precipitation_sum",
        "timezone": TIME_ZONE,
    }
    url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params, safe=",")
    answer = (fetch or _fetch_json)(url, timeout)
    locations = answer if isinstance(answer, list) else [answer]
    if len(locations) != len(points):
        raise ValueError(f"expected {len(points)} locations from the archive, got {len(locations)}")

    days = (end - start).days + 1
    totals: List[float] = []
    missing = set()
    for location in locations:
        daily = location.get("daily") or {}
        values = daily.get("precipitation_sum") or []
        if len(values) != days:
            raise ValueError(f"expected {days} days from the archive, got {len(values)}")
        total = 0.0
        for index, value in enumerate(values):
            if value is None:
                missing.add(index)
            else:
                total += float(value)
        totals.append(total)

    return {
        "season": key,
        "year": year,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rain_mm": round(sum(totals) / len(totals), 1),
        "days": days,
        "missing_days": len(missing),
        "points": [list(p) for p in points],
        "source": "ERA5 / ERA5-Land reanalysis (Open-Meteo archive API), mean of the points",
    }


def seasonal_table(
    area_m2: float,
    year: Optional[int] = None,
    fetch: Optional[Fetcher] = None,
    today: Optional[dt.date] = None,
) -> List[dict]:
    """The four seasons (and the year of the normals) with their rain and volume on ``area_m2``.

    With ``year``: the seasons of that year from the archive; a season not yet in the
    archive is left out. Without ``year``: the 1991-2020 normals, plus the whole year.
    """
    rows = []
    if year is None:
        for key in list(SEASONS) + ["year"]:
            mm = normal_rain_mm(key)
            rows.append({"season": key, "rain_mm": mm, "volume_m3": round(rain_volume_m3(mm, area_m2), 1),
                         "source": "normal 1991-2020, Findel"})
        return rows
    for key in SEASONS:
        try:
            rain = seasonal_rain_mm(year, key, fetch=fetch, today=today)
        except ValueError:
            continue
        rows.append({"season": key, "rain_mm": rain["rain_mm"],
                     "volume_m3": round(rain_volume_m3(rain["rain_mm"], area_m2), 1),
                     "start": rain["start"], "end": rain["end"], "source": "reanalysis"})
    return rows


def _main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Seasonal rain over the Alzette reach and its volume on an area.")
    parser.add_argument("--year", type=int, help="year of the seasons (winter = Dec of the year before to Feb)")
    parser.add_argument("--normals", action="store_true", help="use the 1991-2020 normals (offline)")
    parser.add_argument("--area-m2", type=float, default=1000.0,
                        help="area that receives the rain, in m2 (default 1000: the volume then equals the mm)")
    args = parser.parse_args(argv)
    if args.year is None and not args.normals:
        parser.error("give --year YEAR or --normals")
    try:
        rows = seasonal_table(args.area_m2, None if args.normals else args.year)
    except OSError as error:
        raise SystemExit(f"Could not reach the weather archive ({error}). Check the internet "
                         "connection, or use --normals for the 1991-2020 averages.")
    title = "normals 1991-2020 (Findel)" if args.normals else f"{args.year} (reanalysis, Alzette reach)"
    print(f"Rain and volume on {args.area_m2:,.0f} m2, {title}")
    print(f"{'season':<8} {'rain (mm)':>10} {'volume (m3)':>14}")
    for row in rows:
        print(f"{row['season']:<8} {row['rain_mm']:>10.1f} {row['volume_m3']:>14,.1f}")
    if not rows:
        print("No season of this year is complete in the archive yet.")


if __name__ == "__main__":
    _main()
