# eddie_base_data

The PostGIS side of the module pipeline, shared by `eddie_building_population` and `eddie_flood_costs`:

```
[ raw files ] --ingest once--> [ base_* tables, EPSG:2169 ]        ingest.py, datasets.py
                                      |
                       resolve_aoi(conn, dataset)                  query.py   <- the AOI seam
                                      |
                       AOI-filtered query, ST_Intersects in SQL    query.py
                                      |
                       module: GeoDataFrames in -> values out      the module
                                      |
                       write_derived(engine, table, frame, scope)  publish.py
                                      |
                       [ derived table, EPSG:2169, reach/variant columns ]
                                      |
                       publish_view: <table>_4326 -> GeoServer     publish.py
                                      |
                       Terria catalog (lists every input_layers layer)
```

## Ingest

`ingest_base_data` runs at every worker start and can be sent by hand. It reads a source only if its
SHA-256 differs from the one in `base_data_manifest` (`force=True` overrides that). Every table is
EPSG:2169 and 2D, with lowercase columns and a GiST index. A source whose coordinates land outside
Luxembourg is refused, which catches a wrong CRS or swapped axes.

| table | source | used by |
|---|---|---|
| `base_buildings_inspire` | `src/static/geo/BU.Building.gml` (EPSG:3035) | population |
| `base_buildings_lu00` | `src/static/geo/LU00.gpkg` (EPSG:3035) | population: heights, types |
| `base_communes` | `src/static/geo/LIMADM_COMMUNES.shp` | population |
| `base_commune_population` | `src/static/geo/rnpp_population_per_municipality.csv` | population |
| `base_buildings_act` | `data/raw/Buildings/ACT2023v2_Buildings3d_Footprints.gpkg` | flood costs |
| `base_flood_extent_hq10`, `_hq100` | `data/raw/Flood_hazard/HWGK2021_HQ*/*.shp`, one row per polygon | flood costs |
| `base_river_km_primary`, `_secondary` | `src/static/geo/kilometrage-des-cours-deau.gpkg` | AOI, flood costs |

These stay files: the DEM (raster, read through a scoped window) and the JRC workbook (validated cell by
cell, with its hash recorded).

EDDIE's own startup task (`add_base_data_to_db`) separately loads every `.geojson` and `.shp` in
`src/static/geo` as it is, on every restart, and stops at the first file it cannot read. That is why
`LIMADM_COMMUNES` also exists as a table with its original column case. Modules read `base_communes`.

## Connecting the AOI

Replace the body of `query.resolve_aoi` with the reach/variant resolver (openspec `aoi-reach-variants`,
group 8). It returns an `AoiScope`; `write_derived` stamps its `reach_id`, `variant_key`, `degraded` and
`degraded_reason` on every row and replaces only the rows of that scope. No module changes.

GeoServer stores a layer's bounding box when it creates the layer, so it needs recalculating once a
table holds a reach outside the first one.

## Tests

Run them inside the worker container, because its library versions are the ones in force:

```sh
docker cp eddie_base_data/tests celery_worker_digital_twin:/tmp/base_tests
docker exec -w /app celery_worker_digital_twin /venv/bin/python -m pytest -p no:cacheprovider /tmp/base_tests
```

`test_publish_integration.py` uses the live PostGIS, on a scratch table it removes afterwards.
