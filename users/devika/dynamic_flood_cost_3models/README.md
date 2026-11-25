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

**Team assumptions for Model 2:** β = 0.3 (a long flood does up to 30 % more damage than a
short one at the same depth) and τ = 24 h (about 63 % of that extra after one day of standing
water, 95 % after three days), for every building class. No literature source yet.

## Stage-damage curve: cost as the water level changes

The front end's water-level slider sets the same stage (0–600 cm) at every gauge, and REM
draws the water. `scripts/stage_damage_curve.py` computes the cost for each of those stages:

- **Model 1**: JRC damage at that level (one level has no history, so its depth is the peak);
- **Model 2**: the same level held for 6, 24 and 72 hours (β = 0.3, τ = 24 h).

It writes `stage_damage_totals.csv` (one row per stage), `stage_damage_buildings.csv.gz`
(per building and stage), `scenario_equivalent_stages.csv` and, if matplotlib is installed,
`stage_damage_curve.png`.

The chart goes up to €30M and draws the static model's HQ10, HQ100 and HQextreme totals
(from the run's `damage_summary.csv`, when present) as dashed lines, with the stage at which
Model 1 reaches each one. A second panel zooms on the low stages where those totals are reached.
The match is on cost, not on water level: "Model 1 at ≈277 cm" means the slider curve reaches
the HQ100 total there, not that HQ100 is a 277 cm stage.

To redraw the chart from a finished run without EDDIE:

```powershell
python scripts/stage_damage_curve.py --replot outputs/stage_curve --monetary-dir ..\..\..\data\flood_costs\runs\postgis-depth-2026-11-08\monetary_damage
```

### Running it against the twin (Windows, PowerShell, from the repository root)

Read-only for EDDIE: the folder is copied into the backend container's `/tmp`, nothing in
the image, the compose files or `/app/data` changes.

```powershell
docker cp users\devika\dynamic_flood_cost_3models backend_digital_twin:/tmp/dyn
docker exec backend_digital_twin python /tmp/dyn/scripts/stage_damage_curve.py --out /tmp/dyn_out --save-depths
docker cp backend_digital_twin:/tmp/dyn_out users\devika\dynamic_flood_cost_3models\outputs\stage_curve
```

Defaults: the monetary run `/app/data/flood_costs/runs/postgis-depth-2026-11-08/monetary_damage`
(sample weights, building classes, `jrc_parameters.json`), stages 0–600 cm in 10 cm steps.
Change them with `--monetary-dir`, `--stages 0:600:20` or `--durations 6,24,72`.

With `--save-depths` the REM depths are kept, so the curve can be recomputed later without
EDDIE (for other β, τ or durations):

```powershell
python scripts/stage_damage_curve.py --depths outputs/stage_curve/depth_by_stage.csv.gz --monetary-dir <local copy of the monetary run> --out outputs/stage_curve_offline
```

Inside the container the flood-cost module is mounted as `eddie_flood_costs`; in the
repository it is `flood_costs`. `jrc_source.py` imports whichever exists.

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
  parameters.py            assumptions: wet threshold, β = 0.3, τ = 24 h, curve stages/durations,
                           unit disruption costs (PLACEHOLDERS)
  jrc_source.py            imports the team's flood-cost module as flood_costs or eddie_flood_costs
  stage_curve.py           cost per slider stage (Model 1, Model 2 for fixed durations)
  core.py                  shared steps: Δt, running max, hours wet, JRC fraction, euros per building, wet rule
  model1_peak_envelope.py  Model 1
  model2_duration.py       Model 2
  model3_disruption.py     Model 3
  inputs.py                reading the existing outputs, the depth series and the Model 3 attributes
  rem_depths.py            REM depths at the sample points: per slider stage, or over a period (inside EDDIE)
  outputs.py, cli.py       writing results; command-line arguments
scripts/
  stage_damage_curve.py    cost vs slider stage (inside EDDIE, or offline from saved depths)
  build_depth_series.py    depths over a period of stored gauge readings, inside the EDDIE backend container
  run_model1.py, run_model2.py, run_model3.py
tests/                     synthetic checks; no data or services needed
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
- **β = 0.3, τ = 24 h are team assumptions** without a literature source; c_disp, c_bus are placeholders.
- **The stage-damage curve has not been run inside EDDIE yet.**
- **Model 3 attributes** (`residents`, `floor_area_m2`) need a source; residents could come from
  `eddie_building_population`.
- **Depth method differs from the static model.** Static HQ10/HQ100 depths use the reconstructed
  water surface over the 5 m DEM; these use REM over the 1 m grid. A flood peaking at HQ100
  levels will give similar, not identical, damage.
- **Scope.** `eddie_flood_costs` deliberately excludes indirect losses and live monetary
  calculation. Model 3 and the dynamic results are new scope, to agree with the team before
  moving anything into the shared package.
