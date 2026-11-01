# Tasks

## 1. Package scaffold

- [x] 1.1 Create `eddie_precip_interpolation/` package structure (`pyproject.toml`, `eddie_precip_interpolation/__init__.py`), mirroring `eddie_alzette_ingest/`'s layout; verify `pip install -e eddie_precip_interpolation[dev]` succeeds and `python -c "import eddie_precip_interpolation"` works.
- [x] 1.2 Add `eddie_precip_interpolation/blueprint.py` with an empty `blueprint = Blueprint("eddie_precip_interpolation", __name__)`; verify a unit test imports the module and asserts `blueprint.name == "eddie_precip_interpolation"`.

## 2. Read and transform logic (plain functions, no `eddie.*` import, testable without Docker)

- [x] 2.1 Implement a function reading all `sum_nn050` rows from `weather_observations` joined to `weather_stations` geometry into a GeoDataFrame; verify with a SQLite-backed unit test using fixture rows (including a station with a null `value`, matching the real source's known null case). *(Split into `read_precipitation_observations` (SQL, sqlite-unit-tested) + `attach_station_geometry` (pure merge, unit-tested in-memory) because a real Geometry column can't be unit-tested on SQLite - same boundary `eddie_alzette_ingest/db.py` already draws around its own stations table; see design.md.)*
- [x] 2.2 Implement the placeholder transform (`value_doubled = value * 2`, preserving NULL as NULL); verify a unit test asserts the doubled value for populated rows and that a null `value` stays null, not `0`.
- [x] 2.3 Implement a write function that upserts the transformed rows into a new table (e.g. `precipitation_doubled`, columns `station_id`, `datetime`, `value_doubled`, `geometry`), keyed by `(station_id, datetime)`, following the same `ON CONFLICT` upsert style as `eddie_alzette_ingest/db.py`; verify a unit test that calling it twice with the same input does not duplicate rows. *(Geometry column means this is integration-tested against the live `db_postgres_digital_twin` container, not SQLite-unit-tested - same boundary as `eddie_alzette_ingest`'s `test_upsert_stations_overwrites_on_conflict`. Ran for real: row count stays at 1 on re-run, overwrite takes effect.)*

## 3. Celery task and GeoServer publish (`tasks.py`, the only file importing `eddie.*`)

- [x] 3.1 Implement `@app.task(name="eddie_precip_interpolation.tasks.derive_precipitation")` wiring read -> transform -> write, then call `gs.create_main_db_store` + `gs.create_datastore_layer` to publish the new table into `Workspaces.INPUT_LAYERS_WORKSPACE`; verify with an `@pytest.mark.integration` test against the running `db_postgres_digital_twin` + `geoserver` containers (skipped by default, matching `eddie_alzette_ingest`'s existing test convention). *(No pytest test imports `eddie.*` anywhere in this repo - `eddie` core only exists inside the containers, not on the host where pytest runs; `eddie_alzette_ingest` hits the same wall and verifies `tasks.py` by actually running it, not via pytest. Did the same, for real: triggered via `docker compose exec celery_worker ... celery call eddie_precip_interpolation.tasks.derive_precipitation` against the live stack - succeeded with `{'rows_written': 43860, 'layer': 'precipitation_doubled'}`, matching the live `weather_observations` `sum_nn050` count exactly; spot-checked values in `precipitation_doubled` against source rows, confirmed `value_doubled = value * 2` and NULL preserved as NULL.)*
- [x] 3.2 Verify idempotent re-run: an integration test runs the task twice against the same DB state and asserts the derived table's row count is unchanged on the second run. *(Same real-run substitution as 3.1. Triggered the task a second time against the live stack: succeeded with the same `rows_written: 43860`, and `SELECT count(*) FROM precipitation_doubled` stayed at 43860 - no duplicates.)*

## 4. Docker wiring and manual end-to-end check

- [x] 4.1 Add bind-mount entries for both `backend` and `celery_worker` in `EDDIE/eddie_antarctica/docker-compose.override.yml`, mirroring the existing `eddie_alzette_ingest` entries exactly; verify `docker compose up -d` brings up all containers healthy with no new errors in `docker compose logs backend`.
- [x] 4.2 Manually trigger the task (`docker compose exec celery_worker ... celery ... call eddie_precip_interpolation.tasks.derive_precipitation`); verify by querying the derived table via `psql` and confirming its row count matches the source `sum_nn050` observation count. *(Done against the live stack: source had 43860 `sum_nn050` rows, `precipitation_doubled` ended up with 43860 rows after the run.)*
- [x] 4.3 Verify the published layer is discoverable end-to-end: `curl http://localhost:5000/terria-catalog.json` includes a catalog entry for the derived layer under the `input_layers` group. *(Confirmed: the response's "Input Layers" group includes `{"name":"precipitation_doubled","type":"wfs","typeNames":"input_layers:precipitation_doubled", ...}` - no frontend code touched.)*

## 5. Documentation

- [x] 5.1 Write `eddie_precip_interpolation/README.md` (layout, what it stores, how to run standalone against a local Postgres and as an EDDIE plugin), mirroring `eddie_alzette_ingest/README.md`'s structure; verify by following the documented standalone-run steps against a local Postgres and confirming they work as written.

## Workflow follow-up

- Archive this change once the team has reviewed the smoke-test result and decided whether/how to carry the derive-and-publish pattern forward into the real interpolation module.
