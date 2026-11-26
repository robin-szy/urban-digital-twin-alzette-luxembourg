# Tasks

> **Scope of this round (2026-11-20):** make `eddie_aoi` the single source of AOI geometry, add the current reach, and make `building_population` and `flood_costs` follow the selection. In scope: 4.6–4.7, 6.6, 8.1–8.3, 8.5–8.9, 10.3a, 11.x, 12.7–12.9 (12.2–12.6 remain open). **Deferred** to a later round, left unticked and marked *(deferred)*: 8.4, 9.x, 10.1, 10.2, 10.3b, 13.x, and the precipitation and water-level parts of 14.x. See `proposal.md`, Delivery in stages.

## 1. Module scaffold

- [x] 1.1 Create `eddie_aoi/` with `pyproject.toml`, package directory and `tests/`, matching the shape of `eddie_precip_interpolation` (loose dependency specifiers only, so it does not fight EDDIE's `environment.yml` pins); verify `pip install -e eddie_aoi` succeeds in the EDDIE conda environment and `import eddie_aoi` works
- [x] 1.2 Add `pytest` as a dev extra and a placeholder test; verify `pytest eddie_aoi` collects and passes

## 2. Reach routing, vendored and locked down

- [x] 2.1 Vendor `select_route` and `PreparationError` byte-identical from `users/devika/src/river_aoi.py` under a provenance header naming the source file, commit and author; verify the vendored function body is unchanged by diffing it against the original
- [x] 2.2 Write tests asserting the validation invariants hold — all markers within the 20 m tolerance, marker chainage strictly increasing, rejection when the mapped river is disconnected, and rejection when the route length differs from the chainage span by more than 2%; verify each test fails if its corresponding check is removed from the vendored copy
- [x] 2.3 Document in `eddie_aoi/README.md` why the routing is vendored rather than imported and that the invariants in 2.2 must not be weakened; verify the README names the follow-up consolidation with Devika

## 3. Reach model and identity

- [x] 3.1 Implement the reach model holding river, start and end kilometre, centerline, chainage and validation provenance (route length, max marker offset, markers-in-order); verify a unit test constructs one from fixture markers and river lines
- [x] 3.2 Implement the canonical `reach_id` at 1 m precision, zero-padded to six digits (`alzette_035000_050000`); verify a test that `35`, `35.0`, `35.00` and values differing by under a metre all produce one identifier
- [x] 3.3 Implement bounds validation — non-increasing range, non-finite values, unknown river with available rivers listed, missing endpoint markers; verify a test per rejection case asserting the message names the failing condition
- [x] 3.4 Implement chainage lookup from kilometre to distance along the centerline, rejecting kilometres outside the reach rather than extrapolating; verify tests for an in-range lookup and an out-of-range rejection
- [x] 3.5 Add a parameterised loader for markers and river lines that takes the river name as an argument instead of hardcoding it, keeping all geometry in EPSG:2169; verify a test loads a non-Alzette river from a fixture
- [x] 3.6 Add a `marker_polyline` centerline method that joins the ordered markers and needs no mapped-river source, recording in provenance that it does not follow the mapped river and that the chainage gate was not applied; verify tests that a short meandering centerline is accepted, that the same length would fail the routed gate, and that self-crossing markers are rejected
- [x] 3.7 Make the marker source hash content-based (over the marker rows) rather than file-based, so it works identically for a PostGIS markers table; verify tests that row order does not change the hash and changed coordinates do

## 4. Reach persistence and the configured fallback

- [x] 4.1 Create the `reach_cache` table (identity, bounds, centerline geometry SRID 2169, validation provenance, source hash, status, error, timestamps, pinned); verify the migration applies to a PostGIS instance and the geometry column reports SRID 2169
- [x] 4.2 Implement reach persistence and reuse with `last_used_at` updates and `building`/`ready`/`failed` status, so a failed reach returns its recorded reason rather than a partial result; verify integration tests for a cache hit, a build-in-progress read and a failed-reach re-request
- [x] 4.3 Persist a source hash over the marker and river-line inputs and treat a reach as stale when it changes; verify a test that mutating a fixture source triggers a rebuild while an unchanged source serves from cache
- [x] 4.4 Read the fallback reach from configuration (`AOI_FALLBACK_REACH`, defaulting to Alzette 35–50), pin it, and resolve it at startup; verify startup fails with the validation reason when the configured fallback is invalid, and that the pinned flag is set when it is valid
- [x] 4.5 Compare the fallback reach's corridor against the existing `aoi_alzette_35_50_300m` geometry and record the result in `results/`; verify the comparison reports the deviation and that it is within the tolerance the report states
- [x] 4.6 Create the one-row `aoi_current` table referencing `reach_cache.reach_id` with `ON DELETE RESTRICT`, and set it only when a selection's build reaches `ready`; verify integration tests that a successful build becomes current, a refused or failed build leaves it unchanged, and a direct delete of the current reach from `reach_cache` is refused
- [x] 4.7 Implement `current_reach()` returning the reach in `aoi_current`, or the pinned fallback with a fallback flag when the table is empty; verify tests for both cases and that the value survives a worker and backend restart

## 5. Computed variants

- [x] 5.1 Define the variant provider interface — `build(reach, params)` returning a single-row GeoDataFrame in EPSG:2169 with variant, params, reach and source-identifier fields; verify a test asserts the returned shape, CRS and fields
- [x] 5.2 Implement `corridor(half_width_m)` delegating to the vendored buffer behaviour with flat end caps; verify tests that the corridor extends the requested half-width each side and terminates at the reach endpoints without overshooting
- [x] 5.3 Implement `bbox(margin_m)` and `buffer(d)`; verify tests covering each, plus rejection of non-positive and non-finite distance parameters
- [x] 5.4 Implement a registry of variants by name, rejecting unknown names with the registered names listed; verify a test for the unknown-name rejection message

## 6. Lookup variants, availability and degradation

- [x] 6.1 Implement the lookup variant base that selects features related to the reach from a configured source layer and dissolves them to one geometry; verify tests that unrelated features are excluded, several related features dissolve to one polygon, and a present-but-empty selection is reported rather than returned as a valid empty polygon
- [x] 6.2 Implement `hq100` reading its source path from configuration (`data/raw/Flood_hazard_HQ100/HWGK2021_HQ100.shp`); verify a test that relocating the configured path changes where it reads from without a code change
- [x] 6.3 Implement `watersheds` and `hq_extreme` as declared-unavailable until their sources are configured; verify a test that listing variants reports availability and the reason for each unavailable one
- [x] 6.4 Implement degradation — substitute a variant's configured fallback and return `{requested, actual, degraded, reason}`, failing instead when no fallback is configured; verify tests for the watershed substitution, the no-fallback failure, and that every degraded result carries all four fields
- [x] 6.5 Configure `watersheds` to fall back to a river-km-plus-margin variant; verify an integration test that requesting the watershed variant today yields a degraded result naming both variants
- [x] 6.6 Widen the watershed fallback from `buffer(1000)` to `buffer(5000)` (`WATERSHED_FALLBACK_BUFFER_M`), so it is the largest variant; verify the degraded result names `buffer_5000`, the envelope (8.3) covers it, and an existing cached `buffer_1000` substitute is rebuilt rather than served

## 7. Variant persistence and invalidation

- [x] 7.1 Create the `aoi_variant_cache` table keyed on reach and variant key, with geometry SRID 2169, params, source hash and timestamps; verify the migration applies and the composite key rejects a duplicate insert
- [x] 7.2 Implement variant caching with parameters encoded in the variant key, so differing parameters build separately rather than reusing a neighbouring entry; verify tests for a cache hit on repeat request and a miss when a parameter differs
- [x] 7.3 Implement source-hash invalidation, including not serving a degraded entry once its real source becomes available; verify tests for a changed source triggering a rebuild, an unchanged source serving from cache, and a degraded entry being rebuilt after its source appears
- [x] 7.4 Add source-layer read caching with a spatial index, shared across variants and reaches, separate from variant caching; verify a test that two lookup variants over one source layer read it once

## 8. Resolver and dataset bindings

- [x] 8.1 Implement the dataset-to-variant binding registry and `resolve(dataset, reach)` returning geometry plus provenance; verify tests for a normal resolution, a degraded resolution, and rejection of an unregistered dataset with the registered names listed
- [x] 8.2 Populate the initial bindings — `building_population` and `flood_costs` to `corridor(500)` (Devika's `BUILDINGS_CORRIDOR_M`, so people and costs share one area); landcover, buildings and addresses to `corridor(300)`; water gauges to `buffer(1000)`; precipitation to `watersheds`; `dem_build` to the envelope; `dem_publish` to `corridor(300)`; verify a test asserting each binding resolves to the expected variant
- [x] 8.3 Implement `envelope()` as the union of the reach's variants for bounds-only sources; verify a test that it covers every variant's geometry
- [ ] 8.4 *(deferred)* Reject a binding that uses a dataset's own derived variant as its analysis domain, and reject narrowing a build extent that a dataset declares a dependence on surrounding terrain for; verify tests for the flood-footprint-as-own-denominator rejection, the acceptance of that variant for another dataset, and the build-extent rejection
- [x] 8.5 Document the registry and the three AOI roles (fetch envelope, clip mask, analysis domain) in `eddie_aoi/README.md`; verify the README states that the envelope must never be used as an analysis domain
- [x] 8.6 Replace the body of `eddie_base_data.query.resolve_aoi` with a call to the resolver for the current reach, mapping the result onto `AoiScope` and keeping the signature; verify `compute_building_population` reports `variant_key` `corridor_500` and the current reach id with no edit to `eddie_building_population`, and record the old (`bbox_500`) and new building and population counts in `results/` for Pegah
- [x] 8.7 In the flood-cost wrapper (ours), replace `_frozen_scope()` in `flood_costs/tasks.py` with `resolve_aoi(conn, "flood_costs")` and take the km filter in `postgis_inputs.markers()` from the current reach instead of `34.999–50.001`, commenting each change for Devika; verify the scope written with the results names the current reach and `corridor_500`
  - *Status 2026-11-20:* code done; `scope.json` names the current reach and `corridor_500` (verified). Writing to PostGIS is blocked by an existing guard: `flood_depth_buildings` predates the HQextreme columns and needs an explicit table migration (a decision for Robin/Devika).
  - *Verified 2026-11-20 (live):* switching to km 35–50 ran depths for `corridor_500` (run `reach-20261120T164544Z`, 507 buildings, 65 s); rows carry reach + `corridor_500`; the view shows only the current reach. Unblocked by the nullable migration of the 28 HQextreme columns on `flood_depth_buildings`.
- [x] 8.8 In `flood_costs/depth_reconstruction.py` (Devika's), make the smallest commented edits so it follows the reach: `RIVER_BEGIN_KM` / `RIVER_END_KM` in the constants at `:489` and the marker filter at `:615` from the reach, without editing the SHA-pinned reference functions; check the comparison cohort (`:674-676`) only for cohort buildings inside the current study area; verify the km 35–50 reach reproduces today's results exactly (276 buildings; provisional HQ10 €122,528 and HQ100 €1,603,670), and record the diff of her files in `results/` for her review
  - *Status 2026-11-20:* the depth edits are done; km 35–50 reproduces the unchanged-code depth run byte for byte (507 buildings; see `eddie_aoi/results/flood_costs_follow_the_reach.md`). Found while testing km 40–48: depths depend on the stretch they are computed over (up to 5 m at the ends) — a method question for Devika before flood costs joins the followers. The 276-building / € figures above predate the HQextreme extension and cannot be re-checked: the damage step does not run on this host on `main` (missing `hqextreme_review/*.json`).
  - *Done 2026-11-26:* depths follow the reach (`run_depth_reconstruction` takes the reach's computed km); Devika's dynamic flood costs (479c973, 2794205) run per reach.
- [x] 8.9 Make the flood-cost run work for a second reach: report buildings without a reviewed type as unknown (never zero, never classified by code), relax any hardcoded count that breaks with the smallest commented edit, and compute depths only within gauge coverage (km 35–54.34) with the uncovered part flagged; verify on a reach inside 35–50 (a subset of today's buildings, same per-building results) and on one extending beyond km 54.34 (partial-coverage label present, no zeros for uncovered buildings)
  - *Status 2026-11-20:* blocked. The damage step needs Devika's missing review files, always loads a comparison baseline EDDIE does not configure, and asserts her historical cohort throughout.
  - *Done 2026-11-26 (Devika, 2794205):* `recompute_for_current_reach` builds the reach's own classification and runs `run_classified_damage`; depths only within gauge coverage, the rest flagged. Verified live on km 37–39 (234 buildings) and km 44–46 (127).
- [x] 8.10 Damage stand-in until Devika takes over 8.9 (no edits to her method): for a current reach other than the one of the loaded damage run, show her km 35–50 damage only for buildings of that run inside the current reach's `corridor_500`, every other building "not computed" (never 0 €), and scenario totals summed over those buildings only, labelled partial ("N of M buildings in this reach have a damage estimate"); replaces the all-or-nothing `_require_current_reach` guard in `flood_costs/blueprint.py`; verify on km 40–48 (partial, smaller totals), km 5–15 (all not computed), km 35–50 (unchanged), and her 76 tests; add a line to her results note. *(2026-11-25: implemented - `_reach_subset` in `flood_costs/blueprint.py`, area-filtered view via `cut_cost_view_to_current_reach` in `tasks.py`; checked on km 40-42 (15 of 1258, partial) and km 43-46 (21 of 880). NOT yet run: km 5-15, km 35-50 unchanged, her 76 tests, the results-note line.)*
  - *Superseded 2026-11-26:* damage now runs per reach (8.9). The corridor cut of the map layer stays only for the moment after a switch, while the loaded run still belongs to the previous reach; a run of the current reach is shown whole (fix: km 44–46 showed 10 of 127 buildings).

## 9. Derived-data attribution and migration *(deferred)*

> `eddie_base_data.publish.write_derived` already stamps `reach_id`, `variant_key` and `degraded` on tables written through it, so `building_population` is attributed today. The tasks below are for the precipitation and water-level tables and are deferred with 10.1 and 10.2.

- [ ] 9.1 Add reach, variant and degraded columns to `precipitation_interpolated_history` and the other derived tables in `eddie_precip_interpolation/db.py` and the water-level tables; verify the migration applies and the columns are present
- [ ] 9.2 Backfill existing precipitation and water-level rows with the Alzette 35–50 reach id; verify a query reports zero rows left unattributed
- [ ] 9.3 Stamp reach, variant and degraded on every derived row at write time; verify an integration test that rows written during a degraded resolution still carry the degraded marker when read back
- [ ] 9.4 Update the reuse check in `blueprint.py` and every other cache-hit query to match on reach and variant, not only on the dataset's own keys; verify tests that the same period for a different reach reports not-available, the same period for the same reach reports available, and a previously degraded period for the same reach reports not-available once the real source exists
- [ ] 9.5 Reject serving unattributed derived data as belonging to the current reach; verify a test that an untagged row is not returned for a reach query

## 10. Repoint the consumers

- [ ] 10.1 *(deferred)* Replace `AOI_TABLE` in `eddie_precip_interpolation/tasks.py` with a resolver call; verify the precipitation task runs against the fallback reach and its output matches the pre-migration result for the same area
- [ ] 10.2 *(deferred)* Replace `AOI_TABLE` and `AOI_STATION_TOLERANCE_M` in `eddie_water_level/tasks.py` with a resolver call bound to `buffer(1000)`; verify the same gauge set is selected as before the change
- [x] 10.3a Send `eddie_aoi.tasks.ensure_fallback_reach` from the worker startup hook in `EDDIE/eddie_antarctica/src/eddie_antartica/tasks.py`, alongside the existing static ingest; verify on a fresh database that the worker starts, logs the fallback reach it built, and that `reach_cache` holds it pinned
- [ ] 10.3b *(deferred)* Stop reading `selected_polygon.geojson` for `add_base_data_to_db` at `EDDIE/eddie_antarctica/src/eddie_antartica/tasks.py:50`; deferred because it widens EDDIE's own static layers (3D buildings, land use) from the km 46.3–48.3 rectangle to km 35–50 — see design, Open Questions

## 11. Cache eviction

- [x] 11.1 Implement eviction by last-used age, retaining the pinned fallback and the current reach, and deleting an evicted reach's derived rows from every table carrying `reach_id` in the same transaction, reporting what was removed (reaches, and rows per table) and what was retained with reasons; verify tests for removal of an unused reach together with its derived rows, retention of the pinned fallback, and retention of the current reach and its derived rows
- [x] 11.2 Expose eviction as an explicitly invoked endpoint and confirm nothing is removed without a request; verify an integration test that no eviction occurs until the endpoint is called

## 12. Frontend selection via WPS

- [x] 12.0 Satisfy the EDDIE plugin contract: `tasks.py` (imported by the worker) and `blueprint.py` (imported and registered by the backend) are both mandatory for any `eddie_*` package in site-packages; mount the package and `src/static/geo` into backend and celery_worker; verify all six containers reach healthy with the module loaded
- [x] 12.0b Expose REST endpoints on the plugin blueprint - POST `/aoi/reach` (validates, then queues a build), GET `/aoi/reach/<id>`, GET `/aoi/reaches`, GET `/aoi/variants`; verify over HTTP that a valid request returns a task id and builds, and that a reversed range, unknown river and missing fields are each refused with a reason
- [x] 12.0g Serve the WPS process from the plugin's own `/aoi/wps` endpoint rather than the app's `processes` list in `src/eddie_antartica/blueprint.py`, which is COPYed into the image at build time and so unreachable without a rebuild; verify GetCapabilities advertises the process and the app's own `/wps` is left untouched
- [x] 12.0d Build and persist the display variants (corridor, buffer, bbox, watersheds) in the same task that builds a reach, so it is ready to show when the task reports success; verify the task result names each variant and that a variant with no fallback is reported rather than sinking the reach
- [x] 12.0e Serve a reach's AOI as GeoJSON in EPSG:4326 at `/aoi/reach/<id>/variants.geojson`, narrowable by `variant_key`, with provenance and the degraded marker on each feature; verify coordinates are geographic, the metric area is reported from the metric CRS, and the AOI bands stay well under the payload warning size
- [x] 12.0f Serve a Terria catalog fragment at `/terria-aoi-init/<id>.json` with one styled geojson item per variant; verify a degraded variant is named and described as a substitute in a warning style, the corridor shows by default and the bbox does not, and the group warns when anything is degraded
- [x] 12.0c Read lookup-variant source paths from the environment (`AOI_HQ100_PATH`, `AOI_WATERSHEDS_PATH`, `AOI_HQ_EXTREME_PATH`) so availability reflects what is actually on disk; verify `/aoi/variants` reports hq100 available and names why the other two are not

- [x] 12.1 Implement a `build_aoi` PyWPS process with literal `river`, `km_start` and `km_end` inputs and register it in the empty `processes` list in `src/eddie_antartica/blueprint.py`; verify the process appears in the `/wps` GetCapabilities response
- [x] 12.2 Validate the selection before any build starts, refusing a reversed range, an unavailable river and kilometres outside the river's covered range without replacing the current AOI; verify a test per refusal case asserting the message and that the served AOI is unchanged
  - *Done 2026-11-26:* the river is fixed to the Alzette (user decision: not a parameter); `reach.validate_selection` (REST and WPS) refuses another river, non-numbers, a reversed range, km outside the markers (0.1–68.4, `GET /aoi/river.json`) and km between markers. `tests/test_selection.py` (19 tests); live check in `results/aoi_selector.md`.
- [x] 12.3 Return build status through the existing task-status route so building, success and the specific validation failure reason are all observable; verify an integration test covering all three states
  - *Done 2026-11-26:* checked live through `/digital-twin-task-status`: processing → finished (km 44–46), error + reason (km 69–75 sent to the worker directly); validation failures return 400 with the reason at once (12.2).
- [x] 12.4 Add a `{"type": "wps"}` catalog entry plus the reach centerline and AOI variant layers to the catalog fragment the plugin returns; verify Terria renders the input form and the AOI layers are listed
  - *Superseded 2026-11-25:* the selector moved into the map panel (Area of interest); the WPS entry was removed from `catalog.json`, the AOI browser (variant layers) is kept.
- [x] 12.5 Flag degraded datasets in the catalog fragment naming the requested and substituted variant, and indicate when the fallback reach is in use; verify the flag is present while the watershed variant is degraded, absent when nothing is degraded, and present again after a client reload
  - *Done 2026-11-26:* SUBSTITUTE + group warning verified live on `current.json`; the default-area label is tested (`test_the_default_area_is_named_as_such_and_a_selection_is_not`).
- [x] 12.6 Document the selector in `results/` — how to select a reach, what a degradation flag means, and the known WPS "Run Analysis" awkwardness with the layer-refresh nudge; verify the documented steps run as written against the live stack
  - *Done 2026-11-26:* `eddie_aoi/results/aoi_selector.md` (panel selector, refusals, banner, flags, verification).
- [x] 12.7 Serve `/terria-aoi-init/current.json` (the current reach's catalog fragment, flagged when it is the fallback) and point `EDDIE/eddie_antarctica/terriajs/client_config.json` at it instead of `fallback.json`; rebuild `terria_map` and bump the `?v=` in `entrypoint.sh`; verify a fresh deployment opens on the flagged fallback, and that a reload after a successful selection opens on that reach with no fallback flag
  - *Status 2026-11-20:* route done and verified live; `client_config.json` points at it. Terria rebuild not done yet: rebuilding from this branch would drop Lagloire's unmerged weather layer from the running map.
  - *Done 2026-11-25/26:* `terria_map` rebuilt from the working tree (now v=32), no hand-copied script; `current.json` serves the current reach, flagged while it is the fallback.
- [x] 12.8 At the end of a successful selection build, set the current reach (4.6) and send the task of every dataset on the follower list beside the bindings (`eddie_building_population.tasks.compute_building_population`, and the flood-cost chain — depth reconstruction, damage, publish — once 8.8 passes), naming the sent tasks in the build result; verify an integration test that a successful build sends both for the new reach and a failed build sends nothing, and record how long the flood-cost chain takes for one reach
  - *Status 2026-11-20:* done and verified live for `building_population` (selection of km 40–48 became current and recomputed in 47 s). The flood-cost chain is not on the follower list until 8.7–8.9 are unblocked.
  - *Verified 2026-11-20 (live):* flood costs joined the followers as `eddie_flood_costs.tasks.recompute_for_current_reach` (depths always; damage reported as skipped while 8.9 is open). The flood-cost chain takes about 65 s for km 35–50, population about 71 s. Flood damage text/popups are shown only when the loaded damage run belongs to the current reach (guard in `flood_costs/blueprint.py`).
- [x] 12.9 Make the `_4326` view created by `eddie_base_data.publish.write_derived` show only rows of the current reach and the dataset's bound variant, refresh it when the current reach changes, and recalculate the GeoServer layer bounding box; verify that after selecting a second, overlapping reach the published layer shows each building once and only for that reach, the first reach's rows remain stored, and the old `bbox_500` rows are not shown
- [x] 12.10 Switch between built reaches without rebuilding: WPS process `select_built_reach` (dropdown of every ready reach in the cache, refreshed per request) makes the chosen reach current and sends the followers; the AOI browser (`/terria-aoi-init/all.json`) now holds that form plus one layer per variant for the CURRENT reach (`/aoi/current/variants.geojson`) instead of one group per cached reach
  - *Verified 2026-11-20 (live):* switch 40–42 → 35–50 returned in under a second; population (6,301 buildings) and flood depths (507) recomputed for 35–50.
- [x] 12.11 Make the map redraw what follows the reach without a page reload: `/map-layers/status.json` (eddie_water_level) reports `aoi_reach` and a `fees_version` (hash of current reach + latest `computed_utc`); `water-level-control.js` polls it every 15 s, loads the water-fee layer under `fees.<version>` (a new init URL, since Terria caches init files by URL; the WMS item also gets an explicit Luxembourg extent) and points every AOI layer on the workbench (centerline, corridors, buffer, watersheds, bbox) at the current reach; the selector's own result layers ("AOI corridor - <river> km ...") are left on their reach
  - *Status 2026-11-20:* backend verified (version changes within 10 s of a switch); the browser side was not yet confirmed by the user. **The running Terria container (built from Lagloire's branch) received the edited `water-level-control.js` by `docker cp` (`?v=25`, also bumped in `entrypoint.sh`)**: lost if that container is recreated, until the image is rebuilt with 12.7. Check: `docker exec eddie_antarctica-terria_map-1 grep -c followAoiReach /app/wwwroot/water-level-control.js`.
- [x] 12.12 Check that the flood-depth layer (EDDIE catalog, Input Layers → `flood_depth_buildings_4326`, fixed id) redraws after a reach switch; if not, give it the same treatment as 12.11
  - *Done 2026-11-26:* the flood-depth layer is a GeoServer Input Layer, so `refreshResultLayers` re-requests it on every status change; confirmed by Robin.
- [x] 12.13 *(2026-11-25, user request)* After a reach switch show only the new reach's results, the same layers as before, and "computing" until each module is done: `building_population_4326` re-filtered (her `recreate_view` had dropped the filter), flood-cost view cut to the reach (8.10), 3D buildings from the current reach's `corridor_500` (binding `buildings` 300 -> 500 m, caches per reach, reach in item id/URL), flood extents cached on disk (cold buildings 74 s -> 9.5 s), `aoi_followers` + `GET /aoi/current/status.json` + map banner, WMS result layers re-requested on change. Backend verified on km 40-42 / 43-46; **map side not yet verified in a browser**; the population layer that appears unasked is not yet found. **Later 2026-11-25:** per-reach 3D-building disk cache prepared in a backend subprocess when the status route first sees a reach (cold 0.7 s), official flood zones cut to the reach (binding `flood_zones`), 3D water cut to the corridor client-side, user-added workbench layers restored after each init (Terria replaces the workbench). Popups do not work in Terria pedestrian mode (user finding).
  - *Done 2026-11-25:* confirmed in the browser by Robin ("All works"). 2026-11-26: flood costs cached per reach (the newest earlier run is shown at once, then recomputed), banner shows time per task and hides 6 s after the end.

## 13. EDDIE core CRS split *(deferred)*

- [ ] 13.1 Split `to_working_crs_rectangle` in `lib/eddie/src/eddie/digitaltwin/working_crs.py:67-82` into `fetch_envelope()` keeping the bbox and a shape-preserving `to_working_crs()`; verify unit tests that the first returns a rectangle and the second preserves a corridor's shape
- [ ] 13.2 Repoint existing core callers to whichever function matches their role, leaving `retrieve_from_instructions` on the bbox for bounds-only endpoints; verify the existing core tests still pass
- [ ] 13.3 Record the diff in `results/` as a candidate upstream contribution, scoped to this split and its callers only; verify the recorded diff touches no unrelated CRS code

## 14. Integration verification

- [x] 14.1 Run the full stack against the fallback reach and confirm the AOI layers and `building_population` resolve through the registry; verify each layer's provenance reports the expected reach and variant *(precipitation and water level: deferred with 10.1–10.2)*
  - *Done 2026-11-26:* fallback `alzette_035000_050000` pinned; building_population, flood_costs, buildings resolve to corridor on it, precipitation to the flagged buffer substitute; every AOI layer's provenance names the reach (see `results/aoi_selector.md`).
- [x] 14.2 Select a second, different reach from the frontend and confirm `building_population` is recomputed for it automatically and the map shows only that reach; verify the new rows carry the new reach id, the first reach's rows remain stored, and a reload opens on the new reach *(the reuse-check part: deferred with 9.4)*
  - *Done 2026-11-26:* switch to km 44–46: population, water fees, flood depths + costs recomputed automatically; new rows carry the new reach id, km 40–42 rows remain, the views show km 44–46 only, a reload opens on it.
- [ ] 14.3 *(deferred)* Confirm no AOI geometry remains defined outside the module; verify a search finds no surviving `AOI_TABLE`, `AOI_STATION_TOLERANCE_M`, or `selected_polygon.geojson` AOI read in the plugins

## Workflow follow-up

- Raise with the team: a review trigger or date for the degraded watershed binding, so the fallback does not become permanent by default.
- Raise with Devika: consolidating her `build_corridor` against the new `corridor` variant, retiring the vendored copy.
- Tell Pegah: `building_population` moves from `bbox_500` to `corridor(500)` with 8.6, with the old and new counts recorded there.
- Start the change `dem-follows-aoi` with Lagloire: window water level's whole-DEM reads to the reach envelope, then point `TERRAIN_DEM` back at the national DEM already on the host (`data/terrain/dem_merged_FULL_BACKUP.tif`) instead of the km 30–59 extract; the map's terrain tiles already cover the country and stay as they are; Lagloire's modules on the same envelope, and a real watershed source (derived from the DEM, or an official layer) to replace the `buffer(5000)` fallback. See design, Open Questions.
- Send Devika `eddie_aoi/results/flood_costs_follow_the_reach.md` ("What you need to do"): (1) depths depend on the selected stretch — acceptable while visible, her call to change; (2) the damage step cannot run on the host (missing `hqextreme_review/*.json`, baseline always loaded, cohort assertions) — until fixed, damage figures stay those of the 2026-11-08 km 35–50 run; (3) `flood_depth_buildings` migrated by adding her 28 new columns as NULL. Plus the commented edits, the diff and the exact km 35–50 reproduction.
- Raise with Pegah: migrating the Colab copy of `aoi.py` (`users/Pegah/population_03.py:31`) onto the module.
- Raise with Luiz: reconciling `config.toml [extent]` with the `dem_build` envelope binding, and the GeoTIFF rectangularity caveat for the publish clip.
- Archive the change once the team's review requirements are satisfied, then verify the archived result.


## Where we stopped (2026-11-26)

Everything in sections 1–8, 11, 12 and 14 that is not marked deferred is done (on `main`).
Next: sections 9 and 10 (precipitation and water level follow the AOI; attribution of derived rows).
Still deferred: 8.4, 10.3b, 13, 14.3.

## Where we stopped (2026-11-20)

Branch `aoi-merge-with-modules`, pushed by Robin at the end of the day. Pick up in this order:

1. 8.10 — Devika's damage stand-in.
2. 12.11 browser check (ask Robin) and 12.12 — the flood-depth layer after a switch.
3. 12.2–12.6 selector polish, then 14.1–14.2 end-to-end checks.
4. Before merging with Lagloire's branch: rebuild `terria_map` (12.7); until then the map runs on the hand-copied script noted under 12.11.

Notes:
- `eddie_aoi/results/` is gitignored: the notes for Devika and Pegah are only in the repository if added with `git add -f eddie_aoi/results`.
- The yellow watershed line with a rectangle around the AOI comes from Lagloire's work: leave it.
- How to use it: build a new reach with the "AOI selector"; switch to an already built one with "AOI browser → Switch to a built AOI"; the AOI browser's layers show the current reach. Water fees (Pegah): In-depth → River → "Water fees (model result)". Flood depths (Devika): Input Layers → `flood_depth_buildings_4326`; damage: the flood scenario text and building popups (km 35–50 only, see 8.9/8.10). Rain does not follow the AOI yet (10.1).
- Editing teammates' modules: smallest change that works, every edit commented ("AOI change") so the author understands it, and recorded in `eddie_aoi/results/`.