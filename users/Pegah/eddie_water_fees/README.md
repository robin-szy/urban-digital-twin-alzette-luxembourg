# eddie_water_fees

EDDIE plugin for the two models that are not in `eddie_building_population` yet, written to
be copied out of `users/Pegah/`:

* **scenario 1, 15-minute** — `src/timeseries_model_first_scenario.py`: the gauge readings
  give a total river volume every 15 minutes, which becomes a tariff shared by the
  non-residential buildings by footprint.
* **scenario 2, seasonal rain** — `seasonal_rain.py` (Lagloire's module) +
  `src/simple_model_second_scenario.py`: the rain of a season on the roofs becomes a volume,
  shared by footprint over **all** buildings.

Both write **extra columns on the existing `building_population` rows** — no new table, no
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
   `EDDIE/eddie_antarctica/terriajs/water-level-control.js`, then rebuild Terria with a
   bumped `?v=`.

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
