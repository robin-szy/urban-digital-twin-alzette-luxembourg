# Precipitation and water level follow the area of interest

*Robin, 2026-11-26. For Lagloire (eddie_precip_interpolation) and Luiz (eddie_water_level).
Tasks 9.1–9.5, 10.1, 10.2 of `openspec/changes/aoi-reach-variants`.*

## Lagloire: precipitation

**What changes for you.** Your interpolation now runs over the CURRENT area of interest instead
of the fixed `aoi_alzette_35_50_300m` table. Everything AOI-related is in one new file,
`eddie_precip_interpolation/aoi_scope.py`. Your files have small edits, each marked
`AOI change`:

| File | Edit |
|---|---|
| `tasks.py` | `AOI_TABLE` gone. The three tasks read `aoi_scope.current_aoi(conn)` and use `AOI_IDW_CONFIG` (your `IDW_CONFIG` with 500 m cells). History: storage table, per-area cache check, layer published under the old name. The latest tables are stamped with their area. The live task replaces a surface of another area even without a newer reading. |
| `db.py` | The history table has 4 new columns (`reach_id`, `variant_key`, `degraded`, `degraded_reason`). The unique key is now `(reach_id, variant_key, grid_id, datetime)`. `write_interpolated_history(..., scope)` stamps the area and replaces only that area's surface for the timestamp. |

**The area.** It is the reach's *watersheds* area. No watershed layer exists yet, so it is a
5 km buffer around the reach, flagged as degraded (the banner says so). For km 35–50 that is
174 km², against 8.4 km² for the old 300 m band.

**500 m cells (your call to revisit).** At 100 m that area would be ~17,000 cells per timestamp
and ~7 GB of history per area. Robin chose 500 m: ~600 cells for km 40–48. With stations ~15 km
apart, IDW has no real detail at 100 m. Only `resolution_m` differs; power, station count and
radius are yours unchanged.

**Where the rows are.**
- `precipitation_interpolated_history_by_reach` (table) holds the history of every area.
- `precipitation_interpolated_history` (now a **view**) shows only the current area's rows. GeoServer
  publishes it as before, its TIME dimension works, and the map and catalog needed no change.
- `precipitation_interpolated_history_wms` (view, found in the database and created by no code)
  was rebuilt on the view above with its original columns (`datetime_utc`).
- Your 814,086 existing rows are kept, attributed to `alzette_035000_050000` / variant
  `aoi_alzette_35_50_300m`. They no longer show on the map, because they are a different area and
  resolution. Requesting a period computes it again for the current area.

**What follows a switch.** `derive_precipitation` (the latest grid) runs on every area switch
(about 1 s). History is computed per area when someone requests a period.
`/load-precipitation-period` (EDDIE's `blueprint.py`) reports a period as available only if it
was computed for this very area, variant and degraded state, so stand-in surfaces are recomputed
once a real watershed layer exists. The live layer (`precip_live_test`) catches up at its next
15-minute run.

**Checked (2026-11-26):** km 40–48 latest 597 cells; one day of history = 24 surfaces, 14,328
rows. The second request returned `cached`. GeoServer GetMap 200, and TIME lists only the current
area's hours. km 47–49 latest 439 cells. Your tests: 12 passed, 1 skipped.

## Luiz: water level

`AOI_TABLE` and `AOI_STATION_TOLERANCE_M` are gone from `tasks.py`. `_station_near_aoi` now
asks the AOI module for the current reach's `water_gauges` area (a 1 km buffer around the
reach). For km 35–50 it selects the same gauges as before (Pfaffenthal, Hesperange). **Pfaffenthal
and Hesperange are always loaded too**, because your `derive_water_level_interpolation` (km 35–50)
is built between them. Gauge readings are not stamped with an area: a reading belongs to its
gauge and is the same for every area (Robin's decision).
