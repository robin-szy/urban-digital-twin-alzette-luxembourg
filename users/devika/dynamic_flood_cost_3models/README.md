# Dynamic flood costs (experimental)

Three building-level flood-cost models that **evolve over time during a flood**, built on
top of the team's `eddie_flood_costs` package. Experimental: not an EDDIE plugin, not
installed into the twin, and not validated on a real event yet.

| Model | Equation | Adds |
|---|---|---|
| 1. Peak-envelope JRC | D_i(t) = V_c Σ_j w_ij f_c(max_{τ≤t} h_ij(τ)) | irreversibility: the deepest water so far |
| 2. Duration-dependent | D_i(t) = V_c Σ_j w_ij min(1, f_c(h_max) [1 + β_c (1 − e^(−d/τ_c))]) | time underwater d |
| 3. Direct + disruption | C_i(t) = D_i(t) + Σ Δt · 1[wet_i] (p_i c_disp + F_i c_bus) / 24 | displacement and business interruption |

Amounts are **2010 EUR**, like the static model.

## What is reused from the repository

| Need | Reused from | How |
|---|---|---|
| JRC curves f_c and Luxembourg V_c | `eddie_flood_costs/flood_costs/damage_calculation.py` | `read_jrc()` on the JRC 2017 workbook |
| Curve interpolation (0–6 m, saturated above) | same module | `fraction()` |
| Sample points and Voronoi weights w_ij | monetary stage output | `sample_damage_weights.csv.gz` |
| Accepted building classes | monetary stage output | `building_flood_damage.csv` (`jrc_category`, `accepted_provisional_classification`) |
| Water levels over time | `gauge_collector` history (since 2017, every 15 min) | `eddie_water_level.blueprint.water_levels(start, end)` |
| Depth at each timestep | REM, 1 m grid | `rem.compute_level(key)` (or `flood_grid.compute_level` when not using REM) |

Only the time logic (running maximum, hours wet, duration factor, disruption) is new.

## Unknown values stay unknown

As in `eddie_flood_costs`, a missing value is **never treated as zero**:

- a sample whose depth is missing at some timestep has an unknown maximum depth and
  unknown hours wet from then on;
- a building's cost is reported only when every sample with a positive weight is known;
  otherwise it is empty in the CSVs;
- totals add up known buildings only, and `unknown_buildings` counts the rest.

## Files

```
dynamic_flood_costs/
  parameters.py            assumptions: wet threshold, β, τ, unit disruption costs (PLACEHOLDERS)
  core.py                  shared steps: Δt, running max, hours wet, JRC fraction, euros per building, wet rule
  model1_peak_envelope.py  Model 1
  model2_duration.py       Model 2
  model3_disruption.py     Model 3
  inputs.py                reading the existing outputs, the depth series and the Model 3 attributes
  rem_depths.py            depth series from gauge history + REM (runs inside EDDIE)
  outputs.py, cli.py       writing results; command-line arguments
scripts/
  build_depth_series.py    step 1, inside the EDDIE backend container
  run_model1.py, run_model2.py, run_model3.py
tests/test_dynamic_models.py   synthetic checks; no data or services needed
```

## Running

From this folder:

```bash
# Tests (needs eddie_flood_costs' compute dependencies: numpy, pandas, geopandas, shapely)
python -m pytest

# 1. Depth series for a period (inside the EDDIE backend container)
python scripts/build_depth_series.py --start 2021-07-14T00:00:00Z --end 2021-07-18T00:00:00Z \
    --monetary-dir <monetary stage> --out depth_series.csv.gz

# 2. Models (anywhere with the inputs)
python scripts/run_model1.py --monetary-dir <monetary stage> --depth-series depth_series.csv.gz \
    --jrc-workbook <JRC workbook> --out outputs/model1
python scripts/run_model2.py ... --out outputs/model2
python scripts/run_model3.py ... --attributes building_attributes.csv --direct-from model1 --out outputs/model3
```

`--jrc-workbook` can be left out when the `FLOOD_COSTS_*` variables are set (see
`eddie_flood_costs/README.md`). `outputs/` is ignored by git.

Each model writes `building_cost_timeseries.csv`, `total_cost_timeseries.csv`,
`building_cost_final.csv` and, if matplotlib is installed, `total_cost_over_time.png`.

## Open items

- **Not yet run on real data.** `build_depth_series.py` follows the code paths used by the
  day replay and the 3D buildings, but has not been run inside EDDIE.
- **β, τ, c_disp, c_bus are placeholders** without a source (`parameters.py`).
- **Model 3 attributes** (`residents`, `floor_area_m2`) need a source; residents could come from
  `eddie_building_population`.
- **Depth method differs from the static model.** Static HQ10/HQ100 depths use the reconstructed
  water surface over the 5 m DEM; these use REM over the 1 m grid. A flood peaking at HQ100
  levels will give similar, not identical, damage.
- **Scope.** `eddie_flood_costs` deliberately excludes indirect losses and live monetary
  calculation. Model 3 and the dynamic results are new scope, to agree with the team before
  moving anything into the shared package.
