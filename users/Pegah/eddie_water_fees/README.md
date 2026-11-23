# eddie_water_fees

EDDIE plugin for the five water-fee scenarios Pegah runs in `users/Pegah/main.py`, written to
be copied out of `users/Pegah/`:

* **scenario 1, 15-minute** — `src/timeseries_model_first_scenario.py`: the gauge readings
  give a total river volume every 15 minutes, which becomes a tariff shared by the
  non-residential buildings by footprint.
* **scenario 2, seasonal rain** — `seasonal_rain.py` (Lagloire's module) +
  `src/simple_model_second_scenario.py`: the rain of a season on the roofs becomes a volume,
  shared by footprint over **all** buildings.
* **scenario 3, flood-risk proximity levy** — `src/simple_model_third_scenario.py`: the
  closer a building is to the water, the higher the levy, `150 € × (1 − distance / 50 m)`
  inside 50 m and 0 beyond.
* **scenario 4, water-quality surcharge** — `src/simple_model_fourth_scenario.py`:
  `footprint × pollution multiplier × 2.5 €/m²`, the multiplier from OSM (3.0 industrial,
  1.0 residential, 2.0 for commercial / blank / unclassified).
* **scenario 5, water consumption** — `src/simple_model_fifth_scenario.py`: 160 l per person
  for residential buildings and `footprint × floors × 0.80 × 160/3 l` for the
  non-residential ones, at 0.005 €/l.

All five write **extra columns on the existing `building_population` rows** — no new table, no
new GeoServer layer. The map layer `input_layers:building_population_4326` (tick
**Water fees (model result)** under *River*) therefore shows the new values on the same
buildings, and the panel reads the totals back from `/water-fees/summary.json`.

## What it writes

| column | meaning |
|---|---|
| `water_area_share_15min` | share of the non-residential footprint (0 for residential / unclassified) |
| `allocated_water_volume_15min_m3` | mean pool of the window × share |
| `water_fee_15min_mean_eur` | mean tariff of the window × share — **the fee** |
| `water_fee_15min_min_eur`, `water_fee_15min_max_eur` | cheapest / dearest interval × share |
| `tariff_mean_eur_15min`, `tariff_intervals_15min` | the window's tariff and how many intervals priced |
| `gauge_quality_15min` | weakest interval label: `observed` / `interpolated` / `carried` / `missing` |
| `gauge_window_start_utc`, `gauge_window_end_utc` | the window actually computed |
| `rainwater_area_share` | share of the total footprint, over all buildings |
| `allocated_rainwater_volume_m3`, `rainwater_cost_eur` | the rain on that building's roof, and its fee |
| `rain_mm_season`, `rain_volume_m3` | the season's rain and the volume on all the roofs |
| `rainwater_unit_price_eur_per_m3`, `rain_season`, `rain_source` | the parameters of the run |
| `distance_to_river_m` | shortest distance from the footprint to the mapped watercourse |
| `flood_risk_fee_eur` | **scenario 3's levy**: 150 € at 0 m, falling to 0 at 50 m |
| `pollution_multiplier` | scenario 4's OSM runoff factor (3.0 / 2.0 / 1.0) |
| `water_quality_charge_eur` | **scenario 4's surcharge**: footprint × multiplier × 2.5 €/m² |
| `water_consumption_liters`, `water_consumption_m3` | scenario 5's water the building consumes |
| `water_consumption_cost_eur` | **scenario 5's cost** at 0.005 €/l |
| `water_fees_computed_utc` | when this task last wrote the scope |

The existing pool-model columns (`area_share`, `allocated_water_volume`, `total_cost`) are
left alone: the two models sit side by side, so the old numbers stay comparable.

## Install (three steps, all outside `users/Pegah/`)

1. **Copy the package** into the repo root as `eddie_water_fees/eddie_water_fees/` (the
   folder next to `eddie_building_population/`), i.e. copy the inner
   `eddie_water_fees/eddie_water_fees/` of this folder to `<repo>/eddie_water_fees/eddie_water_fees/`.
2. **Mount it** — snippet `snippets/01-docker-compose-mount.md`:
   one line in each of the `backend` and `celery_worker` services of
   `EDDIE/eddie_antarctica/docker-compose.override.yml`, then
   `docker compose up -d --no-build --no-deps backend celery_worker`.
3. **Add the button** — snippet `snippets/02-water-level-control.js.md`: one block in
   `EDDIE/eddie_antarctica/terriajs/water-level-control.js` (the panel *and* the one line that
   loads the click popup), then rebuild Terria with a bumped `?v=`.

Nothing else is needed: EDDIE discovers packages named `eddie_*` in `site-packages`, imports
`tasks.py` in the worker and `blueprint.py` in the backend. `blueprint.py` and `tasks.py` are
both present because the plugin contract requires both.

## Run it

In the map: **Water-fee model** panel → *15-minute tariff and rain* → **Run 15-minute
tariff + rain**. Choose the gauge window (UTC, empty = the last 7 days), the share and the
tariff, the rain season and year, and tick *Use 1991-2020 normals* to stay offline.

Without the interface:

```powershell
curl.exe -s -X POST http://localhost:5000/water-fees/run -H "Content-Type: application/json" -d "{\"gauge_start\": \"2026-11-20T14:00\", \"gauge_end\": \"2026-11-20T18:00\"}"
curl.exe -s http://localhost:5000/water-fees/summary.json
```

Or straight in the worker, which is the quickest way to see the log:

```powershell
docker exec -w /app celery_worker_digital_twin /venv/bin/python -c "from eddie.tasks import app; print(app.send_task('eddie_water_fees.tasks.compute_water_fees', kwargs={'gauge_start': '2026-11-20T14:00', 'gauge_end': '2026-11-20T18:00'}).get(timeout=500))"
```

## Click a building: the prices of the five scenarios

Clicking a building in the 3D view opens Terria's **Feature Information** card; this plugin puts a
**Water fee** section at the top of that card with **scenarios 1 to 5** and **their sum**
for the clicked building — the prices only, as asked. It is served entirely by this plugin:

| route | what it returns |
|---|---|
| `GET /water-fees/building/<building_id>.json` | one building's five `scenarios` (cost, volume, how it was computed), their `total_eur`, its scope and the run time |
| `GET /water-fees/popup.js` | `static/water_fees_popup.js`, the card section itself |

The id is the map's (`ACT_…`, the 3D buildings' `buildingid`) or the table's
(`Building2D.ACT_…`, `gml_id` / `localid`); both are accepted. The lookup takes the row with
the newest `water_fees_computed_utc` across scopes, so a building shows its last run's prices
whichever scope holds them.

```powershell
curl.exe -s http://localhost:5000/water-fees/building/ACT_3e3e3aaf-17fa-4dd2-9dca-4405b04fc25e.json
```

`total_eur` is the sum of all five scenarios, so a building's card is a single figure for the
whole fee model, not one per scenario.

The script is loaded by the fee block in `water-level-control.js` (one `<script>` tag pointing at
`/water-fees/popup.js`). It adds its own `<li>` as the first child of the card's
`.tjs-feature-info-panel__body` list and puts it back if Terria re-renders that list, so **no
other popup, panel or layer is touched**: the flood-cost popup, the Terria feature-info template
and `eddie_buildings` stay as their authors wrote them, and the HQ10/HQ100 content of the card
keeps rendering below the prices. A building without a result for the run says so instead of
showing numbers, and deselecting the building removes the section.

## Check the model without EDDIE (host)

From this folder, with the repository's raw data in place:

```powershell
python -m eddie_water_fees.gauge_timeseries --start 2026-11-20T14:00 --end 2026-11-20T18:00
```

It prints the three section areas, the interval count and the mean tariff, using
`data/raw/River_kilometers/kilometrage-des-cours-deau.gpkg` and the OSM extract. Expected for
that window on the current extract: Pfaffenthal 159 462 m², Hesperange 195 124 m², Livange
0 m², 17 intervals, mean tariff 179 209.30 EUR — the same numbers `users/Pegah/main.py`
writes.

## What to say about the numbers

**Scenario 1 (15-minute tariff)**

* It is a levy on the volume in the river, not a bill for water consumed: nothing measures
  what a building uses.
* The mapped river surface is treated as a fixed area and the gauge stage as its effective
  thickness, so the volume is an *indicator*, not a surveyed discharge.
* Livange has no mapped water polygon in km 49.845–50 (0 m²), so the total is effectively
  Pfaffenthal + Hesperange.
* A missing gauge is interpolated between the others at that timestamp; if no gauge reports,
  the previous 15-minute value is carried forward. `gauge_quality_15min` says how weak the
  window was, so a stale stretch is visible instead of being averaged silently.
* Timestamps are treated as UTC, as the collector stores them; around a clock change a
  period boundary can shift by an hour.

**Scenario 2 (rain)**

* This is the rain that falls, not the runoff into drains or the river — no runoff
  coefficient, so it is an upper bound.
* The reanalysis is modelled rain on a grid of about 9 km: the region's rain, not one
  street's.
* The 1991–2020 normals come from one station (Findel, about 5 km east of the river).

**Scenario 3 (flood-risk proximity levy)**

* The distance is measured to the nearest mapped watercourse around the reach
  (`gis_osm_waterways_free`, classes river / stream / canal / drain), not to one chosen
  line, so a building on a side stream is charged like one on the Alzette.
* The levy is the source model's: 150 € at 0 m, falling linearly to 0 € at 50 m.

**Scenario 4 (water-quality surcharge)**

* The pollution multiplier comes from the OSM `type` tag of the building's footprint
  (3.0 industrial, 1.0 residential, 2.0 for commercial and for buildings without an OSM
  match), exactly as `simple_model_fourth_scenario.py` classifies it.
* It is a charge on the footprint (2.5 €/m² × multiplier), so it dominates the total of the
  five scenarios: it is a levy on sealed area, not an annual water bill.

**Scenario 5 (water consumption)**

* The source model's residential test is kept verbatim — `"residential" in building_type` —
  and that string is also contained in `"non-residential"`, so every building takes the
  160 l per person branch and a building without population pays 0 €. The floor-area branch
  (footprint × floors × 0.80 × 160/3 l) is dead code in `main.py` too; change the test if the
  floor-area charge is what was meant.
* A positive population below 1 counts as one person, as in the source model.

## Traps

* **Rows are matched on `ctid`, not on `gml_id`.** The km 35-50 scope holds one building twice
  (14,684 rows, 14,683 distinct `gml_id`), so an id join would either fail or write the same fee
  to two rows. The task reads `ctid` with the rows and updates by it.
* **A population run resets these columns.** `eddie_building_population` deletes and rewrites
  its scope's rows, so run this task again after it. The columns stay, filled with NULL for
  the rewritten rows.
* **Scope.** The task writes the newest computed scope in `building_population` unless
  `reach_id` / `variant_key` are given, matching the scope the panel reports. Rows of other
  scopes keep their old values.
* **The layer draws every scope in the table.** The `_4326` view has no scope filter, so
  overlapping scopes draw the same building twice. Look at the panel's scope, not the map,
  when numbers disagree.
* **GeoServer's attribute list.** The task rebuilds `<table>_4326` and re-registers it, so the
  drawing updates at once; GeoServer still caches the attribute list of the existing feature
  type, so WFS / identify may not list the new columns until the feature type is reloaded in
  GeoServer.
* **Colour.** The WMS layer uses GeoServer's default polygon style, so the new columns are
  drawn as footprints, not as a graduated fee. Colouring by `water_fee_15min_mean_eur` needs
  an SLD uploaded to GeoServer and a `styles` parameter on the catalog item in
  `eddie_water_level/blueprint.py` — ask and it can be added.
* **GeoServer keeps a layer's bounding box** from when the layer was created; after a run for
  a different reach the layer may need its bounds recalculated in the GeoServer UI.
* `setup_environment.get_database()` is autocommit, so the write here uses
  `eddie_base_data.ingest.transactional` and checks that it updated as many rows as it staged.
