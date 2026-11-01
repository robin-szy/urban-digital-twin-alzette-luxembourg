# Proposal

## Why

The project has five incompatible definitions of "the AOI" — a hardcoded lat/lon rectangle in `selected_polygon.geojson`, a bbox-plus-margin in `users/robin/src/aoi.py`, a validated river corridor in `users/devika/src/river_aoi.py`, a PostGIS table name duplicated as a constant in two plugins, and a `1000.0` metre tolerance hiding in `eddie_water_level`. They cover three different kilometre ranges (46.3–48.3, 35–50, 42–50), so no two datasets are currently clipped to the same area and no cross-dataset percentage is comparable. Different datasets genuinely need different areas (precipitation needs watersheds, gauges need a wider buffer, the DEM needs hydrologically complete upstream terrain), and without a shared module every new dataset adds another undeclared variant as a magic number.

## What Changes

- **New AOI module** built on a *reach* identity — `(river, km_start, km_end)` plus a validated centerline and chainage. A reach is one-dimensional; every AOI polygon is a derivation from it. This replaces "the AOI is a polygon" as the project's central assumption.
- **Reuse `select_route()` from `users/devika/src/river_aoi.py` unchanged.** It is already a pure function of two geometry lists and is the only code in the repo that verifies the route is the actual Alzette (all markers within 20 m, strictly increasing chainage, route length within 2% of the chainage span) rather than a tributary. Only `build_corridor` hardcodes the river name.
- **Variant providers** behind one protocol, `build(reach, params) -> single-row GeoDataFrame in EPSG:2169`. Two classes: *computed* (`corridor`, `bbox`, `buffer`) and *lookup* (`watersheds`, `hq100`, `hq_extreme`), which select and dissolve an externally supplied layer and may be unavailable.
- **A resolver** with an explicit dataset-to-variant binding registry, replacing the scattered constants. It keeps three roles of "AOI" distinct that are currently conflated: *fetch envelope* (a bbox is legitimately correct — most APIs only accept bboxes), *clip mask* (per dataset), and *analysis domain* (exactly one polygon per metric, or percentages are not comparable).
- **Graceful degradation with visible provenance.** The external watershed layer has no delivery date, so `precipitation` binds to `watersheds` but falls back to river-km-plus-margin. `resolve()` returns the polygon together with `{requested, actual, degraded, reason}`, surfaced both in the Terria catalog and stamped on every derived row built while degraded — a frontend-only warning disappears as soon as a layer is cached.
- **Live reach selection from the frontend via WPS.** `src/eddie_antartica/blueprint.py` already serves `/wps` with a fully configured `src/pywps.cfg` and an empty `processes` list; a `build_aoi` process with literal `river`/`km_start`/`km_end` inputs makes TerriaJS auto-generate the input form.
- **Reach and variant caching** keyed on a canonical `reach_id` (fixed precision, zero-padded, so `35`, `35.0` and `35.00` collapse to one key), invalidated by a source hash over the input layers, with LRU eviction behind a manual endpoint.
- **BREAKING: `reach_id` becomes required on derived data.** `precipitation_interpolated_history` is keyed `(grid_id, datetime)` only, and the cache-hit check in `blueprint.py` is a bare `COUNT(DISTINCT datetime)` over a date range — so once the reach can change, it reports "already available" and skips the run for the wrong area. Every derived table and every cache-hit query gains the reach.
- **BREAKING: three constants are retired** — `selected_polygon.geojson` as the AOI source, `AOI_TABLE` in `eddie_precip_interpolation` and `eddie_water_level`, and `AOI_STATION_TOLERANCE_M` (becomes `buffer(1000)`, a declared variant).
- **EDDIE core change**: split `to_working_crs_rectangle` in `lib/eddie/src/eddie/digitaltwin/working_crs.py` into `fetch_envelope()` (keeps the bbox, correct for WFS/ArcGIS requests) and a shape-preserving `to_working_crs()`. Kept as a narrow, plausibly upstreamable diff rather than a maintained fork.
- **One CRS rule**: compute and store EPSG:2169; reproject to 4326 only at the WMS/WFS/GeoJSON publish boundary; never read 4326 back to compute. Devika's buffer order is already correct — the defect is the handoff, since at 49.5°N a `.area` in degrees is meaningless and a `.buffer` is 1.54:1 anisotropic, both silently.
- **Fallback reach from config, not code**: Alzette km 35–50, pinned so eviction cannot remove it. It matches the existing `aoi_alzette_35_50_300m` table and Devika's corridor, so existing precipitation and water-level rows can be *tagged* with that reach id rather than dropped.

## Capabilities

### New Capabilities
- `aoi/reach`: Defining a river reach from river kilometres — canonical identity, centerline and chainage derivation, route validation against the kilometre markers, and the reach cache lifecycle including eviction.
- `aoi/variants`: Deriving AOI polygons from a reach — the provider protocol, computed and lookup variant classes, degradation provenance when a lookup source is unavailable, and variant caching with source-hash invalidation.
- `aoi/resolution`: Serving the correct AOI per dataset — the dataset-to-variant binding registry, the separation of fetch envelope from clip mask from analysis domain, and the reach stamping that keeps derived data attributable to the area it was computed over.
- `aoi/selection`: Selecting a reach from the frontend — the WPS process and its inputs, validation feedback, build status, and making a degraded AOI visible to the user in the map client.

### Modified Capabilities
<!-- None: the project has no existing specs (`openspec list --specs` reports none). -->

## Impact

**Affected code**

- New AOI module (location to be decided in design; it must be importable by EDDIE plugins).
- `EDDIE/eddie_antarctica/src/eddie_antartica/tasks.py:50` — startup no longer reads `selected_polygon.geojson`.
- `EDDIE/eddie_antarctica/src/eddie_antartica/blueprint.py` — register the `build_aoi` PyWPS process in the empty `processes` list; add a `type: "wps"` catalog entry to the fragment it already returns; add the cache-eviction endpoint; add the reach to the precipitation cache-hit query.
- `EDDIE/eddie_antarctica/lib/eddie/src/eddie/digitaltwin/working_crs.py:67-82` — split into `fetch_envelope()` and `to_working_crs()`.
- `eddie_precip_interpolation/tasks.py:17` and `eddie_water_level/tasks.py:38,48` — drop `AOI_TABLE` and `AOI_STATION_TOLERANCE_M`, resolve through the registry instead.
- `eddie_precip_interpolation/db.py` — `reach_id` on `precipitation_interpolated_history` and the other derived tables.
- `users/devika/src/river_aoi.py` — imported, not modified.

**Affected data**

- New `reach_cache` and `aoi_variant_cache` tables in PostGIS (SRID 2169).
- Existing `precipitation_interpolated_history` and water-level rows need tagging with the km 35–50 reach id before the live selector goes in; untagged they are indistinguishable from any other reach.

**Dependencies**

- The external watershed layer is an unresolved dependency with no delivery date — hence the degraded-fallback design. `data/raw/Flood_hazard_HQ100/HWGK2021_HQ100.shp` is available; `hq_extreme` is not yet obtained.

**Out of scope (follow-ups, not work in this change)**

- Deduplicating Devika's `build_corridor` against the new `corridor` variant — her module, and a conversation with her rather than a unilateral rewrite.
- Migrating Pegah's Colab copy of `aoi.py` (`users/Pegah/population_03.py:31`).
- Reconciling Luiz's `config.toml [extent]` DEM rectangle with the resolver.
- Any raster/DEM pipeline change beyond declaring the two-stage `dem_build` / `dem_publish` bindings. Clipping the DEM before hydrology would break flow accumulation, which needs upstream contributing area; the earlier whole-Luxembourg crash cause is unconfirmed and is not a justification here.
