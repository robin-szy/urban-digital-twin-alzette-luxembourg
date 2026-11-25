# Design

## Context

See `proposal.md` — Why, for the motivation. The constraints that shape the approach:

- **The only validated river geometry in the repo is not importable.** `users/devika/src/river_aoi.py` is reached by `sys.path.insert(0, parents[2])` followed by `from devika.src.river_aoi import build_corridor` (see `users/devika/scripts/prepare_river_corridor.py`). There is no `pyproject.toml` or `setup.py` under `users/devika/`; the import resolves only because `devika` and `devika.src` happen to work as implicit namespace packages, with `ROOT` pointing at `users/`. That path trick will not survive running inside the EDDIE containers.
- **The three existing plugins share one shape**: a top-level directory with `pyproject.toml`, a package directory, and `tests/` (`eddie_alzette_ingest`, `eddie_precip_interpolation`, `eddie_water_level`). Two of them already hold the duplicated `AOI_TABLE` constant.
- **EDDIE core rectangularizes unconditionally.** `lib/eddie/src/eddie/digitaltwin/working_crs.py:67-82` returns `shapely.box(*reprojected.total_bounds)`. Core is editable by team decision, but the bbox there is not a bug for its own caller — `retrieve_from_instructions` requests data from WFS/ArcGIS endpoints that accept only rectangular bounds.
- **The frontend has no AOI input and no custom code.** The existing precipitation timeline is GeoServer's WMS TIME dimension driving TerriaJS's native widget, configured by a catalog fragment from `/terria-date-init/<date>/<p>/<w>.json`. PyWPS is already mounted at `/wps` with a complete `src/pywps.cfg` and `processes = []` empty.
- **Derived data has no notion of area.** `precipitation_interpolated_history` is keyed `(grid_id, datetime)`; the reuse check in `blueprint.py` is `COUNT(DISTINCT datetime)` over a date range.
- **The external watershed layer has no delivery date.**

## Goals / Non-Goals

**Goals:**

- One importable module that is the single source of AOI geometry for every plugin and script.
- Preserve the validation semantics of `select_route` exactly — they are the only thing preventing a corridor around the wrong tributary.
- Make the AOI a live, cached, frontend-selectable parameter rather than a constant.
- Make a degraded AOI impossible to mistake for the requested one, both at the moment of use and afterwards in the stored data.
- Keep the EDDIE core diff narrow enough to be plausibly upstreamable.

**Non-Goals:**

- Reorganising `users/` or making any teammate's directory an installable package.
- Any change to the DEM/terrain pipeline beyond declaring its two-stage binding.
- Scheduled or automatic cache eviction.
- Supporting a user-drawn AOI polygon. The input is a kilometre range; a drawn shape would have to be reverse-mapped onto chainage to be usable.
- Multi-river or discontinuous reaches.

## Decisions

### Module lives at `eddie_aoi/`, shaped like the existing plugins

A top-level `eddie_aoi/` with `pyproject.toml`, package directory and `tests/`, matching the other three. `eddie_precip_interpolation` and `eddie_water_level` take it as a dependency.

*Alternatives:* inside `users/robin/` — rejected, the AOI is shared and `users/` content is being adapted into a different architecture anyway. A standalone HTTP service — rejected as disproportionate; the consumers are already in-process with Celery.

*Consequence:* a new inter-plugin dependency. Acceptable — both consumers already depend on `eddie.*` core, so they are not dependency-free today.

### `select_route` is vendored verbatim, not imported and not rewritten

Copy `select_route` (and the one-line `PreparationError`) into `eddie_aoi` byte-identical, under a provenance header naming `users/devika/src/river_aoi.py`, the commit it came from, and the author. Cover it with tests that assert the validation behaviour — markers within tolerance, strictly increasing chainage, the 2% length check, disconnected-river rejection — so a future edit cannot quietly weaken it.

*Alternatives considered:*

| Option | Why not |
|---|---|
| Import from `users/devika/` | Depends on a `sys.path` trick and implicit namespace packages; `users/` is not a package and `users/Luiz Ganem/` contains a space. Will not work in the containers. |
| Add `pyproject.toml` under `users/devika/` | Restructures a teammate's directory. That is the conversation the proposal explicitly deferred, and it would block this change on it. |
| Rewrite the routing | Discards the only validated river geometry in the repo. Not a real option. |

*Trade-off accepted:* two copies of the routing logic until the follow-up consolidation. Mitigated by the provenance header and by the tests; the proposal already lists deduplication as a follow-up.

### A reach is the identity; polygons are derived

The cached entity is `(river, km_start, km_end)` → validated centerline + chainage. Variants hang off it.

*Why:* the user selects two numbers, so a polygon is an answer rather than a question. Keeping the centerline means chainage-dependent consumers (longitudinal water-level interpolation, "water level at km 42") do not re-derive it, and every variant is `f(centerline)` rather than a fresh re-read of the marker source. If the AOI were stored as a polygon, the centerline would be thrown away and each variant would have to rebuild it — which is how the five current implementations arose.

### `reach_id` is canonical at 1 metre precision

Format `<river-slug>_<start_m>_<end_m>` zero-padded to six digits, e.g. `alzette_035000_050000`. Kilometres are rounded to whole metres before formatting.

*Why 1 m:* the marker source spaces points at 100 m and `KILO_KM` already carries float noise (`users/robin/src/aoi.py:14-16` notes values like `46.299999` and compensates with a `1e-3` tolerance). Sub-metre precision in the key would fragment the cache on rounding noise alone; 1 m is two orders of magnitude finer than the marker spacing.

### Variants: one signature, two classes, with declared availability

`build(reach, params) -> single-row GeoDataFrame in EPSG:2169` for all of them. Computed variants depend only on the centerline; lookup variants select and dissolve from a configured source layer and declare availability up front rather than failing on first build.

Variant cache key is `(reach_id, variant_key)` where `variant_key` encodes the parameters. **Parameters are part of the key and geometry is never derived between parameter values**: `buffer(500).buffer(-200)` is not `buffer(300)` once meanders make the wider buffer self-overlap, and the flat end caps do not invert. The containment holds but the construction does not, so eroding a cached wider corridor would yield a subtly wrong narrower one.

The reuse that *does* exist is one level down: repeated reads and intersections of the same external source layer (the HQ100 shapefile, the watershed layer) across variants and reaches. That is cached at source-layer access with its own spatial index, separate from variant caching. Variant derivation itself is a buffer on a 15 km line — milliseconds — so variant-cache granularity is not a cost driver. The reach cache is where the expensive work (routing plus validation) is saved.

### Degradation is recorded twice, deliberately

`resolve(dataset)` returns `(geometry, provenance)` with `{requested, actual, degraded, reason}`. A degraded resolution is surfaced in the Terria catalog **and** stamped on every derived row written during it.

*Why both:* a frontend warning is attached to the act of viewing. Once a layer is cached and the warning is no longer rendered, nothing distinguishes rows computed over the fallback from rows computed over the real watershed. The row-level stamp is what makes the degradation recoverable months later; the catalog flag is what stops it being forgotten now.

*Alternative considered:* fail hard on an unavailable variant. Rejected by the user — the watershed layer has no date and hard failure would block precipitation entirely. The cost of the softer choice is that degraded operation can become normalised, which is exactly what the two records and the proposal's open question exist to resist.

### Core change splits the two meanings of `to_working_crs_rectangle`

`fetch_envelope(aoi)` keeps today's behaviour for bounds-only sources; a new shape-preserving `to_working_crs(aoi)` is used for clipping and analysis. Existing core callers are repointed to whichever matches their role.

*Why this shape:* it is a separation of two conflated roles rather than a behaviour reversal, so it reads as a defect fix and stands a chance of being accepted upstream — keeping our fork small. Deleting the rectangularization outright would break `retrieve_from_instructions` against bbox-only endpoints.

### Frontend entry point is a PyWPS process

A `build_aoi` process with literal `river`, `km_start`, `km_end` inputs, registered in the empty `processes` list, plus a `{"type": "wps"}` entry in the catalog fragment the plugin already returns. TerriaJS's `WebProcessingServiceCatalogFunction` generates the form.

| Option | Real input form | Cost | Verdict |
|---|---|---|---|
| PyWPS process + `wps` catalog item | yes, auto-generated | endpoint, config and empty list already exist | **chosen** |
| URL-parameterised init route | no — user edits a URL | lowest | not a UI |
| Custom panel inside TerriaJS | yes, exactly as wanted | high — Terria is vendored by Dockerfile only | deferred |
| Separate page beside Terria | yes | medium | sidesteps the map client |

*Accepted weakness:* Terria's WPS flow is "Run Analysis" plus a result item, and refreshing the AOI layers afterwards needs an explicit nudge. A custom panel remains the upgrade path if the UX proves too awkward.

### Storage is PostGIS, SRID 2169, with source hashes

`reach_cache` and `aoi_variant_cache` as described in the proposal, both with a source hash and `last_used_at`. The hash covers the input layers; `users/devika/src/derived_layers.py` already computes `input_sha256_before` inside `run_export`, so the approach is proven in this codebase — this change persists it instead of only reporting it.

*Why hashes rather than file mtimes:* a re-downloaded but identical layer should not invalidate, and a layer edited in place with a preserved mtime must.

### The current reach is deployment state

A one-row `aoi_current` table referencing `reach_cache.reach_id` with `ON DELETE RESTRICT`, plus the time it was set. It is written only when a selection's build reaches `ready`, so a refused or failed selection never replaces it. When the row is absent, the pinned fallback is the current reach and resolutions report that the fallback was used.

*Why a table rather than a flag on `reach_cache`:* exactly one current reach is a property of the table, not of each row; a flag needs a uniqueness guard and a two-row update on every switch. `ON DELETE RESTRICT` makes "the current reach is never evicted" hold in the database, not only in the eviction code.

*Accepted consequence:* the current reach is shared by everyone using the deployment. A selection by one viewer changes what another sees after a reload. Acceptable for a single-host project twin; per-user selection would need a session concept the app does not have.

### `resolve_aoi` is the seam modules call

`eddie_base_data.query.resolve_aoi(conn, dataset)` keeps its signature. Its body resolves the dataset through the binding registry for the current reach and maps the result onto `AoiScope` (`reach_id`, `variant_key`, geometry, `degraded`, `degraded_reason`). A module that already calls it — `eddie_building_population` — follows the selection with no edit to its code. `building_population` and `flood_costs` both bind to `corridor(500)`, replacing the provisional `bbox_500` and `study_area_frozen`. 500 m rather than the plan's 300 m for buildings because it is the band Devika's flood-cost study already uses (`BUILDINGS_CORRIDOR_M`), so population and damage are counted over the same area and stay comparable.

### Flood costs follows the reach with minimal, commented edits

Devika's study area is already "a 500 m corridor along the km range, widened by the flood extents", so following the reach means changing *which km range*, not her method. The edits, each carrying a comment that says what changed and why, so she can review them without reconstructing the reasoning:

1. `flood_costs/tasks.py` (wrapper): `_frozen_scope()` becomes `resolve_aoi(conn, "flood_costs")`.
2. `postgis_inputs.markers()` (wrapper): the km filter comes from the current reach instead of `34.999–50.001`.
3. `depth_reconstruction.py`: `RIVER_BEGIN_KM` / `RIVER_END_KM` in the constants passed to the reference functions (`:489`), and the second marker filter (`:615`), come from the reach. The SHA-pinned reference functions themselves are not edited.
4. `depth_reconstruction.py:674-676`: the 184-building comparison cohort is checked only for cohort buildings inside the current study area; as written, any reach leaving one of them out fails the assertion.
5. Damage: a building without a reviewed building type is reported unknown, never zero and never classified by code. The review decisions are human input. Hardcoded counts that break for another reach are found on a first run and relaxed minimally, each with a comment.

*Limits that stay, reported rather than hidden:* depths are interpolated between the gauges at km 35.04, 45.35 and 54.34, so only the part of a reach within km 35–54.34 is computed and the rest is flagged as not covered; damage totals cover classified buildings only and report the unknown count, as today.

*Proof that the method is unchanged:* for the km 35–50 reach the edited module must reproduce today's results exactly — 276 buildings, HQ10 €122,528, HQ100 €1,603,670 provisional.

*Rule for every teammate's module, here and later (Lagloire's, Luiz's):* new behaviour goes in our wrapper where possible; where their file must change, the edit is the smallest that works and is commented at the point of change.

### Selecting a reach triggers recompute

At the end of a successful selection build, after the current reach is set, the worker sends the task of every dataset registered as following the selection; the list sits beside the bindings. In this round it holds `eddie_building_population.tasks.compute_building_population` and the flood-cost chain (depth reconstruction, then damage, then publish), which has no reuse check either. The build result names the tasks it sent. A dataset joins the list only once it resolves through the registry *and* its reuse checks match on reach (see Migration Plan).

*Why automatic:* without it the map shows the new reach while every number still describes the old one, and nothing on screen says so.

### Published views show the current reach

`write_derived` keeps rows for every reach and variant, which is what makes switching back cheap. The `_4326` view it creates filters to the current reach and the dataset's bound variant, so a second selection shows one reach, not both, and rows of a superseded binding (the old `bbox_500`) disappear from the map without being deleted. GeoServer keeps a layer's bounding box from when the layer was created, so it is recalculated after the current reach changes.

### Eviction is manual first

An endpoint taking an age threshold. The pinned fallback and the current reach are retained. Evicting a reach also deletes the derived dataset rows computed for it, from every table carrying `reach_id`, in the same transaction, and reports the rows removed per table. No Celery beat schedule — the team has not decided on a scheduling mechanism, and that decision is not this change's to make.

*Why derived rows go with the reach:* with automatic recompute every selected reach gets derived rows, so a rule that retained referenced reaches would make eviction remove almost nothing. The derived rows in scope here are recomputable from base tables. This must be revisited before precipitation history follows the selection, since recomputing it is costly.

### The watershed fallback is a generous buffer, larger than every other variant

Until a watershed layer exists, `watersheds` degrades to `buffer(5000)` — 5 km around the reach, replacing the earlier `buffer(1000)` (`WATERSHED_FALLBACK_BUFFER_M` in `eddie_aoi/variants.py`), which was no larger than the gauge buffer. It is deliberately the largest variant, so the envelope (the union of all variants) and every dataset bound to watersheds — precipitation, and Lagloire's weather modules later — keep the lateral tributaries and hillslopes a narrow band would cut off.

*Still not a watershed:* a buffer is not hydrologically complete — it misses the upstream catchment, part of which lies in France — so the result stays marked degraded. 5 km is a starting value, not a derived one; it is one constant and can be tuned.

### DEM declares two bindings and nothing more

`dem_build` → envelope, `dem_publish` → corridor. No pipeline code changes here.

*Why build cannot be narrowed:* flow accumulation, D8 routing, filled DEM and watershed delineation all need upstream contributing area. Clipping to a 600 m corridor first makes every accumulation value wrong, because the contributing hillslopes are gone.

*Caveat to record for whoever implements the publish clip:* GeoTIFF is rectangular. Masking to a corridor yields an array the size of the corridor's bounding box with nodata in the corners, so there is no in-memory saving by itself. The saving materialises through `users/Luiz Ganem/config/config.toml`'s windowed `block_size = 2048` path skipping all-nodata blocks, plus compression on disk. A naive mask will measure as no improvement.

## Risks / Trade-offs

- **Adding `reach_id` to derived tables is larger than the AOI module itself** → Sequence it separately (see Migration Plan). Until a consumer's reuse checks match on reach, that consumer must not follow the selection, or its cache will report "already available" and skip computation for the wrong area. The selector itself is live; the guard is per consumer.
- **Vendored `select_route` diverges from Devika's original** → Provenance header plus behaviour tests on the validation invariants. Consolidation is already a named follow-up, not an open question.
- **Degraded operation becomes permanent and normalised if the watershed layer never arrives** → Two independent records, a catalog flag the user sees every session, and an explicit open question below with a review trigger rather than an indefinite wait.
- **The EDDIE core diff drifts from upstream** → Keep it to the one function split and repointed callers; do not fold unrelated CRS work into the same diff.
- **Cache growth with a live selector** → Eviction endpoint from the start, even though it is manual, so there is a remedy before there is a problem.
- **Eviction deletes derived rows** → Limited to recomputable results in this round; the current and pinned reaches' rows are never touched; the report names every table and row count. Revisit before an expensive-to-recompute dataset (precipitation history) follows the selection.
- **Automatic recompute changes a teammate's output** → `building_population` moves from `bbox_500` to `corridor(500)` the moment the seam lands. Record old and new building and population counts and tell Pegah before it reaches the demo. Flood costs must reproduce its km 35–50 results exactly before it is put on the follower list.
- **Flood-cost recompute is slow** → Depth reconstruction runs per selection; its duration for one reach is measured on the first run, and if it is long the build status says the costs are still computing rather than showing stale numbers.
- **The map and the numbers can still describe different areas** → Only datasets on the follower list move with the selection; precipitation and water level keep their own area this round. Their layers keep their own reach attribution so the difference is inspectable.
- **WPS form UX proves too clunky for the demo** → The URL-parameterised route stays available as a fallback for a scripted demo path, and the custom panel is the upgrade.
- **A reach spanning a confluence or a braided section may fail validation legitimately** → Surface the specific reason (marker offset, ordering, length mismatch) so the user can tell a bad selection from a data problem; do not add a tolerance override that would defeat the 2% check.

## Migration Plan

The fallback reach is Alzette km 35–50, which matches the existing `aoi_alzette_35_50_300m` table and Devika's corridor. That makes the data migration a tagging exercise rather than a deletion.

1. **Stand up `eddie_aoi` with the fallback reach only.** Build `reach_cache`/`aoi_variant_cache`, resolve the configured fallback at startup, verify its corridor matches the existing `aoi_alzette_35_50_300m` geometry. No consumer changes yet — this is a read-only comparison step that proves the module reproduces the current AOI before anything depends on it.
2. **Add `reach_id`, variant and degraded columns to derived tables**, backfilling existing precipitation and water-level rows with the km 35–50 reach id. Safe because the fallback is that reach.
3. **Update every reuse check** to match on reach and variant. Until this lands, step 5 must not.
4. **Repoint consumers** to the resolver: drop `AOI_TABLE` from both plugins and `AOI_STATION_TOLERANCE_M` in favour of `buffer(1000)`. Compare outputs against the pre-migration results for the same reach.
5. **Enable the WPS selector** and non-fallback reaches.
6. **Split the core function** and repoint its callers. Independent of 1–5 and can proceed in parallel.

**Where this stands (2026-11-20).** Step 1 is done and step 5 is already live: the WPS selector and non-fallback reaches work. That is safe only because no consumer with a reuse check follows the selection yet — precipitation and water level still read `AOI_TABLE`, so their reuse checks never see another reach. The governing rule from here: **a consumer may follow the selection only once it resolves through the registry and its reuse checks match on reach.** This round therefore adds two consumers, `building_population` and `flood_costs`, neither of which has a reuse check (both recompute on every run and stamp the reach on what they write), and defers steps 2–4 for precipitation and water level, and step 6, to a later round.

**Rollback:** steps 1–2 are additive — new tables and nullable columns — and can be left in place harmlessly. Step 3 reverts with the queries. Step 4 reverts by restoring the constants, which is why step 1's geometry comparison matters: it establishes that the resolver and the old constant name the same area. Step 5 reverts by unregistering the process and leaving the fallback pinned. Step 6 reverts independently.

## Open Questions

- **Does the Terria degradation flag go in the layer name or its description?** Cosmetic, decidable when the catalog fragment is written; does not affect the specs or the task breakdown.
- **DEM and terrain follow the reach envelope — planned as a separate change with Lagloire** (working name `dem-follows-aoi`). Two DEMs are in play and must not be confused. The national 5 m DEM is on the host as `EDDIE/eddie_antarctica/data/terrain/dem_merged_FULL_BACKUP.tif` (11462 × 16369 px, 337 MB); the map's Cesium terrain tiles (`data/terrain/lidar_egm2008/`) were built from it on 2026-11-07, so the map shows terrain for all of Luxembourg. On 2026-11-09 `dem_merged.tif` — the file `TERRAIN_DEM` points at, which water level, the flood-depth grid and flood-cost depth reconstruction compute on — was cut to a 12 × 18 km extract covering about Alzette km 30.2–58.9 (26 MB). Why is not recorded; the timing falls in the 3D out-of-memory investigation. So a reach outside km ~30–59 shows terrain on the map but has none to compute on. The intent is to compute on the national DEM cut to the reach's largest AOI (the envelope, driven by the watershed variant). Points settled in exploration: (1) cut what is *computed on* by a windowed read per reach, as flood costs already does with `WindowedDEM`; leave the Cesium terrain tiles alone — they already cover the country and Cesium loads only what is in view; (2) the water-level reads (`flood_grid.dem_grid()`, `rem.py`) read the whole DEM and must be windowed before `TERRAIN_DEM` is pointed back at the national file, or memory blows up (~0.75 GB per float array at 5 m for the country); (3) the Alzette rises in France, so even a real watershed is not fully covered by a Luxembourg DEM — a coverage limit to state; the national DEM could also produce the missing watershed layer by catchment delineation, or an official layer may exist on data.public.lu.
- **When does EDDIE's static ingest stop reading `selected_polygon.geojson`?** Replacing it (task 10.3b) widens `buildings_3d_polygons_eddie` and the land-use table from the km 46.3–48.3 rectangle to km 35–50, which is where the 3D out-of-memory problem lives. Deferred until that is understood.
- **Should eviction eventually be scheduled?** Blocked on the team's unresolved scheduling-mechanism decision. The manual endpoint is sufficient until then.
- **What is the review trigger for the degraded watershed binding?** Needs a date or a milestone from the team rather than a technical answer, so that the fallback does not silently become permanent. Worth putting to them at the next team sync; it changes no code here.
