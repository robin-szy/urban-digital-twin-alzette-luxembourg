# Tasks

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

## 7. Variant persistence and invalidation

- [x] 7.1 Create the `aoi_variant_cache` table keyed on reach and variant key, with geometry SRID 2169, params, source hash and timestamps; verify the migration applies and the composite key rejects a duplicate insert
- [x] 7.2 Implement variant caching with parameters encoded in the variant key, so differing parameters build separately rather than reusing a neighbouring entry; verify tests for a cache hit on repeat request and a miss when a parameter differs
- [x] 7.3 Implement source-hash invalidation, including not serving a degraded entry once its real source becomes available; verify tests for a changed source triggering a rebuild, an unchanged source serving from cache, and a degraded entry being rebuilt after its source appears
- [x] 7.4 Add source-layer read caching with a spatial index, shared across variants and reaches, separate from variant caching; verify a test that two lookup variants over one source layer read it once

## 8. Resolver and dataset bindings

- [ ] 8.1 Implement the dataset-to-variant binding registry and `resolve(dataset, reach)` returning geometry plus provenance; verify tests for a normal resolution, a degraded resolution, and rejection of an unregistered dataset with the registered names listed
- [ ] 8.2 Populate the initial bindings — landcover, buildings and addresses to `corridor(300)`; water gauges to `buffer(1000)`; precipitation to `watersheds`; `dem_build` to the envelope; `dem_publish` to `corridor(300)`; verify a test asserting each binding resolves to the expected variant
- [ ] 8.3 Implement `envelope()` as the union of the reach's variants for bounds-only sources; verify a test that it covers every variant's geometry
- [ ] 8.4 Reject a binding that uses a dataset's own derived variant as its analysis domain, and reject narrowing a build extent that a dataset declares a dependence on surrounding terrain for; verify tests for the flood-footprint-as-own-denominator rejection, the acceptance of that variant for another dataset, and the build-extent rejection
- [ ] 8.5 Document the registry and the three AOI roles (fetch envelope, clip mask, analysis domain) in `eddie_aoi/README.md`; verify the README states that the envelope must never be used as an analysis domain

## 9. Derived-data attribution and migration

- [ ] 9.1 Add reach, variant and degraded columns to `precipitation_interpolated_history` and the other derived tables in `eddie_precip_interpolation/db.py` and the water-level tables; verify the migration applies and the columns are present
- [ ] 9.2 Backfill existing precipitation and water-level rows with the Alzette 35–50 reach id; verify a query reports zero rows left unattributed
- [ ] 9.3 Stamp reach, variant and degraded on every derived row at write time; verify an integration test that rows written during a degraded resolution still carry the degraded marker when read back
- [ ] 9.4 Update the reuse check in `blueprint.py` and every other cache-hit query to match on reach and variant, not only on the dataset's own keys; verify tests that the same period for a different reach reports not-available, the same period for the same reach reports available, and a previously degraded period for the same reach reports not-available once the real source exists
- [ ] 9.5 Reject serving unattributed derived data as belonging to the current reach; verify a test that an untagged row is not returned for a reach query

## 10. Repoint the consumers

- [ ] 10.1 Replace `AOI_TABLE` in `eddie_precip_interpolation/tasks.py` with a resolver call; verify the precipitation task runs against the fallback reach and its output matches the pre-migration result for the same area
- [ ] 10.2 Replace `AOI_TABLE` and `AOI_STATION_TOLERANCE_M` in `eddie_water_level/tasks.py` with a resolver call bound to `buffer(1000)`; verify the same gauge set is selected as before the change
- [ ] 10.3 Stop reading `selected_polygon.geojson` in `EDDIE/eddie_antarctica/src/eddie_antartica/tasks.py:50` and use the configured fallback reach at worker startup; verify the worker starts and logs the reach it resolved

## 11. Cache eviction

- [ ] 11.1 Implement eviction by last-used age, retaining pinned reaches and reaches still referenced by derived data, reporting what was removed and what was retained with reasons; verify tests for removal of an unused reach, retention of the pinned fallback, and retention of a reach with referencing derived rows
- [ ] 11.2 Expose eviction as an explicitly invoked endpoint and confirm nothing is removed without a request; verify an integration test that no eviction occurs until the endpoint is called

## 12. Frontend selection via WPS

- [x] 12.0 Satisfy the EDDIE plugin contract: `tasks.py` (imported by the worker) and `blueprint.py` (imported and registered by the backend) are both mandatory for any `eddie_*` package in site-packages; mount the package and `src/static/geo` into backend and celery_worker; verify all six containers reach healthy with the module loaded
- [x] 12.0b Expose REST endpoints on the plugin blueprint - POST `/aoi/reach` (validates, then queues a build), GET `/aoi/reach/<id>`, GET `/aoi/reaches`, GET `/aoi/variants`; verify over HTTP that a valid request returns a task id and builds, and that a reversed range, unknown river and missing fields are each refused with a reason
- [x] 12.0g Serve the WPS process from the plugin's own `/aoi/wps` endpoint rather than the app's `processes` list in `src/eddie_antartica/blueprint.py`, which is COPYed into the image at build time and so unreachable without a rebuild; verify GetCapabilities advertises the process and the app's own `/wps` is left untouched
- [x] 12.0d Build and persist the display variants (corridor, buffer, bbox, watersheds) in the same task that builds a reach, so it is ready to show when the task reports success; verify the task result names each variant and that a variant with no fallback is reported rather than sinking the reach
- [x] 12.0e Serve a reach's AOI as GeoJSON in EPSG:4326 at `/aoi/reach/<id>/variants.geojson`, narrowable by `variant_key`, with provenance and the degraded marker on each feature; verify coordinates are geographic, the metric area is reported from the metric CRS, and the AOI bands stay well under the payload warning size
- [x] 12.0f Serve a Terria catalog fragment at `/terria-aoi-init/<id>.json` with one styled geojson item per variant; verify a degraded variant is named and described as a substitute in a warning style, the corridor shows by default and the bbox does not, and the group warns when anything is degraded
- [x] 12.0c Read lookup-variant source paths from the environment (`AOI_HQ100_PATH`, `AOI_WATERSHEDS_PATH`, `AOI_HQ_EXTREME_PATH`) so availability reflects what is actually on disk; verify `/aoi/variants` reports hq100 available and names why the other two are not

- [x] 12.1 Implement a `build_aoi` PyWPS process with literal `river`, `km_start` and `km_end` inputs and register it in the empty `processes` list in `src/eddie_antartica/blueprint.py`; verify the process appears in the `/wps` GetCapabilities response
- [ ] 12.2 Validate the selection before any build starts, refusing a reversed range, an unavailable river and kilometres outside the river's covered range without replacing the current AOI; verify a test per refusal case asserting the message and that the served AOI is unchanged
- [ ] 12.3 Return build status through the existing task-status route so building, success and the specific validation failure reason are all observable; verify an integration test covering all three states
- [ ] 12.4 Add a `{"type": "wps"}` catalog entry plus the reach centerline and AOI variant layers to the catalog fragment the plugin returns; verify Terria renders the input form and the AOI layers are listed
- [ ] 12.5 Flag degraded datasets in the catalog fragment naming the requested and substituted variant, and indicate when the fallback reach is in use; verify the flag is present while the watershed variant is degraded, absent when nothing is degraded, and present again after a client reload
- [ ] 12.6 Document the selector in `results/` — how to select a reach, what a degradation flag means, and the known WPS "Run Analysis" awkwardness with the layer-refresh nudge; verify the documented steps run as written against the live stack

## 13. EDDIE core CRS split

- [ ] 13.1 Split `to_working_crs_rectangle` in `lib/eddie/src/eddie/digitaltwin/working_crs.py:67-82` into `fetch_envelope()` keeping the bbox and a shape-preserving `to_working_crs()`; verify unit tests that the first returns a rectangle and the second preserves a corridor's shape
- [ ] 13.2 Repoint existing core callers to whichever function matches their role, leaving `retrieve_from_instructions` on the bbox for bounds-only endpoints; verify the existing core tests still pass
- [ ] 13.3 Record the diff in `results/` as a candidate upstream contribution, scoped to this split and its callers only; verify the recorded diff touches no unrelated CRS code

## 14. Integration verification

- [ ] 14.1 Run the full stack against the fallback reach and confirm precipitation, water level and the AOI layers all resolve through the registry; verify each layer's provenance reports the expected reach and variant
- [ ] 14.2 Select a second, different reach from the frontend and confirm derived data is recomputed rather than served from the first reach's cache; verify the reuse check reports not-available and the new rows carry the new reach id
- [ ] 14.3 Confirm no AOI geometry remains defined outside the module; verify a search finds no surviving `AOI_TABLE`, `AOI_STATION_TOLERANCE_M`, or `selected_polygon.geojson` AOI read in the plugins

## Workflow follow-up

- Raise with the team: a review trigger or date for the degraded watershed binding, so the fallback does not become permanent by default.
- Raise with Devika: consolidating her `build_corridor` against the new `corridor` variant, retiring the vendored copy.
- Raise with Pegah: migrating the Colab copy of `aoi.py` (`users/Pegah/population_03.py:31`) onto the module.
- Raise with Luiz: reconciling `config.toml [extent]` with the `dem_build` envelope binding, and the GeoTIFF rectangularity caveat for the publish clip.
- Archive the change once the team's review requirements are satisfied, then verify the archived result.
