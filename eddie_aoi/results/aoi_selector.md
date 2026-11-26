# Selecting the area of interest (AOI)

*Robin, 2026-11-26. Tasks 12.2, 12.3, 12.5, 12.6, 12.7, 14.1, 14.2 of `openspec/changes/aoi-reach-variants`.*

The twin computes everything for one **area of interest**: a stretch of the **Alzette**
between two river kilometres. The river is fixed to the Alzette; only the kilometres are chosen.
Before anything is selected, the twin uses the configured default, **Alzette km 35–50**.

## How to select an area

Open the map, open the panel, then the **Area of interest** category.

- **Built areas → Switch to this area.** Picks an area that was built before. The switch itself is
  instant; the modules are then recomputed (see the banner).
- **New area → From km / To km → Build and select.** The Alzette has a kilometre marker every
  100 m, from **km 0.1 to km 68.4**, so use one decimal (e.g. 37.2). Building a new area takes a
  few seconds; it then becomes the current area like a switch.

The same can be done without the map:

```
POST /aoi/reach            {"km_start": 44, "km_end": 46}     -> 202 + task_id
GET  /digital-twin-task-status/<task_id>                      -> processing / finished / error + message
GET  /aoi/current/status.json                                 -> what each module has computed
GET  /aoi/river.json                                          -> {"river": "Alzette", "km_start": 0.1, "km_end": 68.4}
```

## What is refused (before anything is built)

The current area stays as it is, and the panel shows the reason:

| Selection | Message |
|---|---|
| another river | Only the Alzette is available; got river 'Mosel'. |
| start ≥ end | Require an increasing kilometre range: start 50.0 is not less than end 35.0. |
| outside km 0.1–68.4 | km_end 70 is outside the Alzette's kilometre markers (km 0.1-68.4). |
| between two markers | km_start 37.25 lies between two markers; the Alzette has one every 100 m (use one decimal, e.g. 37.2). |
| a km missing | Provide 'km_start'. |

## After a switch: the banner

A banner at the top of the map lists every module recomputed for the new area:

- `⏳ waiting…`: queued behind the module before it (water fees wait for the population).
- `⏳ computing… 37 s`: running, with a clock that counts up.
- `✓ ready in 52 s` + a result line (e.g. "756 buildings, about 2,094 people").
- `◐ partial` + a note: computed for part of the area only (e.g. scenario 1 of the water fees outside
  the gauges' km 35.0–54.34).
- `– none`: flood costs only. No earlier result is cached for this area, so the fresh run fills it.
- `⚠️ failed` + the reason.

The flood costs appear twice. First comes **earlier result**: the newest earlier run for the same
area, shown within seconds. Then **fresh run**: computed again, and it replaces the earlier result
when done. The banner hides 6 s after the last module finished, and clicking it hides it at once.

## Flags in the AOI browser (Explore data → AOI)

- **SUBSTITUTE**: a variant whose source layer is missing is drawn from a stand-in and says so.
  Today only **watersheds** (replaced by a 5 km buffer, so precipitation is measured over the
  buffer). The group description warns while any substitute is in use.
- **(default - no reach selected yet)**: shown on the group while nothing has been selected and
  the configured km 35–50 is in use. It disappears with the first selection and stays gone after
  a reload.

## Verified on the live stack (2026-11-26)

- **Refusals (12.2):** each case in the table above returned 400 with that message, and
  `aoi_current` was unchanged (km 40–42 before and after). The tests are in
  `tests/test_selection.py`: 13 unit tests and 6 endpoint tests that check that nothing was
  queued and that the current area was unchanged.
- **Build status (12.3):** a valid build (km 44–46) went `processing` → `finished` through
  `/digital-twin-task-status`. A build that slips past the checks (km 69–75, sent directly to the
  worker) came back `error` with "Fewer than two 'Alzette' markers between km 69.0 and 75.0."
- **Flags (12.5):** `/terria-aoi-init/current.json` for km 44–46 names the group without
  "default", marks watersheds SUBSTITUTE, and warns in the description. The "default" label
  is tested in `test_publish_integration.py::test_the_default_area_is_named_as_such_and_a_selection_is_not`.
- **Default area (14.1):** the fallback `alzette_035000_050000` is pinned. `resolve()` gives
  building_population, flood_costs and buildings → corridor on that reach, not degraded, and
  precipitation → buffer instead of watersheds, degraded. Every AOI layer's provenance names the reach.
- **Second area (14.2):** the switch to km 44–46 recomputed population (756 buildings, 53 s),
  water fees (13 s) and flood depths + costs (127 buildings, 53 s). The new rows carry
  `alzette_044000_046000`/`corridor_500`; the rows for km 40–42 (1,258) and every other built area
  are still stored; `building_population_4326` shows only km 44–46. A reload opens on km 44–46
  (`current.json`, status route).
- **Found and fixed during 14.2:** the flood-cost map layer showed only 10 of km 44–46's 127
  buildings. It was still cut to the 500 m corridor (the 8.10 stand-in), while Devika's study area is
  that corridor widened by the flood extents. The layer now shows a run of the current area whole,
  and cuts it only while the loaded run belongs to another area (`cut_cost_view_to_current_reach`).
  Popups and totals were not affected.
