# eddie_weather

Lagloire's backend module of the Alzette digital twin. It gives:

* **rain and temperature per sub-catchment** of the whole Alzette (the areas between the six
  CAMELS-LUX gauges on the main river), every 15 minutes for the last week and the coming
  days, every hour back to 1950, and per day, month, season and year;
* **the river flow at each gauge**, from a GR4J rainfall–runoff model tuned on the measured
  flow of CAMELS-LUX (2004–2015) and tested on the following years (2016–2021).

It works ahead of the map: scheduled tasks fetch, average, aggregate and model; the map only
reads small, ready answers. It changes nothing in the other modules. They can read its API
when they choose to.

## The four rules that keep it consistent

| Rule | How |
|---|---|
| One rain | ERA5-Land (0.1°) for the past, the same rain the model is tuned on. DWD ICON-D2/ICON for the last days and the forecast, as **provisional** values. When ERA5-Land reaches those days (about 5 days later), its **final** values replace them. A final value is never overwritten by a provisional one. |
| One set of areas | The sub-catchments cut at the CAMELS-LUX gauges (`alzette-sub-<id>`), and the whole catchment above each gauge (`alzette-gauge-<id>`). A grid cell counts for an area in proportion to the part of the area it covers. |
| One clock | Everything is stored in UTC and labelled by the **start** of its period. Days, months, seasons and years are cut at local midnight (Europe/Luxembourg). The model's days run on UTC+1 all year, like CAMELS-LUX. |
| One evaporation | Oudin (2005), from the temperature and the latitude, as in CAMELS-LUX. |

## Files

| File | What it does | Needs |
|---|---|---|
| `hydro.py` | GR4J, Oudin evaporation, KGE and NSE scores, DDS calibration | numpy |
| `catchment.py` | Sub-catchments, grid weights, area averages, levels of detail, volumes | numpy, pandas, shapely |
| `sources.py` | Open-Meteo archive (ERA5-Land) and forecast (DWD ICON), call budget | standard library |
| `alzette.py` | The six gauges, their areas, their order, the model periods | — |
| `camels.py` | Reads a CAMELS-LUX daily flow file | pandas |
| `db.py` | The four tables and every read and write | sqlalchemy |
| `pipeline.py` | Setup, backfill, catch-up, forecast, roll-up, calibration, flow, answers for the map | all the above |
| `tasks.py` | Celery tasks, their schedule, the start-up catch-up (the only file importing `eddie`) | EDDIE |
| `blueprint.py` | The data API | Flask |

## Tables (PostGIS, created on first use)

* `weather_places`: place_id, name, kind (`sub_catchment` or `gauge_catchment`), gauge_id,
  area_km2, outline (GeoJSON, plus a `geometry` column that GeoServer can publish), grid weights.
* `weather_values`: place_id, variable (`rain`, `temperature`, `flow`), level (`15min`, `hour`,
  `day`, `month`, `season`, `year`), time_utc, value, unit, quality (`provisional`, `final`),
  source, complete, updated_at.
* `weather_models`: the tuned GR4J parameters and scores of each gauge, and its saved state.
* `weather_jobs`: the backfill log, one row per year, so a restart resumes where it stopped.

About 8 million rows for the hours since 1950 (6 sub-catchments, rain and temperature); the
15-minute values are kept for a week only.

## Schedule (UTC)

| When | Task | What |
|---|---|---|
| every hour at :20 | `update_forecast` | ICON from a week ago to a week ahead (provisional) |
| every hour at :35 | `update_flow` | GR4J of the tuned gauges, final days then provisional ones |
| every hour at :45 | `backfill` | one more year of ERA5-Land (2002 onwards first, then back to 1950) while the day's budget allows |
| every day at 07:10 | `catch_up` | ERA5-Land from the last final hour to the newest day of the archive |
| when a worker starts | `catch_up` → `update_forecast` → `update_flow` | fills the gap left by a stop |

Each task holds a database lock (a second run skips instead of doubling the work) and has a
time limit. Each writes in one transaction: a task stopped half-way leaves nothing half-written.

**Call budget.** The free Open-Meteo API allows 10,000 calls a day for non-commercial use, and
a request counts once per location for each two weeks of data. A year of hourly data for about
30 grid points costs about 780 calls. The module keeps under 8,000 a day, so the whole
backfill takes about a week of background work, with 2002–2026 (what the model needs) first.

## API

| Request | Answer |
|---|---|
| `GET /weather/places[?kind=sub_catchment]` | GeoJSON of the places |
| `GET /weather/series?places=a,b&variable=rain&start=…&end=…[&level=auto]` | times, values, quality and complete flags per place; with rain, the volume in m³ (1 mm on 1 km² = 1,000 m³) |
| `GET /weather/at?time=…&variable=rain&level=hour` | one value per place for the period holding `time`, to colour the map at the timeline's time |
| `GET /weather/status` | what is stored, the backfill log, the model scores |
| `POST /weather/run/<setup\|backfill\|catch_up\|forecast\|flow\|calibrate>` | starts a step now, returns a task id |

`level=auto` picks the finest level that keeps the answer small: 15 minutes up to 3 days, hours
up to a month, days up to 2 years, months up to 15 years, then years. Gauge catchments and the
flow start at days. A seasonal total (Pegah's stormwater volumes) is
`/weather/series?variable=rain&level=season&start=2024-01-01&end=2025-01-01`.

## Setting it up (on the PC)

1. Mount the module in `EDDIE/eddie_antarctica/docker-compose.override.yml`, in the `volumes`
   of **backend**, **celery_worker** and **celery_beat** (to agree with the team first, as it is a
   shared file):

   ```yaml
         - ../../eddie_weather/eddie_weather:/venv/lib/python3.11/site-packages/eddie_weather:ro
   ```

   and, in **celery_worker** only, the CAMELS-LUX files:

   ```yaml
         - ../../data/raw/CAMELS-LUX:/app/data/camels_lux:ro
   ```

2. Restart: `docker compose up -d backend celery_worker celery_beat`.
3. Create the places from the CAMELS-LUX catchment outlines:
   `POST /weather/run/setup` with `{"path": "/app/data/camels_lux/<catchments file>.shp"}` (add `"id_field"` if the gauge-number column is not found by itself).
4. The schedule does the rest: the forecast within the hour, the archive over the next days.
   Follow it on `GET /weather/status`.
5. Once 2002–2021 is in (status shows the backfill log), tune each gauge:
   `POST /weather/run/calibrate` with `{"gauge_id": 3, "flow_csv": "/app/data/camels_lux/CAMELS-LUX/timeseries/daily/CAMELS_LUX_hydromet_timeseries_<id of gauge 3>.csv"}` (the `Date` and `Q` columns are read).
   The scores (KGE, calibration and test periods) appear in the status.

## Tests

```bash
pip install -e eddie_weather[dev]
pytest eddie_weather/tests
```

No EDDIE, Docker, network or PostGIS needed: SQLite and a stand-in for Open-Meteo. The
database lock and the PostGIS geometry column are only exercised in the real twin.

## Limits to state with the results

* ERA5-Land is modelled rain on 9–11 km cells: it gives the rain of a sub-catchment (50–380 km²),
  not of a street. ICON-D2 is finer (2 km) but sampled at the same grid points.
* GR4J is a lumped model: one flow per gauge, no flooded areas. The flooded areas stay with the
  precomputed HQ scenarios.
* The model is tuned on 2004–2015 and scored on 2016–2021, a period that includes the July 2021
  flood. The scores are reported as they come.
* Provisional values (the last ~5 days and the forecast) change until the final ones arrive.

Data: Open-Meteo (CC BY 4.0), ERA5-Land (Copernicus Climate Change Service), DWD ICON,
CAMELS-LUX v2.1 (LIST, CC BY 4.0, doi:10.5281/zenodo.18776538).
