# Proposal

## Why

The team has a documented EDDIE plugin architecture and one working plugin (`eddie_alzette_ingest`), but no plugin has yet proven the *derived-layer* path: reading rows an ingest plugin already wrote to Postgres, transforming them, and republishing the result as a new map layer that reaches the frontend through the existing Terria-catalog auto-discovery (`GET /terria-catalog.json` enumerating GeoServer workspaces). Before building the real precipitation interpolation module, the team wants a minimal, working proof that this path holds end-to-end — plugin discovery, DB read, transform, GeoServer publish, frontend visibility.

## What Changes

- New EDDIE plugin `eddie_precip_interpolation`, structured like `eddie_alzette_ingest` (empty `blueprint.py` required for plugin discovery; `tasks.py` as the only file importing `eddie.*`; plain, DB-free, unit-testable transform logic elsewhere).
- A manually triggered Celery task that:
  - reads all `weather_observations` rows for the precipitation variable (`sum_nn050`) together with their station geometry from `weather_stations`,
  - applies a placeholder transform — multiply `value` by 2 — standing in for the real spatial interpolation (future work, out of scope here),
  - writes the result to a new table,
  - publishes that table as a GeoServer vector layer in the existing `input_layers` workspace, so it appears automatically in `/terria-catalog.json` with no frontend code changes.
- No AOI filtering, no scheduler, no raster output, no new GeoServer workspace — deliberately the smallest slice that exercises the whole path once.
- Dev-only Docker wiring (a `docker-compose.override.yml` bind-mount entry) added for this plugin, following the exact same mechanism `eddie_alzette_ingest` already uses — including its known limitation (plugin code reaches the container via bind-mount into `site-packages`, not a real `pip install`, so it doesn't survive a from-clean-image build without the override). Fixing that mechanism is explicitly out of scope for this change.

## Capabilities

### New Capabilities
- `precip-interpolation`: the system's ability to derive a precipitation-based layer from already-ingested weather observations and publish it as a map layer discoverable via the Terria catalog. The initial implementation applies a placeholder multiply-by-two transform in place of real spatial interpolation, to validate the plugin/publish path before that algorithm is built.

### Modified Capabilities
(none — `eddie_alzette_ingest` and EDDIE core are untouched)

## Impact

- New top-level package `eddie_precip_interpolation/` (sibling to `eddie_alzette_ingest/`), with its own `pyproject.toml`, tests, and README following the same conventions.
- New database table holding the doubled precipitation values plus station geometry, written by this plugin (owned by this capability, not shared with `eddie_alzette_ingest`'s tables).
- New GeoServer layer in the `input_layers` workspace — no changes needed to `eddie.geoserver.terria_catalogs` or the Flask `/terria-catalog.json` route, since catalog generation already enumerates that workspace automatically.
- `EDDIE/eddie_antarctica/docker-compose.override.yml` gains a bind-mount + env entry for the new plugin, mirroring the existing `eddie_alzette_ingest` entries exactly.
- No changes to `eddie_alzette_ingest`, EDDIE core (`lib/eddie`), or `data_sources/*.yml`.
