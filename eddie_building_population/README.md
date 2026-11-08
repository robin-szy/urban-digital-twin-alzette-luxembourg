# eddie_building_population

EDDIE plugin made from Pegah's code in `users/Pegah/src/` (copied 2026-11-07): building heights,
dasymetric population per building, and the non-residential water-fee model.

Task `eddie_building_population.tasks.compute_building_population` reads the `eddie_base_data` tables,
runs the model and writes the PostGIS table `building_population` (EPSG:2169, one result per
`reach_id` + `variant_key`). GeoServer serves it as `input_layers:building_population_4326`.

The AOI comes from `eddie_base_data.query.resolve_aoi`. Until the reach resolver is connected it is
Pegah's rectangle: the km 35–50 markers' bounding box plus 500 m (`variant_key` = `bbox_500`).

## Changes from `users/Pegah/src/`

`population_model.py` stays as close to Pegah's as possible: `calculate_population_and_heights` takes
GeoDataFrames instead of file paths, and the first two fixes below are its only other changes. `aoi.py`, `aoi_processor.py`, `config.py` and `exporters.py` were dropped because the base tables
and the 4326 view replace them. `simple_economic_model.py` is unchanged.

Three fixes change the numbers. All three were invisible on synthetic data, and the same defects are in
`users/Pegah/src/population_model.py`:

| fix | before | why |
|---|---|---|
| Population joined to communes by name | joined `COMMUNE_CODE` to `LAU2` | They are different numberings: 64 codes coincide, all of them for different communes, and 36 communes get no match. Luxembourg City received Esch-sur-Alzette's population. Names match 100 of 100. |
| `hommes_majeurs` counted | code looked for `hommes_majeures`, and skipped columns it did not find | The 283,201 adult men in the register were left out. |
| A commune's population is divided over all its buildings, then filtered to the AOI | divided over the AOI's buildings only | The whole commune's population landed inside the AOI, and a building's estimate changed with the AOI. This fix is in `tasks.py`, which also checks that each commune's estimates add up to its register total, so a commune without a population row stops the run. |

The water-fee pool (`total_water_volume` × `percentage`) is still divided among the non-residential
buildings of the AOI, as before, so `allocated_water_volume` and `total_cost` do depend on the AOI.
