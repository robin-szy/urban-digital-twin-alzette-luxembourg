# EDDIE flood costs backend

Provisional HQ10/HQ100 reconstructed depths and building monetary damage for the
Alzette km 35–50 study area. The read-only API serves a validated, precomputed
lookup; it never runs geospatial processing during a request. Unknown damage is
JSON `null`; a calculated zero is numeric `0.0`. Amounts are in 2010 euros.

This is a reorganisation of the existing validated local calculations, with
external configuration. The original plugin and `flood_costs_local/` are retained.

## Files

```text
eddie_flood_costs/
├── pyproject.toml
├── flood_costs/
│   ├── __init__.py
│   ├── blueprint.py
│   ├── tasks.py
│   ├── cost_lookup.py
│   ├── depth_reconstruction.py
│   └── damage_calculation.py
├── scripts/
│   ├── build_cost_lookup.py
│   └── apply_classification_review.py
├── tests/
│   ├── conftest.py
│   ├── test_cost_lookup.py
│   ├── test_damage_calculation.py
│   ├── test_depth_reconstruction.py
│   └── test_packaging.py
└── README.md
```

| File | Purpose |
|---|---|
| `pyproject.toml` | Package metadata, API and optional computation dependencies, installed plugin compatibility name, and the two utility scripts. |
| `flood_costs/__init__.py` | Version, lazy `WorkflowPaths` configuration, overwrite guard and GDAL library locator. Uses only the standard library and loads no datasets on import. |
| `flood_costs/blueprint.py` | Module-level Flask `blueprint`, read-only routes, lazy lookup loading, cache and error responses. |
| `flood_costs/tasks.py` | Required EDDIE task-loader interface; registers no tasks and starts no processing. |
| `flood_costs/cost_lookup.py` | Unchanged validated `CostLookup`, source reconciliation, summary calculation, external lookup writer and builder CLI. Standard library only. |
| `flood_costs/depth_reconstruction.py` | Adapted `reconstruct_depths.py`; `WindowedDEM`, reconstruction, sample assignment and summaries. Embeds the ten required frozen reference functions and gauge constants. |
| `flood_costs/damage_calculation.py` | Adapted `calculate_damage.py`; `read_jrc`, `fraction`, `build_weights`, `weighted_estimate`, validation, damage outputs and methodology report. |
| `scripts/build_cost_lookup.py` | Thin CLI wrapper around `flood_costs.cost_lookup.main`; no duplicate lookup implementation. |
| `scripts/apply_classification_review.py` | Validate/replay the six frozen reviewed classification artifacts; `--check-only` performs no writes. |
| `tests/conftest.py` | Synthetic saved-result/curve/workbook fixtures and isolation from inherited research configuration. |
| `tests/test_cost_lookup.py` | Reused lookup/API tests, adapted to the canonical import and five synthetic buildings instead of local datasets. |
| `tests/test_damage_calculation.py` | JRC parsing/interpolation, area weighting, dry/unknown losses, partial flooding and calculated scenario aggregation. |
| `tests/test_depth_reconstruction.py` | Sample assignment, summaries, in-memory DEM sampling and actual two-building HQ10/HQ100 reconstruction with synthetic inputs. |
| `tests/test_packaging.py` | Canonical/EDDIE source mappings and an optional offline wheel/discovery check using a temporary source copy. |
| `README.md` | Inputs, methodology, configuration, commands, API contract and integration limits. |

## External input datasets

The default layout below is relative to an explicitly configured external data
project. It matches the preserved `flood_costs_local/` layout; no input or generated
monetary file is distributed with this module.

| Input key | Default under `inputs/` | Existing source and use |
|---|---|---|
| `dem` | `dem/dem_merged.tif` | Luiz's supplied 5 m EPSG:2169 DEM; terrain elevations, with vertical datum still unverified. |
| `buildings` | `buildings/ACT2023v2_Buildings3d_Footprints.gpkg` | ACT Luxembourg national 3D building footprints; original `buildingid`, complete 2D geometry and area. Layer `ACT2023v2_Buildings3d_Footprints`. |
| `markers` | `river/kilometrage-des-cours-deau.gpkg` | Official Luxembourg river kilometrage, primary and secondary watercourse layers (`Kilometrierung primäre Gewässer`, `Kilometrierung sekundäre Gewässer`). |
| `hq10`, `hq100` | `flood_hazard/hwgk2021-hq10.geojson`, `flood_hazard/hwgk2021-hq100.geojson` | Existing AGE official flood extent polygons, explicitly labelled EPSG:2169. They are not official depth rasters. |
| `landuse` | `landuse/alzette_35_50_landuse_ownership.geojson` | Devika's processed corridor output, used upstream for dominant-landuse classification. Configurable for provenance and the external research workflow; neither calculation reruns that upstream process. |
| `jrc_workbook` | `jrc/copy_of_global_flood_depth-damage_functions__30102017.xlsx` | JRC Global Flood Depth–Damage Functions, October 2017 European curves and Luxembourg valuations. |
| `review_snapshot` | `classification_review/` | Frozen approved decisions and resulting classification tables/reports, treated as human-reviewed inputs. |

Luxembourg source publisher: [data.public.lu](https://data.public.lu/). Exact local
source identities/hashes remain in the research project's provenance reports.
JRC methodology: Huizinga, de Moel and Szewczyk (2017), EUR 28552 EN,
[doi:10.2760/16510](https://doi.org/10.2760/16510). Classification evidence uses the
frozen OpenStreetMap responses retained locally, including the November 3
Hesperange snapshot. Attribution: © OpenStreetMap contributors,
[ODbL](https://www.openstreetmap.org/copyright).

The damage calculation additionally requires the three depth products and
`validation_report.json`, reviewed `provisionally_classified_buildings.csv`,
`building_jrc_osm_classification.csv`, `classification_review_decisions.json`, and
`target_manifest.json`. These remain external. The lookup builder requires four
monetary products: `building_flood_damage.csv`, `building_flood_damage.geojson`,
`damage_summary.csv`, and `damage_validation.json`.

## Reconstruction methodology

`depth_reconstruction.main()` preserves the validated workflow:

1. Construct the Alzette km 35–50 corridor and tributary cells using river-marker
   Voronoi cells. The study area uses the 500 m building corridor widened by the
   nearby HQ100 extent. Scenario water cells use a 300 m corridor and include
   official extent within 1,000 m of the river. Extents retain the original
   3,000 m marker-bounds crop.
2. Reconstruct each scenario's water-surface elevation from the median DEM height
   sampled every 5 m along extent edges in each cell. Smooth offsets from the
   gauge-zero baseline across five cells, interpolate missing offsets and enforce
   upstream nondecreasing water levels. Tributaries take confluence levels.
3. Read a scoped, north-up EPSG:2169 DEM window through the installed GDAL library;
   never load the national raster in full. Sample complete building footprints
   along rings and on a 1 m interior grid. Duplicate source IDs are dissolved.
4. Assign samples strictly within water cells, accepting boundary matches only
   for a unique chainage. Ambiguous assignments remain unknown. Compute
   `max(0, WSE - DEM)` and mask by strict interior of the official scenario extent.
   Outside-extent samples become zero only when DEM and WSE are both valid.
5. Save full footprints, sample records and validation. Positive-only min/mean/max
   are null when no samples are wet. The all-valid mean includes known dry zeros.
   Missing observations are counted rather than replaced by zero.

The embedded reference functions are `water_cells`, `_tributary_cells`,
`scenario_wse`, `_scenario_profile`, `_is_stage`, `wse_from_gauges`, `_wse_profile`,
`study_area`, `_extent_status`, and `_footprint_points`. Their ASTs and decorators
are preserved from the local snapshots at reference commit
`769cfc4ed85f7f56caca951807de4e2d0d9dc863`. `load_reference_functions()` compiles
only this bundled source in the original isolated namespace, with configured data
paths and the scoped DEM adapter. No excluded file is opened or imported.
`_REFERENCE_SOURCE_SHA256` identifies the original complete reference snapshots,
not this adapted module. The six frozen gauge position/zero constants are embedded
unchanged. No live gauge request is needed for HQ reconstruction.

## JRC damage and approved classifications

`damage_calculation.read_jrc()` reads exact stored workbook XML values and cached
formula totals. It selects European Residential, Commercial and Industrial curves
on `Damage functions` and Luxembourg building-based totals at `D117` in each
`MaxDamage-<category>` sheet. It verifies structure `B117` plus standard contents
`C117` against the total. Piecewise linear interpolation applies over 0–6 m;
greater depths saturate at the last fraction, with quality flags.

`build_weights()` assigns clipped Voronoi areas to strictly interior observations.
Boundary observations do not generate valuation cells. Invalid interior samples
retain their unknown areas. Shared footprint slivers belong once to the first
classified building in sorted ID order; exported geometries remain unchanged.
Weights are corrected to the original stored footprint area after coordinate
round-trip. `weighted_estimate()` applies the damage curve at each sample before
spatial averaging and multiplies by the category maximum €/m². It never applies a
curve to a single mean depth. Known dry area stays in the denominator.

A full building estimate requires no material unknown weighted area: tolerance is
`max(1e-6 m², valuation_area × 1e-9)`, with some known area. Otherwise full damage
stays null; a diagnostic known-area contribution is not included in scenario
totals. Totals use saved unrounded valid amounts, without hardcoded losses.
There is no additional contents factor, depreciation, floor multiplier or inflation.

The frozen cohort contains 276 buildings: 174 Residential landuse proxies, ten
Commercial buildings, 32 unresolved in the corridor and 60 excluded outside it.
Of 42 non-residential review targets, seven high-confidence Commercial mappings
were retained; three medium-confidence service/recreation/cultural proxies were
explicitly approved. Four medium-confidence proposals were held unresolved.
No Industrial building was accepted, although its JRC parameters are read and its
category is represented in summaries. Confidence, decisions and evidence remain
unchanged. The museum uses standard Commercial contents, not specialist collections.

`apply_classification_review.validate_review()` checks the frozen evidence,
decisions, category counts, IDs, areas and exposure statuses before replay.
Its six replayed files are the review decisions JSON, reviewed building CSV,
accepted inventory CSV, exposure-summary CSV, classification-summary JSON and
classification-report Markdown. It does not collect OSM or infer new decisions.
With `--check-only`, a missing candidate CSV is reconstructed from the frozen
pre-review columns for internal consistency checks; that mode does not independently
verify fresh OSM evidence. Replay mode requires the actual pre-review candidate CSV.

## Dependencies and configuration

Python 3.11+ and Flask are required for the API. Computation uses GeoPandas, NumPy,
Pandas, Pyogrio/GDAL, PyProj and Shapely with ordered Voronoi support (GEOS 3.12+).
The local validated environment records GeoPandas 1.2.0, NumPy 2.4.6, Pandas 2.3.3,
Pyogrio 0.13.0, PyProj 3.8.0 and Shapely 2.2.0. Workbook reading uses the standard
library; rasterio, Excel engines and Celery are not required by this module.
The `test` extra adds pytest and local wheel-building tools. The full synthetic
suite requires the `compute` extra as well; Flask tests use only Flask's in-process
test client, without starting a server or loading EDDIE.

An eventual installation into a separately selected backend environment is:

```sh
python -m pip install /path/to/eddie_flood_costs
# Include geospatial dependencies only for computation:
python -m pip install '/path/to/eddie_flood_costs[compute]'
```

These are future installation instructions; no backend or shared environment was
changed during reorganisation. For local source execution, use an existing compute
interpreter and set `PYTHONPATH` as below. Imports alone open no data/config files.

| Setting | Meaning |
|---|---|
| `FLOOD_COSTS_PROJECT_ROOT` | External data-project root. Required unless an external JSON supplies `project_root`. There is no repository-relative data fallback. |
| `FLOOD_COSTS_PATH_CONFIG` | Optional external JSON. Keys: `project_root`, `output_root`, `inputs`, `stages`. Existing research `project_paths.json` inputs/stages also work with an explicit project root; historical aliases are unused here. |
| `FLOOD_COSTS_INPUTS_ROOT` | Default input root, normally `<project_root>/inputs`. Relative values resolve under project root. |
| `FLOOD_COSTS_OUTPUT_ROOT` | Output/stage root, normally `<project_root>/outputs`; choose a fresh external folder for new calculations. |
| `FLOOD_COSTS_INPUT_<KEY>` | Override any input, e.g. `FLOOD_COSTS_INPUT_DEM`, `FLOOD_COSTS_INPUT_JRC_WORKBOOK`, `FLOOD_COSTS_INPUT_LANDUSE`. |
| `FLOOD_COSTS_STAGE_<STAGE>` | Override `DEPTH`, `OSM`, `CANDIDATES`, `MONETARY` or `LOOKUP`; allows reuse of frozen external stages while writing a different fresh stage. |
| `FLOOD_COSTS_INPUT_CLASSIFICATION_MANIFEST` | External `target_manifest.json`; defaults to the OSM stage file. Necessary when the review-output stage does not contain the upstream manifest. |
| `FLOOD_COSTS_INPUT_CLASSIFICATION_CANDIDATES` | External pre-review candidate CSV; defaults to the candidates stage CSV. |
| `FLOOD_COSTS_GDAL_LIBRARY` | Optional explicit GDAL shared library; otherwise locate the Pyogrio wheel or system library. |
| `FLOOD_COSTS_LOOKUP_PATH` | External precomputed JSON for the API, from Flask config or the environment. Independent of workflow configuration; no default lookup. |

For workflow paths, environment overrides take precedence over JSON, then defaults.
Explicit JSON input paths are relative to project root and take precedence over
the default inputs root. JSON stage paths and stage overrides are relative to
output root; absolute external paths are accepted. A JSON `project_root` is relative
to the configuration file; JSON `output_root` is relative to project root.

Example external configuration (store it outside the repository):

```json
{
  "project_root": "/external/flood-data",
  "output_root": "/external/new-flood-run",
  "inputs": {
    "dem": "inputs/dem/dem_merged.tif",
    "classification_manifest": "outputs/osm_classification/target_manifest.json"
  },
  "stages": {
    "osm": "/external/flood-data/outputs/osm_classification"
  }
}
```

Generated outputs are rejected inside Git checkouts or the installed module.
Existing products are protected from overwriting. Run with assertions enabled
(`-B` is fine; do not use `-O`, since the original validation relies on `assert`).

## Run the existing calculations

These commands are for an explicitly requested future reproduction. Reconstruction
and full damage calculation were not rerun during this reorganisation. Substitute
actual external paths; do not select a frozen result folder as a write destination.

```sh
export FLOOD_MODULE="/path/to/urban-digital-twin-alzette-luxembourg/users/devika/eddie_flood_costs"
export FLOOD_PYTHON="/path/to/an/existing/compute-environment/bin/python"
export PYTHONPATH="$FLOOD_MODULE"
export FLOOD_COSTS_PROJECT_ROOT="/external/flood_costs_local"
export FLOOD_COSTS_OUTPUT_ROOT="/external/new-flood-run"
# Reuse the already approved classification stage; it is read only here.
export FLOOD_COSTS_STAGE_OSM="$FLOOD_COSTS_PROJECT_ROOT/outputs/osm_classification"

"$FLOOD_PYTHON" -B -m flood_costs.depth_reconstruction
"$FLOOD_PYTHON" -B "$FLOOD_MODULE/scripts/apply_classification_review.py" --check-only
"$FLOOD_PYTHON" -B -m flood_costs.damage_calculation
```

Reconstruction writes `building_reconstructed_depths.geojson`, its CSV,
`building_depth_samples.csv.gz` and `validation_report.json` under the depth stage.
Damage calculation writes `building_flood_damage.geojson`, its CSV,
`damage_summary.csv`, `damage_validation.json`, `methodology.md`,
`jrc_parameters.json` and `sample_damage_weights.csv.gz` under the monetary stage.
For an existing depth run, set `FLOOD_COSTS_STAGE_DEPTH` to that external stage
and omit reconstruction. Either calculation supports `--help`.

Optional replay to a fresh review stage (requires the actual candidate CSV produced
by the excluded local research scripts):

```sh
export FLOOD_COSTS_INPUT_CLASSIFICATION_MANIFEST="$FLOOD_COSTS_PROJECT_ROOT/outputs/osm_classification/target_manifest.json"
export FLOOD_COSTS_INPUT_CLASSIFICATION_CANDIDATES="/external/pre-review/building_jrc_osm_classification.csv"
export FLOOD_COSTS_STAGE_OSM="$FLOOD_COSTS_OUTPUT_ROOT/osm_classification"
"$FLOOD_PYTHON" -B "$FLOOD_MODULE/scripts/apply_classification_review.py"
```

The manifest remains an external input; replay copies only the six agreed files.
The local `prepare_osm_review.py`, `inspect_osm_matches.py`,
`classify_reviewed_matches.py`, `fetch_overpass.py` and `refresh_hesperange.py`
remain in the research project. They are needed for new evidence/cohorts, not for
reading approved results. This module does not silently regenerate their outputs.

## Build and use the lookup

```sh
"$FLOOD_PYTHON" -B "$FLOOD_MODULE/scripts/build_cost_lookup.py" \
  --source-dir "$FLOOD_COSTS_OUTPUT_ROOT/monetary_damage" \
  --output "$FLOOD_COSTS_OUTPUT_ROOT/lookup/eddie_flood_costs_lookup_v1.json"
```

The same CLI is available as `python -B -m flood_costs.cost_lookup`. Explicit
`--source-dir`/`--output` select the paths; with stage overrides, substitute their
actual locations. The builder reconciles IDs and classification/status/amounts
between CSV and GeoJSON, saved validation counts and every scenario/category
summary. It validates null/zero distinctions, records SHA-256 provenance and leaves
the four sources unchanged. It refuses an existing output, a destination inside
the source-results folder, or a destination in a Git checkout. Schema is `1.0.0`.

```python
from flood_costs.cost_lookup import CostLookup

lookup = CostLookup.load("/external/new-flood-run/lookup/eddie_flood_costs_lookup_v1.json")
lookup.damage("ACT_<original-id>", "hq10")  # number, including 0.0, or None
lookup.building_cost("ACT_<original-id>", "hq100")  # classification, status, flags, amount
lookup.summary("hq100")  # validated totals and counts, derived from building records
```

## EDDIE discovery and read-only API

The existing plugin documentation records EDDIE's `eddie_*` package-name discovery
and imports of `<plugin>.blueprint` and `<plugin>.tasks`. A canonical package named
only `flood_costs` would not satisfy that prefix. `pyproject.toml` therefore maps
the same source directory to both installed names: `flood_costs` for new imports
and `eddie_flood_costs` for compatibility. A normal wheel installation must provide
both names; the outer distribution name alone is insufficient. No extra source
package or change to EDDIE is required. Register the blueprint once, through either
the loader or the canonical import, not both.

This compatibility mapping requires installation: adding the source root to
`PYTHONPATH` exposes only the physical `flood_costs` package. A later backend rollout
must replace the old installed distribution rather than leave two copies of that
distribution installed. No installation, backend discovery change, service restart,
Docker change or deployment is performed here. The loader itself was not available
in this workspace for a fresh inspection.

`flood_costs.blueprint.blueprint` retains Flask blueprint name `eddie_flood_costs`
and URL prefix `/flood-costs`. Configure `FLOOD_COSTS_LOOKUP_PATH` in Flask config
or the environment. Lookup snapshots are validated lazily and cached by resolved
path, nanosecond modification time and size; discovery loads no financial data.

| GET route | Expected response |
|---|---|
| `/flood-costs/summary/<scenario>.json` | `status`, `scenario`, `total_eur2010`, represented/accepted/calculated/positive/zero/unknown counts, completeness, category breakdown, version/currency/year/provisional/scope metadata. |
| `/flood-costs/buildings/<buildingid>/<scenario>.json` | `status`, original ID, scenario, `found`, `damage_eur2010`, `monetary_status`, quality flags, category/acceptance/scope/confidence/source/review metadata and version/currency/year/provisional/scope metadata. |

Scenarios normalize to `hq10` and `hq100` (case insensitive). An existing unknown
building returns HTTP 200 with null damage. A missing ID returns 404, `found=false`,
`status="not_found"`, null damage and `monetary_status="missing_building"`.
Invalid scenarios return 400. Missing configuration, unreadable or invalid lookups
return 503 with null damage, without exposing the configured path. Flask supplies
HEAD/OPTIONS; POST/PUT/PATCH/DELETE are not registered. Unknowns must display as
N/A and must never be converted to zero by a client.

## Limitations and later 3D connection

- The original damage/review validations deliberately enforce the frozen
  276-building cohort and classification counts. Configuration relocates that
  workflow; it does not turn it into an arbitrary-area national processor.
- DEM elevation units/metre assumption and vertical datum are unverified. Water
  levels are reconstructed from extents/terrain, not official calibrated depths.
  There are no finished-floor elevations, basement, connectivity or defence models.
- A 1 m sample grid adds no resolution to the 5 m DEM. Interior quadrature can miss
  narrow flooded edge areas; officially exposed buildings can have calculated zero.
  Scenario-order exceptions are flagged, never forced to agree.
- Classifications are proxies with retained confidence and review limits. Totals
  cover complete estimates for the classified subset, not complete AOI losses,
  annual expected losses or additive losses across scenarios. There are no indirect
  losses, inflation adjustments, uncertainty intervals, HQ Extreme or arbitrary
  live/manual-stage monetary calculations.
- Later, join these endpoints/lookup records to Luiz's existing 3D building
  packets by `properties.buildingid`. Multipart visualization IDs may repeat one
  original building: display one whole-building estimate consistently and aggregate
  once per original ID. Do not join using unstable `building-{index}` display IDs.
  A future adapter/UI must preserve geometry, colours, water controls and epochs,
  refresh joins after scenario changes, label nulls N/A, and avoid showing HQ losses
  as live/manual-stage losses. No CZML adapter or frontend integration is implemented.

Generated lookup/monetary outputs contain linkable building-level financial
estimates and remain external. The tree above contains application code, metadata,
documentation and synthetic tests, with no source datasets or generated outputs.

## Local verification of this reorganisation

The initial reorganisation verified syntax/imports, original function ASTs, all ten
embedded reference functions, the four CLI help commands, frozen classifications,
small numeric method checks, and in-memory equality with the saved 276-building
lookup. All 82 original research/plugin files retained their bytes and metadata.
That reconciliation is historical verification, not a dependency of the test suite.

The repository test suite is entirely synthetic: it generates five saved
building-result records, a minimal XLSX XML archive, small polygons/arrays and a
two-building reconstruction. It never discovers research datasets, downloads
anything, queries an external API, or uses Docker. An autouse fixture clears
inherited `FLOOD_COSTS_*` settings for each test. All generated files live under
pytest temporary directories. The original lookup tests are reused with imports
and cohort-specific expectations adapted to the synthetic fixture; none of the
tests needs the original plugin or `flood_costs_local/` on disk.

In a separately prepared environment, install test dependencies and run from any
directory (these commands do not change EDDIE or Docker):

```sh
python -m pip install '/path/to/eddie_flood_costs[compute,test]'
python -B -m pytest -c "$FLOOD_MODULE/pyproject.toml" "$FLOOD_MODULE/tests" \
  -q -ra -p no:cacheprovider
```

| Coverage | Synthetic checks |
|---|---|
| HQ10 / HQ100 | Both lookups, saved-result reconciliation, reconstruction outputs, scenario-specific calculated totals. |
| Zero / null | Dry valid damage is numeric zero; missing depth, unresolved/excluded classifications and absent IDs stay null. |
| JRC | Published-table layout parsing with synthetic Luxembourg structure/contents values; curve knots, interpolation, saturation and invalid depths. No claim that fixture coefficients are official valuations. |
| Partial flooding | Official extent mask, known dry sample areas, curve applied before averaging, boundary exclusion, Voronoi conservation, overlap allocation and missing multipart support. |
| Aggregation | Independently expected scenario totals from calculated synthetic building amounts; unknown contributions excluded and completeness retained. |
| Plugin compatibility | Distribution name, canonical source package, installed `eddie_*` mapping, task interface and optional wheel import/discovery check. |

Current result: **52 passed, 3 skipped**, with two deprecation warnings from frozen
reference code using `shapely.vectorized.contains`. The source mapping matches the
documented EDDIE prefix/task/blueprint convention. Flask, setuptools and wheel are
absent in the current environment; dependencies were not downloaded. The skipped
tests are the two Flask HTTP tests and the offline wheel-build check. All runnable
tests pass without using the original result datasets.

The wheel test uses `--no-index --no-deps --no-build-isolation`, builds only a
temporary source copy, checks both import names and discovers the `eddie_*` name.
With Flask installed, it also checks both blueprint interfaces. The actual wheel
mapping remains unverified until that test runs in a prepared environment; adding
the source directory to `PYTHONPATH` still exposes only `flood_costs`. The EDDIE
loader is unavailable here, so no live EDDIE integration is claimed. The frozen
Shapely helper will need compatibility review if a future version removes it;
its calculation code was not changed in this test update.
