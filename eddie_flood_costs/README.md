# EDDIE flood costs

Provisional HQ10, HQ100 and HQextreme building losses for Alzette km 35–50.
The API reads validated precomputed results; requests never reconstruct depths
or calculate monetary damage. Amounts are **2010 EUR**. Unknown damage is JSON
`null`; a calculated zero is numeric `0.0`. Scenario totals are alternatives and
must not be added together or described as complete AOI losses.

The package extends the team implementation at `upstream/main` commit `0ba0f0d`.
`flood_costs` is the canonical import; the installed `eddie_flood_costs` alias uses
the same source directory for EDDIE's plugin discovery. Register its blueprint once.
The original research project and all generated datasets remain external.

## Code and interfaces

| File | Responsibility |
|---|---|
| `flood_costs/__init__.py` | Lazy external paths, frozen-baseline checksums, output protection and GDAL discovery. No data loads on import. |
| `flood_costs/depth_reconstruction.py` | Scoped DEM sampling, three extents, original reference functions, retained comparison cohort and depth/sample preservation. `main(inputs=None)` accepts file or PostGIS inputs. |
| `flood_costs/damage_calculation.py` | JRC workbook parsing, interior-area weighting, classification/approval validation, preserved legacy results and HQextreme valuation. |
| `flood_costs/cost_lookup.py` | Standard-library schema 1/2 validation, source reconciliation, immutable-by-interface lookup and external builder. |
| `flood_costs/popup_context.py` | Exact-ID join to the SHA-matched monetary GeoJSON, without sending geometry. |
| `flood_costs/blueprint.py` | Existing read-only Flask routes and file/database lookup selection. |
| `flood_costs/postgis_inputs.py` | Existing base-table readers, including `base_flood_extent_hqextreme`. |
| `flood_costs/tasks.py` | Existing four named Celery tasks; fresh calculation stages and read-only schema preflight before publishing. |
| `scripts/build_cost_lookup.py` | Thin CLI wrapper using the package builder. |
| `scripts/apply_classification_review.py` | Historical frozen-review validation/replay; `--check-only` also validates the active approval extension. It does not reapprove proposals. |
| `tests/` | Synthetic source files/workbook, numerical checks, version compatibility, Flask/SQLite contracts, PostGIS adapter I/O substitutes, popup JavaScript and offline wheel discovery. |

The Celery task names remain `eddie_flood_costs.tasks.run_depth_reconstruction`,
`load_depths_to_db`, `run_damage_calculation` and `load_flood_costs_to_db`.
Importing them outside EDDIE registers no tasks. Calling them inside EDDIE is an
explicit compute/publication action, separate from serving a saved release.

## Inventory and compatibility

The completed inventory has **507 original building IDs** and **271 accepted
provisional classifications**: 252 Residential and 19 Commercial. There are
144 additional pending classifications, 32 original unresolved buildings and
60 excluded buildings. These counts describe the verified external release;
they are not substitutes for validating its records.

The original 276-building cohort retains every saved HQ10/HQ100 property,
geometry, depth sample and monetary audit contribution. Its original 184 accepted
classifications remain unchanged. The 87 additional approvals (78 Residential,
9 Commercial) authorize HQextreme valuation only. Their HQ10/HQ100 damage stays
`null` with `not_assessed` and `legacy_scenario_unassessed_amount_preserved_null`.
All approval dates, confidence, evidence, conflicts and proxy limits are retained.
Approval is not independent verification of building use.

| Lookup version | Available scenarios | Behaviour |
|---|---|---|
| `1.0.0` | `hq10`, `hq100` | Existing documents, summary shape, monetary statuses and route contracts remain valid. An HQextreme request returns 400 for this dataset. |
| `2.0.0` | `hq10`, `hq100`, `hqextreme` | Adds pending-classification scope/counts and explicitly scoped `not_assessed`. Null is never imputed as zero. |

The builder detects the version from saved scenario columns and reconciles CSV,
GeoJSON, validation counts and saved summaries. It does not convert a v1 source
into invented HQextreme data. Dataset versions remain content hashes. Keep the
original v1 lookup as an immutable rollback artifact.

## Calculation method and dependencies

The frozen reference functions and gauge constants remain embedded from reference
commit `769cfc4ed85f7f56caca951807de4e2d0d9dc863`. The study area extends the original
HQ100 500 m building corridor with the HQextreme corridor. It retains the same
km 35–50 markers, tributary rules, 300 m scenario cells and 3,000 m extent crop.
Complete footprints are sampled at 1 m spacing over the supplied 5 m DEM.
Depth is `max(0, reconstructed WSE - DEM)`, masked by strict official-extent
interior. Missing ground or ambiguous cell assignments remain unknown.
Positive-only depth means exclude dry samples; they are not flooded-area fractions.
Configured baseline profiles, original coordinates, ground observations and legacy
samples are reused exactly. A fresh depth run without a baseline is supported,
but cannot claim preservation of an existing comparison cohort.

JRC European Residential/Commercial/Industrial curves and Luxembourg structure
plus standard contents totals are read from the October 2017 workbook's stored
XML values. Interpolate each sample's curve over 0–6 m and saturate above 6 m.
Interior observations receive clipped Voronoi weights, with deterministic cell
ordering compatible with Shapely 2.0/GEOS 3.11. Boundary samples carry no valuation
area; missing interior samples retain unknown area. Shared slivers are allocated
first to the original approved cohort, then to new approvals in ID order.
A full estimate requires complete weighted coverage within the original tolerance.
Diagnostic known-area contributions are never presented as full-building losses.
The original cohort's saved weights and legacy money are reused exactly.

Python 3.11+ and Flask are required for the API. The `compute` extra supports the
existing EDDIE image: GeoPandas >=0.14, NumPy >=1.26, Pandas >=2.1, PyProj >=3.6,
Shapely >=2.0 and its installed GDAL library. Pyogrio, rasterio and GEOS 3.12 are
not required. SQLAlchemy and Celery are supplied by EDDIE for its database/tasks.
Tests use Flask in-process and SQLite for the lookup fallback; no server is started.

DEM vertical datum/units and finished-floor levels remain unverified. These are
provisional model estimates, without inflation, depreciation, specialist contents,
indirect losses, annual expected losses or live/manual-stage monetary calculation.
Keep attribution: © OpenStreetMap contributors, ODbL. JRC methodology:
Huizinga et al. (2017), [doi:10.2760/16510](https://doi.org/10.2760/16510).

## External inputs and configuration

| Setting | Meaning |
|---|---|
| `FLOOD_COSTS_PROJECT_ROOT` | Explicit external research/data project; required unless JSON supplies `project_root`. |
| `FLOOD_COSTS_PATH_CONFIG` | Optional external JSON containing `project_root`, `output_root`, `inputs`, `stages`, `baseline_root`. Existing research configuration is supported with an explicit project root. Historical absolute aliases are not imported. |
| `FLOOD_COSTS_BASELINE_ROOT` | Verified pre-extension backup containing `manifest.json` and `outputs/{depth_reconstruction,monetary_damage,osm_classification,...}`. Required for preserved monetary reproduction. |
| `FLOOD_COSTS_INPUTS_ROOT` | Default `<project_root>/inputs`. |
| `FLOOD_COSTS_INPUT_<KEY>` | Explicit input override; keys include DEM, BUILDINGS, MARKERS, HQ10, HQ100, HQEXTREME, JRC_WORKBOOK and REVIEW_SNAPSHOT. |
| `FLOOD_COSTS_OUTPUT_ROOT` | Fresh external calculation root, normally `<project_root>/outputs`. Existing products cannot be overwritten. |
| `FLOOD_COSTS_STAGE_<STAGE>` | Explicit depth, monetary, lookup, candidates or OSM stage. Absolute paths are accepted. |
| `FLOOD_COSTS_STAGE_OSM` | Active accepted master/ledger, independent of monetary output staging. Relative paths used for this active classification root resolve under `<project_root>/outputs`. Default `osm_classification`. |
| `FLOOD_COSTS_GDAL_LIBRARY` | Optional GDAL shared library; the Docker worker retains `/venv/lib/libgdal.so`. |
| `FLOOD_COSTS_LOOKUP_PATH` | Optional API lookup, supplied through Flask config or environment. Empty selects the existing database lookup fallback. |
| `FLOOD_COSTS_DETAILS_PATH` | Optional API monetary GeoJSON. Its exact bytes must match the lookup's source SHA-256. |

Input defaults are `dem/dem_merged.tif`,
`buildings/ACT2023v2_Buildings3d_Footprints.gpkg`,
`river/kilometrage-des-cours-deau.gpkg`, `flood_hazard/hwgk2021-hq10.geojson`,
`flood_hazard/hwgk2021-hq100.geojson`, `flood_hazard/hwgk2021-hqext.geojson`,
`jrc/copy_of_global_flood_depth-damage_functions__30102017.xlsx` and
`classification_review/`, under the inputs root. Explicit JSON input paths resolve
under the project root. Other relative stage paths resolve under the output root.
Environment overrides take precedence. No data is discovered from `devika/`.

The active classification stage must contain the approved master, reviewed CSV,
append-only decision ledger, target manifest and `hqextreme_review/` proposals,
frozen evidence and decisions. Retain the original `inputs/classification_review/`
snapshot and pre-87 backup referenced by the approval ledger. Changing the output
root never recreates or replaces approvals. Research OSM acquisition/review tools
remain in the research project and are not run by the API or calculation jobs.

For an explicit future reproduction, using an existing compute interpreter:

```sh
export PYTHONPATH=/path/to/repository/eddie_flood_costs
export FLOOD_COSTS_PROJECT_ROOT=/external/flood_costs_local
export FLOOD_COSTS_PATH_CONFIG=/external/flood_costs_local/scripts/project_paths.json
export FLOOD_COSTS_BASELINE_ROOT=/external/flood_costs_local/backups/hq10_hq100_BASELINE_ID
export FLOOD_COSTS_OUTPUT_ROOT=/external/fresh-run
export FLOOD_COSTS_STAGE_OSM=/external/flood_costs_local/outputs/osm_classification
python -B -m flood_costs.depth_reconstruction
python -B /path/to/repository/eddie_flood_costs/scripts/apply_classification_review.py --check-only
python -B -m flood_costs.damage_calculation
python -B -m flood_costs.cost_lookup \
  --source-dir /external/fresh-run/monetary_damage \
  --output /external/fresh-run/lookup/eddie_flood_costs_lookup_v2.json
```

To reuse saved depths, set `FLOOD_COSTS_STAGE_DEPTH` and omit reconstruction.
Run with assertions enabled; do not use Python `-O`. All CLIs support `--help`.
Generated outputs are rejected inside Git or the installed module. Monetary outputs
include CSV/GeoJSON, summary, validation, methodology, JRC parameters, sample
contributions and the pending-classification review CSV. They remain outside Git.

## API and popup

The Flask blueprint name/prefix remain `eddie_flood_costs` and `/flood-costs`.

| GET route | Response |
|---|---|
| `/summary/<scenario>.json` | Validated classified-subset totals/counts and actual schema/dataset metadata. |
| `/buildings/<buildingid>/<scenario>.json` | One original ID, amount/status, classification and quality flags. |
| `/popup/<buildingid>.json` | Available scenarios, costs and optional verified whole-building area/depth/exposure; no geometry or scenario totals. |

Scenarios are case-insensitive. Known unknowns return 200/null; absent IDs return
404/null. Invalid or dataset-unavailable scenarios return 400. An invalid explicitly
configured lookup returns 503 without silently choosing database data. Optional
context mismatch suppresses area/depth/exposure and reports a warning, while
validated costs remain available. File snapshots use the existing path/mtime/size
cache; database snapshots retain the existing dataset-version cache.

`terriajs/flood-cost-popup.js` joins exact `properties.buildingid`, supports v1/v2
payloads and all three comparison switches, and resets defaults when the active
map scenario/building changes. It reads `AlzetteTwin.getState()` when available.
Map controls remain exclusive; popup comparisons remain independent. Null and
unassessed amounts display N/A, zero displays €0.00. Provisional proxies and
boundary-only positive samples retain warnings. It preserves selection recovery,
request caching, fallback templates and Casual-mode integration. Multipart CZML
entity IDs/areas never stand in for the original financial building ID/area.
The map-wide damage summary now also accepts HQextreme.

## Dynamic flood costs (Models 1-3, stage curve)

Built like the static costs: inputs from the database, Celery tasks, results in PostGIS, a
read-only API. Code in `flood_costs/dynamic/`; the maths is moved unchanged from
`users/devika/dynamic_flood_cost_3models` (beta = 0.3, tau = 24 h for every class; the Model 3
unit costs are placeholders).

| Step | Where | What |
|---|---|---|
| Inputs | `load_flood_costs_to_db` (automatic) or task `publish_dynamic_inputs` | the published run's sample weights and JRC parameters -> `flood_cost_sample_weights`, `flood_cost_jrc_parameters` |
| Depths | backend, `GET /flood-costs/dynamic/depths.json` | the twin's water model (`eddie_water_level`, REM) at every sample; only the backend has the DEM/REM |
| Stage curve | task `run_dynamic_curve` (or `POST /flood-costs/dynamic/curve/run`) | cost per slider stage 0-600 cm: Model 1, Model 2 held 6/24/72 h -> `flood_cost_dynamic_curve(_buildings)` |
| Event | task `run_dynamic_event(start, end)` (or `POST /flood-costs/dynamic/event/run`) | Models 1, 2, 3 over stored gauge readings -> `flood_cost_dynamic_event*` |
| API | `GET /flood-costs/dynamic/...` | `curve.json`, `stage/<cm>.json`, `stage/<cm>/<id>.json`, `building/<id>/curve.json`, `events.json`, `event/<id>.json`, `event/<id>/<building>.json[?time=]`, `parameters.json` |

The tasks run in the worker and fetch depths from the backend (`FLOOD_COSTS_BACKEND_URL`,
default `http://backend:5000`). Every result carries the run's `dataset_version`; a result of
an older static run is answered as `stale`. Unknown depths, classes or attributes stay null,
never 0. Model 3 reads residents, footprint and floors from `building_population` (read only).
The published run stays the km 35-50 run for now; on another reach the API shows its buildings
inside that reach only (`partial_reach`), as the static popup does.

## Release deployment and rollback (separate approval)

1. Retain the currently served lookup, context, settings and image revision.
   Prepare a new immutable folder under ignored `data/flood_costs/releases/RELEASE_ID/`.
   Include the validated v2 lookup and its exact matching `building_flood_damage.geojson`.
   Retain an external manifest of filenames, SHA-256 values, 507 IDs, 271 approvals
   and unrounded three-scenario totals. Raw inputs, evidence, workbooks, approval
   ledgers, samples, intermediate CSV/GeoJSON and monetary outputs stay external.
2. Validate with `CostLookup.load` and `PopupDetails.for_building` for every original
   ID before publication. Do not reformat or reserialize the context GeoJSON.
   Reconcile the original HQ10/HQ100 cohort and all approval decisions to backups.
3. Deploy compatible code before switching data. The backend mounts
   `data/flood_costs` read-only; the worker's existing writable run mount remains.
   Set both API paths to the same immutable release directory and recreate only
   the backend as a coordinated change. Empty lookup paths retain database mode.
   Test positive, zero, pending, excluded, unassessed, absent-ID and v1 rollback cases.
4. Refresh the frontend assets (popup v2, water control v23) through the existing
   Docker build/entrypoint. Confirm HQ10/HQ100/HQextreme selection, an already-open
   popup, multiple comparison switches and zero/null formatting in the browser.
   Verify map packet availability for all required IDs; financial inventory coverage
   alone does not prove that all 507 buildings are rendered by the existing 3D layer.
5. If publishing computed tables, first ingest the HQextreme base dataset and plan
   an explicit additive migration for existing depth/cost attributed tables. The
   preflight refuses missing new columns or unattributed legacy tables before the
   shared publisher writes anything. It never drops teammates' tables to upgrade.
   The existing database fallback and task names are retained. No migration is
   needed to serve the external file release.
6. Roll back by restoring both previous API paths/settings and the previous image
   if needed. Keep old release bytes and v1 lookup intact. Never update one live
   artifact in place while leaving the other from a different run.

No deployment, service restart, database mutation or Git publication is part of
local implementation/testing. Dockerfiles, catalogs and unrelated `devika/` code
need no integration edits.

## Tests

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest eddie_flood_costs/tests -q -p no:cacheprovider
```

Use an interpreter with the compute/test extras, Flask and SQLAlchemy. Popup tests
use an existing Node executable or `EDDIE_POPUP_JS_ENGINE=/path/to/jsc`; they do
not download a JS runtime. The wheel test builds offline in a temporary copy.
Synthetic tests never discover research data, query OSM or start EDDIE/Docker.
A separate reproduction can select saved external depths and fresh temporary
monetary output to compare all 507 properties/geometries and audit contributions.
