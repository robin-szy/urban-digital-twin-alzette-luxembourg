# Devika: Alzette km 35–50 land use and ownership

Prepare reusable static layers for the Alzette river corridor, combining LIS-L
2024 land use with area-weighted context from the 2019 ownership grid. The AOI
extends 300 m on each side of the mapped river between kilometre markers 35 and 50.
Exports are 2D EPSG:4326 GeoJSON; intersection areas are calculated in EPSG:2169.

**Status:** national preparation/merge, the corridor, regional clipping and ownership
analysis have validated local outputs. Styles and popup configuration have also
been checked locally. EDDIE/PostGIS/GeoServer publication and browser/terrain
acceptance remain pending; nothing has been deployed by this work.

## Directory structure

| Path under `devika/` | Purpose |
| --- | --- |
| `src/` | Reusable preparation, AOI, classification, analysis and export modules |
| `scripts/` | Command-line entry points; processing runs independently of EDDIE |
| `config.yaml` | Shared defaults for direct land-use and ownership preparation |
| `config/` | Classification mapping, ownership labels, popup and catalog templates |
| `config/styles/` | Reviewed SLD and SVG presentation assets |
| `styles/` | Legacy scaffold; current assets are under `config/styles/` |
| `tests/` | Synthetic tests; no external datasets or services required |
| `data/`, `outputs/`, `results/` | Ignored local sources, generated layers and reports |
| `CATEGORY_INVENTORY.md` | Observed national classification counts and distinctions |
| `DEPLOYMENT_WINDOWS.md` | Pending Windows deployment runbook and acceptance checks |

## Environment and required inputs

Run all commands below in **Bash from the repository root** (the parent of
`devika/`). Use Python 3.11 or newer:

```bash
python3 -m venv devika/.venv
source devika/.venv/bin/activate
python -m pip install -r devika/requirements-dev.txt
```

Runtime requirements are GeoPandas, pandas/NumPy, Shapely, Pyogrio/GDAL, PyProj and
PyYAML; development requirements add pytest. Ranges are not a lockfile. The separate
land-cover exporter needs `lxml`, absent from requirements and unused here.

Sources are not transferred by Git. Supply these datasets at the indicated locations:

| Input | Required schema/identity |
| --- | --- |
| `data/landuse-luxembourg-07-15-18-21-24.gpkg` | Layer `LandUse_status_2024`; original classification fields including `LU1_level__3_name` and `LU1_level_3_code` |
| `data/raw/ownership/owner-type-grid.geojson` | Unique `TOP_LEFT`; `PUBLIC_PERCENT`, `Commune_percent`, `Etat_percent`, `Autre_percent`; preserve all additional fields |
| `kilometrage-des-cours-deau.gpkg` | EPSG:2169 point layer `Kilometrierung primäre Gewässer`, with `TOPONYME` and `KILO_KM`, including markers 35 and 50 |
| `gewassear-021025jso.json` | EPSG:2169 mapped river lines with `FIRST_TOPO` and `TOPONYME` |
| `config/landuse_categories.json` | Exact original code/label mapping, including classification whitespace |

Table paths are relative to `devika/`, except the river inputs: enter their absolute
paths at step 3. Compare input SHA-256 values with the existing validation reports.

## Configuration and path rules

Only `prepare_landuse.py` and `prepare_ownership.py` load `config.yaml`. They always
read it first, optionally merge an explicitly supplied YAML via
`--config devika/config.local.yaml`, then apply CLI overrides. The override YAML
filename is relative to the shell working directory; relative data/output paths
inside it resolve from `devika/`. Local overrides are not loaded automatically.

The base file leaves source paths and AOI/bbox unset. It specifies source metadata,
required fields, a 50,000-feature limit, GeoJSON/EPSG:4326 output, and 25 MiB file /
4 MiB feature limits. Source/output, AOI or bbox, and limits can be overridden with
existing CLI flags; bbox selection requires its CRS. Ownership additionally accepts
`--all-features` and `--percentage-tolerance`; land-use repair is enabled only by
`--repair-invalid`. The cadastral and EDDIE sections are unused by these commands.

The national, merge, corridor, regional clip and analysis commands do not load
this YAML. Their relative paths resolve from `devika/`, except corridor
`--markers` and `--river-lines`, which resolve from the working directory.
Use `--help` for each script's actual options. No command below publishes to EDDIE.

## Execution sequence

Use validated existing artifacts to resume at a later step. The commands below
write the established artifact names and refuse existing outputs/reports; for a
rerun, choose new destinations and update downstream references. National runs can
require substantial RAM, disk space and time; they process one original feature
at a time and do not establish national completeness beyond the supplied source.

### 1. Prepare the full supplied land-use source

```bash
OGR_GEOJSON_MAX_OBJ_SIZE=0 python devika/scripts/prepare_national_landuse.py \
  --source data/landuse-luxembourg-07-15-18-21-24.gpkg \
  --layer LandUse_status_2024 \
  --repair-invalid --max-feature-mb 128 \
  --output-directory outputs/luxembourg_landuse_2024_v3
```

This repairs invalid polygon parts, splits pieces, retains source attributes and
exports XY geometry without AOI filtering. It creates numbered GeoJSON files,
per-file `.report.json` sidecars and `manifest.json` in the output directory.
Only a validated manifest with all source records completed denotes success.

The known Main roads polygon is 81,787,865 bytes. The 128 MiB feature limit and
process-local GDAL object-size override accommodate it; the 512 MiB file limit
still applies. GDAL reads of these large files also need the override. Earlier
incomplete `luxembourg_landuse_2024/` and `luxembourg_landuse_2024_v2/` collections
are not valid merge inputs.

### 2. Supply the independent audit, then merge

**Reproducibility limitation:** merging requires
`outputs/luxembourg_landuse_2024_v3.audit.json`, but no versioned script or complete
procedure currently generates this independent audit. National preparation does
not create it. Obtain the existing audit for the exact validated collection;
a fresh raw-source run cannot complete this documented pipeline until the audit
procedure is supplied. The audit checks source FIDs/part lineage, attribute
preservation, geometry and totals; it must agree with the manifest and sidecars.

```bash
python devika/scripts/merge_national_landuse.py \
  --input-directory outputs/luxembourg_landuse_2024_v3 \
  --audit outputs/luxembourg_landuse_2024_v3.audit.json \
  --output outputs/luxembourg_landuse_2024.geojson \
  --expected-files 92 --expected-features 144319
```

The merge copies original feature JSON in manifest order without geometry edits,
then independently checks content/order hashes, geometry, counts and source
checksums. It produces the merged GeoJSON and
`outputs/luxembourg_landuse_2024.merge-report.json`. It does not use GDAL's JSON
reader, so this command needs no GDAL object-size override.

### 3. Construct the 300 m river corridor

Enter absolute paths without surrounding quotes; keep the same Bash session:

```bash
read -r -p 'Absolute path to kilometrage-des-cours-deau.gpkg: ' ALZETTE_MARKERS
read -r -p 'Absolute path to gewassear-021025jso.json: ' ALZETTE_RIVER_LINES
python devika/scripts/prepare_river_corridor.py \
  --markers "$ALZETTE_MARKERS" --river-lines "$ALZETTE_RIVER_LINES" \
  --start-km 35 --end-km 50 --half-width-m 300 \
  --output outputs/alzette_35_50_corridor_300m.geojson
```

The builder verifies the mapped route, ordered markers and route length, then
buffers it in EPSG:2169 with flat end caps. The sibling report records route checks,
input hashes, marker chainages, AOI area and bounds; the AOI follows the river.

### 4. Clip validated national land use to the corridor

```bash
python devika/scripts/clip_regional_landuse.py \
  --source outputs/luxembourg_landuse_2024.geojson \
  --source-report outputs/luxembourg_landuse_2024.merge-report.json \
  --aoi outputs/alzette_35_50_corridor_300m.geojson \
  --mapping config/landuse_categories.json \
  --output outputs/alzette_35_50_landuse.geojson
```

The stream is clipped in EPSG:2169, with no further repairs. Output pieces retain
original attributes and add source lineage, stable display IDs and classification
fields. The sibling report checks source evidence, hashes and AOI containment.
Original `Shape_Area`/`Shape_Length` remain source metadata, not clipped measurements.

### 5. Prepare ownership input and calculate regional estimates

Prepare the full ownership grid once, preserving complete footprints and all fields:

```bash
python devika/scripts/prepare_ownership.py \
  --source data/raw/ownership/owner-type-grid.geojson \
  --all-features --percentage-tolerance 0.01 \
  --output outputs/luxembourg_ownership_2019.geojson
python devika/scripts/analyse_landuse_ownership.py \
  --landuse outputs/alzette_35_50_landuse.geojson \
  --ownership outputs/luxembourg_ownership_2019.geojson \
  --aoi outputs/alzette_35_50_corridor_300m.geojson \
  --percentage-tolerance 0.01 \
  --output outputs/alzette_35_50_landuse_ownership.geojson
```

Both steps generate sibling `.report.json` files. The explicit tolerance preserves
one source value near 100.01% (`PUBLIC_PERCENT`, `TOP_LEFT=0071-0098`); percentages
are never clamped or normalised. Ownership preparation does not clip or repair
cells. Its report also flags 361 source footprints smaller than nominal 1 km² by
more than 1%, preserving them unchanged.

Analysis uses intersection-area weights in EPSG:2169, checks overlapping cells,
and reports overall and per-field valid-data coverage. Nulls are unavailable,
not zero. The output preserves regional geometries/IDs/properties and adds
estimates, calculated area, coverage and popup fields; its report includes source
preservation checks and independently recomputed sample polygons.

These are **grid-based estimates, not verified parcel ownership**. They assume
uniform composition within each footprint and combine 2019 ownership with 2024
land use. `PUBLIC_PERCENT` overlaps component categories: do not sum them.
`Autre` means Other and does not establish private ownership.

## Validated results and verification boundaries

| Artifact under `outputs/` | Validated local result |
| --- | --- |
| `luxembourg_landuse_2024_v3/manifest.json` | 92 source records; 144,319 polygon pieces; 189 repairs; all 11 attributes retained |
| `luxembourg_landuse_2024.merge-report.json` | 144,319 valid XY polygons; 494,886,519 GeoJSON bytes; original feature content/order preserved |
| `luxembourg_ownership_2019.report.json` | 2,790 complete footprints; all 25 source attributes retained |
| `alzette_35_50_corridor_300m.report.json` | One validated mapped-river corridor |
| `alzette_35_50_landuse.report.json` | 1,038 clipped polygons, spanning 32 national classification codes |
| `alzette_35_50_landuse_ownership.report.json` | 1,038 enriched polygons; full grid coverage within numerical tolerance; 12 percentage fields |

Reports, source datasets and outputs remain local and ignored. Check report
`status`, counts, input/output checksums and method parameters before reuse; failed
manifests and timestamped failure reports are diagnostic evidence, not success.
The merged national SHA-256 is
`b7316ceb00542ca2c728fae4ba74a7b163e44b5a4b04c78a598524226cf8852a`.
The [category inventory](CATEGORY_INVENTORY.md) records 44 exact labels and 46
code/label classifications; classification whitespace and code distinctions matter.

Run synthetic checks separately from real-data validation:

```bash
python -m pytest devika/tests -q
```

Tests do not establish service publication or browser behaviour. Land-cover tools
and the cadastral placeholder are separate from this land-use analysis.

## Presentation and pending EDDIE deployment

To rebuild presentation assets from the validated analysis without reprocessing data:

```bash
python devika/scripts/build_landuse_styles.py \
  --source outputs/alzette_35_50_landuse_ownership.geojson \
  --output-directory outputs/step3-style-validation
```

This writes `landuse.sld`, `landuse-legend.svg` and `style-validation.json` after
configuration/data checks, refusing existing destinations. Reviewed assets are in
`config/styles/`; popup, ownership-label and catalog JSON are in `config/`.
Structural checks and local Mustache rendering have been performed;
GeoServer acceptance, WMS GetFeatureInfo and browser clicks remain pending.
The custom popup hides source metadata by default; TerriaJS Show raw data can
still expose retained properties.

See [DEPLOYMENT_WINDOWS.md](DEPLOYMENT_WINDOWS.md) for the unexecuted deployment
runbook. Publication must ingest the derived layer, upload and assign its style,
host the legend, replace catalog placeholders and verify the WMS overlay over the
existing terrain. EDDIE's generated vector catalog uses WFS, so styled display
uses the explicit WMS item. The normal static directory is
`EDDIE/eddie_antarctica/src/static/geo/`; filename-based ingestion can replace tables.
Do not bulk-ingest or change the shared DEM/terrain setup as part of preparation.
Keep datasets, reports, machine-specific overrides and credentials out of Git.
