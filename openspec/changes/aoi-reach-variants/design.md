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

### Eviction is manual first

An endpoint taking an age threshold, with pinned reaches and reaches referenced by derived data retained. No Celery beat schedule — the team has not decided on a scheduling mechanism, and that decision is not this change's to make.

### DEM declares two bindings and nothing more

`dem_build` → envelope, `dem_publish` → corridor. No pipeline code changes here.

*Why build cannot be narrowed:* flow accumulation, D8 routing, filled DEM and watershed delineation all need upstream contributing area. Clipping to a 600 m corridor first makes every accumulation value wrong, because the contributing hillslopes are gone.

*Caveat to record for whoever implements the publish clip:* GeoTIFF is rectangular. Masking to a corridor yields an array the size of the corridor's bounding box with nodata in the corners, so there is no in-memory saving by itself. The saving materialises through `users/Luiz Ganem/config/config.toml`'s windowed `block_size = 2048` path skipping all-nodata blocks, plus compression on disk. A naive mask will measure as no improvement.

## Risks / Trade-offs

- **Adding `reach_id` to derived tables is larger than the AOI module itself** → Sequence it first and separately (see Migration Plan). Until the reuse checks match on reach, the live selector must not be enabled, or the cache will report "already available" and skip computation for the wrong area.
- **Vendored `select_route` diverges from Devika's original** → Provenance header plus behaviour tests on the validation invariants. Consolidation is already a named follow-up, not an open question.
- **Degraded operation becomes permanent and normalised if the watershed layer never arrives** → Two independent records, a catalog flag the user sees every session, and an explicit open question below with a review trigger rather than an indefinite wait.
- **The EDDIE core diff drifts from upstream** → Keep it to the one function split and repointed callers; do not fold unrelated CRS work into the same diff.
- **Cache growth with a live selector** → Eviction endpoint from the start, even though it is manual, so there is a remedy before there is a problem.
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

**Rollback:** steps 1–2 are additive — new tables and nullable columns — and can be left in place harmlessly. Step 3 reverts with the queries. Step 4 reverts by restoring the constants, which is why step 1's geometry comparison matters: it establishes that the resolver and the old constant name the same area. Step 5 reverts by unregistering the process and leaving the fallback pinned. Step 6 reverts independently.

## Open Questions

- **Does the Terria degradation flag go in the layer name or its description?** Cosmetic, decidable when the catalog fragment is written; does not affect the specs or the task breakdown.
- **Should eviction eventually be scheduled?** Blocked on the team's unresolved scheduling-mechanism decision. The manual endpoint is sufficient until then.
- **What is the review trigger for the degraded watershed binding?** Needs a date or a milestone from the team rather than a technical answer, so that the fallback does not silently become permanent. Worth putting to them at the next team sync; it changes no code here.
