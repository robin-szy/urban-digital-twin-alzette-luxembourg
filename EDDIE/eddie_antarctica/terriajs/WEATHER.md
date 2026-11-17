# Weather layer of the Alzette digital twin

`weather-control.js` adds the weather to the 3D twin, laid out like the project mockups: a bar on top
(time mode, status, date and time), **Weather on the map** on the left (layers, legend, viewpoints),
**Weather and water** on the right (headline cards and provenance), **Time navigation** docked at the
bottom (slider, play, chart), and labels pinned to the three gauges on the 3D map with the water level
and its change over 3 hours. Panels can be moved and collapsed; **Arrange panels** puts them back.
It does not change Luiz's terrain, water or buildings, nor any backend module.

Author: Lagloire. Status: first version, built before the weather ingestion module exists.

## What it shows

| Mode | On the 3D map | Cards | Chart | Source today |
|---|---|---|---|---|
| History | One day since 1950, hour by hour | Day total, wettest hour, temperature, wind, water level (from Dec 2017) | Hourly rain and Hesperange water level | Open-Meteo archive (ERA5 / ERA5-Land) |
| Now | Last 48 h and next 24 h; ASTA station values when reachable | Rain now, last 24 h, last 7 days, temperature, wind, water level and 3 h change | Rain and water level, past and forecast told apart | Open-Meteo forecast model; ASTA live; EDDIE gauges |
| Forecast | Next 16 days | Next 24 h, 7 days, 16 days, each with the ensemble's likely range | Rain with a 10–90 % band | Open-Meteo forecast and ensemble |
| 2050 | No weather field, on purpose | Change 1981–2010 → 2021–2049: rain (%), heavy-rain days, temperature, and a model check against the past | 10-year means of each model, their mean, and the reanalysis since 1950 | Open-Meteo climate (CMIP6 HighResMIP), archive |

**Show the river** loads Luiz's 3D water surface at its normal level: the median of the gauge readings
on 12 days of last year (the 15th of each month), independent of the rain and of today's readings.

**History takes a period of days.** "From" and "To" (the same day twice is one day), with shortcuts
for 1 day, 3 days, 1 week and 1 month, and "Show this period". Hourly data for the whole period is
fetched in one request, so a period is at most `historyMaxDays` (92) days; a longer one is cut and a
note says so (whole years are in the 2050 view). What adapts to the length of the period:

| Period | Step first offered | Rain bars of the chart |
|---|---|---|
| up to 3 days | 1 hour | each hour |
| 4 to 10 days | 3 hours | each hour |
| 11 to 31 days | 6 hours | mean of each 3 hours |
| 32 to 92 days | 1 day | mean of each 6 to 24 hours |

The bars widen (`barHours`) until each is at least 1.5 pixels wide; a wide bar is the mean rate of
its hours in mm/h, so the scale keeps its meaning. The step only sets how far Play jumps: the map
always shows the hour under the yellow band. The gauge line is fetched day by day, so it is drawn for
periods up to `historyWaterMaxDays` (31) days. A click on the chart goes to that time.

**The map replays the whole period** (up to `historyReplayMaxDays`, 7 days). Luiz's view files
replay one day (`date=`), but his data also takes a period (`start`, `end`): `periodView` turns the
one-day view into a view of the period, with the replay speed scaled so that a period takes about as
long as one day did. The map timeline then starts on the first day and ends on the last. The river
starts loading as soon as "Show this period" is pressed, next to the weather, not after it. A click on
the chart or the slider moves the map timeline to that time, and the panel keeps following it. A
longer period replays one day: the day under the yellow band (the river data of many days is too
heavy to build at once).

**The map timeline is on the chosen days at once.** Terria puts a view on the map only when all its
layers have loaded, and the water of a replay is built by the backend first: until then the map kept
the previous view and its timeline (today). So a replay goes in two steps that start together: a
view whose water and building layers are replaced by a timeline-only layer (inline CZML, a clock over
the days; `clockView`) is on the map within a moment, and the rain plays on it; the full view follows
when it is built, and keeps the time reached. A new choice is given to Terria at once, never queued
behind an older one; when an older load ends last and takes the map, the newest view is put back.

**The river level follows the mode.** Now and Forecast: the latest gauge reading (at most
`liveMaxAgeH` hours old, otherwise the normal level); it follows the reading at each 15-minute
refresh. Forecast keeps that level: there is no river forecast. History: the replay of the days
chosen. 2050: the normal level. Small daily changes are hardly visible in 3D; the label and the chart
carry the number.

**Forecast takes a period too:** "From" and "To" inside the 16 days of the forecast, with shortcuts.
The data is the same download, so choosing days is immediate; the summary shows the total of these
days with its ensemble range. The river has no forecast: the map keeps the river as it is.

**Casual mode** of the twin (Luiz's `twin-mode.js`): the weather panels, labels and streamlines are
hidden; they belong to the In-depth mode.

**One day on the whole twin.** In History, "Show this day" brings the map to that day as well: Luiz's
water replay of the day is loaded, Terria's timeline runs through it, and *Follow the map timeline*
is ticked, so the rain, the water and both clocks show the same moment (the panel's own Play stops:
one clock). The river view is given to Terria directly (`applyInitData`), one at a time, and the
page address is updated without waking Terria: through the address, Terria applies again every view
the address has held since the page opened, and the one whose data arrives last stays on the map (the
map on 12 November, the panel on 14 July 2021). When an older load still ends late, the day asked for
is put back (`riverKeepEveryS`, `riverKeepTries`). Before the gauge records (14 Dec 2017), or on a day without stored readings, there is no
water to replay: the river goes back to its normal level and the rain plays from the panel, and a note
under the date says so. Leaving History puts the river back to its normal level. Luiz's
options already on (3D buildings, names) are kept, and the camera is not moved. The viewpoints load the river when it is missing,
fly above the gauge first so the terrain there loads, then go down to the bank, looking at the water at
the gauge. Each step goes on as soon as the terrain is loaded (about 3 s for a place already visited,
at most about 5 s), not after a fixed wait. Livange (km 54.3) is outside the modelled water reach (km 35–50): it has a reading but no
water surface.

Map layers: precipitation, air temperature, relative humidity, solar radiation, sea-level pressure,
3D wind streamlines, and falling rain whose strength follows the hourly rain, strongest at ground level.
The viewpoint buttons put the camera at eye level on the bank at Pfaffenthal, Hesperange or Livange.

## Time steps (the teacher's questions on navigating time)

Now opens on the latest 15 minutes and refreshes every 15 minutes. Rain is shown every 15 minutes
there (Open-Meteo 15-minute data, drawn as mm/h; other variables keep the value of the nearest hour,
because only rain is given every 15 minutes); the gauges are read every 15 minutes. Further back the
step gets coarser with the data: hourly reanalysis since 1950 in History, yearly statistics in 2050.
The play step (15 minutes, 1 hour, 3 hours, 6 hours, 1 day) follows what the data has. The 24-hour and
7-day totals always come from the hourly values.

## Decisions

1. **Visualization and data are separate bricks.** Only the `PROVIDERS` object fetches data. When the
   `eddie_weather` ingestion exists, its API replaces the providers; the panel, map and chart stay.
2. **The area follows Robin's AOI selector.** The reach on Terria's workbench, else the fallback reach
   `alzette_035000_050000`. The averaging area is its `watersheds` variant when built, else `buffer`,
   `bbox`, `corridor`. A substitute is named in the panel (yellow text), never hidden. Without the AOI
   module, a box around the gauges is used and named as such.
3. **The field is smooth on purpose.** Values at up to 36 model grid points about 2.5 km apart are
   interpolated by inverse distance (power 2). The models' own cells are several km wide, so a sharper
   picture would show detail the data does not have.
4. **Area averages use the same interpolation as the map,** so the cards and the drawn field agree.
   Missing values are left out, never counted as zero.
5. **Rain of an hour is the sum of the preceding hour** (Open-Meteo convention). Bars in the chart cover
   the hour before their time.
6. **Fixed colour scales.** The same colour means the same value on every day and in every mode.
7. **Past, forecast and projection are told apart.** Forecast bars are lighter; "now" is marked; the
   2050 mode shows 30-year statistics, never daily weather.
8. **Gauge times are corrected.** The gauge collector stores local wall-clock time labelled UTC.
   `CONFIG.waterTimesAreLocal = true` converts it (with summer time). Set it to `false` once the
   collector stores real UTC, or rain and water will be shifted by 1–2 hours.

## Loading, and working next to the other modules

- **Answers are remembered during a visit.** Going back to a mode or to a day shows it at once
  instead of asking the source again (`keepNowMin` 10, `keepForecastMin` 30, `keepPastMin` 720
  minutes). The 15-minute refresh of Now still fetches fresh data.
- **The next clicks are fetched ahead.** Three seconds after the live weather is on screen, the
  forecast, its ensemble and the normal river level are fetched in the background, so Forecast and
  "Show the river" answer at once. The 2050 data is not fetched ahead: it is one large download
  (100 years, four models), kept in the browser after its first use.
- **A moving line under the top bar** shows that something is loading.
- **The panels make room for the other modules.** Terria's feature information box (Devika's flood
  damage popup opens in it) and Devika's "Popup comparison" box are drawn under the weather panels.
  A weather panel that would cover one of them steps aside while it is open and comes back when it
  closes; the time navigation becomes narrower instead of moving. The boxes to make room for are
  listed in `CONFIG.yieldTo`.
- **The side panels end above the time navigation.** On a low screen their content scrolls inside
  the panel instead of running under it.

## Wind streamlines in 3D

"Wind streamlines (3D)" replaces the arrows that were drawn on the map tiles. A streamline is the
line a particle would follow through the wind of the time shown; the lines are evenly spread across
the flow (`streamSeeds`, `traceStreamline`), coloured by speed (light blue, blue, purple) and carry
dashes that move faster in stronger wind.

- **Heights.** 10 m above the ground from the gridded weather, so the lines bend where the wind
  turns across the area. Higher up from one request at the centre of the area (`windAloft`): 120 m
  and 180 m in Now and Forecast, 100 m in History (the archive has no other height). The 10 m wind of
  each place is turned and scaled to the wind of that height. Without an answer, only 10 m is drawn
  and the legend says so.
- **Drawing.** A canvas laid over the 3D view, right above Cesium's own canvas so Terria's controls
  stay on top. Each point is a position in space (terrain height plus the height of the line)
  projected through the camera. No Cesium class is needed, and the dashes keep moving while Terria
  has paused its own drawing.
- **Limits.** Regional wind of the weather model: it follows the height of the terrain, it does not
  flow around buildings or along the valley (that would need a flow simulation). A line behind a
  hill or a building is still drawn. Only in the 3D view.

## Flood scenarios unticked in Luiz's panel

Unticking HQ10, HQ100 or HQextreme makes Luiz's panel go back to the gauge level it had applied
itself (`baseLevel`). When the river on the map came from the weather panel, or the page has just
opened, it has none, and the flood stayed on the map. The weather panel then puts the river back
(`watchScenarioBoxes`): the day replay in History, otherwise the normal level. The cleaner fix is in
`water-level-control.js`, `setScenario`: fall back to the live level when `baseLevel` is empty.

## Drawing: why every change asks Terria to draw

Terria stops drawing the 3D scene when nothing moves: camera still for a second, no terrain tile
loading, map timeline not playing (`CesiumRenderLoopPauser` in Terria's source). Cesium's own
`scene.requestRender()` does not start it again; only Terria's `notifyRepaintRequired()` does. Until
this was used, rain that started or stopped stayed invisible until the mouse moved over the map or
a water replay was playing: the rain looked late or frozen. `repaint()` now asks Terria to draw on
every change, and on every frame while it rains.

## Limits to state

- Model data, not measurements, except the ASTA stations (only in Now, only if the browser may reach
  the geoportail service) and the gauges.
- The watershed layer is not supplied yet, so the averaging area is a substitute (decision 2).
- Climate models are close to the RCP8.5 scenario and end in 2049 in this source.
- Heavy-rain days use a 20 mm/day threshold, a choice, not a standard for the Alzette.
- Ensemble model names change at Open-Meteo; `CONFIG.ensembleModels` is a list tried in order.
- The falling rain is a visual reading of the data, not a simulation of each drop. It follows the
  rain of the hour shown (under the yellow line in the chart): more, larger and faster drops as the
  rate rises (heavier rain has larger drops, which fall faster, from about 3 to 9 m/s), slanted by the
  wind blowing across the view, in three depth layers, with visibility dropping as the rate rises.
  Below 0.1 mm/h nothing falls. From the air only the far layers show, but a rainy hour always shows.

## Install on the project PC

From `EDDIE\eddie_antarctica`, once (the script must be in the image and injected into the page):

```powershell
docker compose --env-file .env --env-file api_keys.env build terria_map
docker compose --env-file .env --env-file api_keys.env up -d --no-deps terria_map
```

Reload http://localhost:3001. The Weather panel appears next to the Alzette twin panel.

After that, edits can be tried without rebuilding:

```powershell
(Get-Content terriajs\weather-control.js -Raw).Replace('$BACKEND_URL','http://localhost:5000') | Set-Content -NoNewline $env:TEMP\weather-control.js
docker compose cp $env:TEMP\weather-control.js terria_map:/app/wwwroot/weather-control.js
```

Then reload the page with Ctrl+F5. Rebuild the image before committing, so the change survives a restart.

**Remove it:** delete the `weather-control.js` line in `Dockerfile` and the injection block in
`entrypoint.sh`, then rebuild `terria_map`.

## Tests

The pure functions (interpolation, area weights, ensemble percentiles, yearly statistics, time-zone
conversion, colours) run without a browser:

```powershell
node --test terriajs\tests\weather-core.test.js
```

Checks on the map, in this order:

1. Now loads within a few seconds; the area line names the reach and the variant used. The panels sit
   between Terria's side panel and Luiz's panel; the first view is tilted over the area.
2. The rain field appears over the area with a yellow outline; Precipitation and the other layers switch.
3. "Last 24 hours" roughly equals the sum of the bars of the last 24 hours in the chart.
4. The Hesperange water level card and the gauge labels on the map match Luiz's panel.
5. Stand at Hesperange: the camera is on the bank at eye level; with rain, streaks are visible.
6. History, 14 July 2021: the wettest hours of the July 2021 floods show, with the water level rising after them.
7. Forecast: the band is wider further ahead.
8. 2050: four models load (the first time can take a minute; results are then kept in the browser).
   "Check against the past" compares models and reanalysis over 1981–2010.

## For the ingestion module later

The providers return these shapes; the future `eddie_weather` API should return the same:

| Provider | Returns |
|---|---|
| `area(reachId)` | `{ reachId, geometry, rings, bbox, variant, requested, degraded, reason }` |
| `grid(points, request)` | `{ times: [ms], values: { variable: [time][point] }, source }` |
| `ensemble(point, days)` | `{ model, times, members: [[...]], band: { p10, p50, p90, count } }` |
| `climate(point)` | `{ models: { model: { year: { total, heavy, temp } } } }` |
| `reanalysisYears(point)` | `{ year: { total, heavy, temp } }` |
| `waterDay(date)`, `waterLatest()`, `gauges()` | as Luiz's `/water-level/...` endpoints, times in real UTC |
| `stations()` | `[{ id, name, value, time, lon, lat }]` |

Data credits shown in the panel: Open-Meteo.com (CC BY 4.0), ECMWF, DWD, Météo-France, Copernicus
ERA5 / ERA5-Land, CMIP6 HighResMIP, ASTA, AGE.
