# Design

## Context

`eddie_alzette_ingest` already establishes the EDDIE plugin skeleton (`blueprint.py` for discovery, `tasks.py` as the only `eddie.*`-importing file, plain testable core logic elsewhere) and populates two Postgres tables: `weather_stations` (station_id, station_name, geometry) and `weather_observations` (station_id, raw time fields, variable, value), as configured in `data_sources/asta_precipitation.yml`. EDDIE core's `/terria-catalog.json` route already enumerates every layer in the `static_files`/`input_layers`/`extruded_layers` GeoServer workspaces automatically — publishing into one of those workspaces is the entire "connect to frontend" step; no frontend code is touched. See `proposal.md` for why this change exists.

## Goals / Non-Goals

**Goals:**
- Prove the derive-from-DB -> transform -> publish-to-GeoServer -> auto-appears-in-catalog path works end to end, with the smallest plugin that can prove it.
- Give the real interpolation work a package to land in later (`eddie_precip_interpolation`) without redoing the plugin wiring.

**Non-Goals:**
- Real spatial interpolation (IDW/kriging/etc.) — the transform is a placeholder (`value * 2`).
- AOI filtering or any scheduling — out of scope per proposal, matches `eddie_alzette_ingest`'s existing "manually triggered" convention.
- Deciding a new GeoServer workspace taxonomy for derived/computed layers — reuses `input_layers`, as flagged in the proposal's Impact section.
- Fixing the plugin-packaging gap (site-packages bind-mount vs a real `pip install` baked into the Docker image) — this change inherits that limitation, it does not fix it.

## Decisions

**New package, not a task added to `eddie_alzette_ingest`.** Ingest (fetch external source -> raw tables) and derive (read raw tables -> compute -> publish) are different lifecycles — `eddie_alzette_ingest`'s own README already scopes it to "ingest a source described by `data_sources/*.yml`". Alternative considered: add a second task to `eddie_alzette_ingest/tasks.py`. Rejected — it would blur that package's boundary and couple the derive step's release cycle to the ingest plugin's.

**No cross-plugin Python import.** `eddie_precip_interpolation` reads `weather_stations`/`weather_observations` with its own plain SQLAlchemy queries against those table names, rather than importing `eddie_alzette_ingest.db`. Alternative considered: reuse `eddie_alzette_ingest`'s DB helpers directly. Rejected for this change — nothing in the codebase currently has one plugin import another's package, and doing so would make `eddie_precip_interpolation` fail to install/test independently if `eddie_alzette_ingest` isn't present. The trade-off (duplicated-looking read logic, coupling to hardcoded table/variable names instead of reading `data_sources/asta_precipitation.yml`) is accepted below as a known risk, not solved here, since the whole point of this change is the smallest possible plugin.

**Derived values live in a new table, not written back into `weather_observations`.** E.g. `precipitation_doubled(station_id, datetime, value_doubled, geometry)`, one row per source observation. Alternative considered: add a column to `weather_observations`. Rejected — that table is documented as append-only/raw by the plugin that owns it; mutating it from a different plugin's module would break that invariant.

**Publish via the existing `create_main_db_store` + `create_datastore_layer` helpers**, into `Workspaces.INPUT_LAYERS_WORKSPACE` — identical call shape to what `eddie_alzette_ingest.tasks.fetch_precipitation` already does for `weather_stations`. No new GeoServer or Terria-catalog code needed.

**Idempotent re-run**: the derived-table write uses the same upsert style (`ON CONFLICT DO UPDATE`) keyed by `(station_id, datetime)` that `eddie_alzette_ingest/db.py` already uses for its own tables, so re-running the task doesn't duplicate rows. `create_datastore_layer`/`create_db_store_if_not_exists`/`create_workspace_if_not_exists` are already idempotent (they check for existing GeoServer state first), so a second run's publish step is a no-op once the layer exists.

**Docker wiring mirrors `eddie_alzette_ingest` exactly**: a bind-mount entry in `EDDIE/eddie_antarctica/docker-compose.override.yml` for both `backend` and `celery_worker`, same path shape (`../../eddie_precip_interpolation/eddie_precip_interpolation:/venv/lib/python3.11/site-packages/eddie_precip_interpolation:ro`). No `DATA_SOURCES_DIR`-equivalent env var is needed since this plugin has no `data_sources/*.yml` of its own.

## Risks / Trade-offs

- **Hardcoded coupling to `eddie_alzette_ingest`'s schema** (table names `weather_stations`/`weather_observations`, variable code `sum_nn050`) with no shared contract enforcing it → if that YAML or its tables are renamed, this plugin breaks silently (empty or wrong results, no error). Mitigation: accepted for this smoke test; a later real interpolation module should decide whether to read table/variable names from `data_sources/asta_precipitation.yml` instead of hardcoding them.
- **Full-table read, no AOI scoping** → fine at today's data volume (a handful of stations, hourly observations); will need revisiting once the real interpolation module is AOI-scoped. Mitigation: explicitly out of scope here, same deferral as the proposal's AOI discussion.
- **Inherited packaging gap** → this plugin reaches the running containers only via the same site-packages bind-mount workaround `eddie_alzette_ingest` uses, so it's also absent from a `docker compose build` run without the override file. Mitigation: none in this change; tracked separately as a team-level decision already raised.
- **No cleanup path for derived rows** if a source observation is later deleted/corrected → stale derived row persists. Mitigation: acceptable given the source table is itself append-only; out of scope.

## Migration Plan

New table is created on first run (`CREATE TABLE IF NOT EXISTS`-style, matching `eddie_alzette_ingest/db.py`'s `checkfirst=True` pattern) — no existing data to migrate. Rollback is dropping the new table and removing the plugin's bind-mount entry; no other system is affected.
