"""
Stage-damage curve: total and per-building flood cost for every slider stage (0-600 cm).

Run INSIDE the EDDIE backend container (it needs REM, the DEM and eddie_water_level):

    python /tmp/dyn/scripts/stage_damage_curve.py --out /tmp/dyn_out

Redraw the chart only, from a finished run's stage_damage_totals.csv (no EDDIE):

    python scripts/stage_damage_curve.py --replot outputs/stage_curve --monetary-dir <monetary run>

Offline (no EDDIE), from depths already computed by the same script (--save-depths):

    python scripts/stage_damage_curve.py --depths outputs/stage_curve/depth_by_stage.csv.gz --monetary-dir <dir> --out <dir>
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dynamic_flood_costs import inputs, outputs, parameters, stage_curve  # noqa: E402


def parse_stages(text):
    """'0:600:10' -> 0, 10, ..., 600; '100,200' -> 100, 200."""
    if ":" in text:
        start, stop, step = (int(v) for v in text.split(":"))
        return list(range(start, stop + 1, step))
    return [int(v) for v in text.split(",")]


def draw(out_dir, table, references):
    """Chart plus the stages where Model 1 reaches each static scenario total."""
    if not references:
        print("No static scenario totals found (damage_summary.csv missing in --monetary-dir?): "
              "chart drawn without scenario lines.")
    equivalents = stage_curve.equivalent_stages(table, references)
    if references:
        pd.DataFrame({"static_total_eur2010": references, "model1_equivalent_stage_cm": equivalents}) \
            .rename_axis("scenario").to_csv(out_dir / "scenario_equivalent_stages.csv")
    outputs.plot_stage_curve(out_dir, table, references, equivalents, parameters.CURVE_Y_MAX_EUR)
    for name, total in references.items():
        stage = equivalents[name]
        print(f"{name}: static total €{total:,.0f} -> Model 1 reaches it at "
              + (f"≈{stage:.0f} cm" if stage is not None else "no stage in range"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--monetary-dir", type=Path, default=inputs.DEFAULT_MONETARY_DIR)
    p.add_argument("--jrc-workbook", type=Path, default=None)
    p.add_argument("--stages", default=None, help="'0:600:10' (default: the slider's range) or '100,200'")
    p.add_argument("--durations", default=None, help="hours for Model 2, e.g. '6,24,72'")
    p.add_argument("--depths", type=Path, default=None,
                   help="offline: CSV buildingid, sample_index, stage_cm, depth_m instead of REM")
    p.add_argument("--save-depths", action="store_true", help="also write depth_by_stage.csv.gz")
    p.add_argument("--replot", type=Path, default=None,
                   help="folder of a finished run: redraw its chart from stage_damage_totals.csv only")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    references = inputs.load_scenario_totals(args.monetary_dir)
    if args.replot:
        table = pd.read_csv(args.replot / "stage_damage_totals.csv", index_col="stage_cm")
        draw(args.replot, table, references)
        return
    if args.out is None:
        p.error("--out is required unless --replot is given")

    stages = parse_stages(args.stages) if args.stages else list(parameters.CURVE_STAGES_CM)
    durations = [float(v) for v in args.durations.split(",")] if args.durations else list(parameters.CURVE_DURATIONS_HOURS)

    samples = inputs.load_samples(args.monetary_dir / "sample_damage_weights.csv.gz",
                                  args.monetary_dir / "building_flood_damage.csv")
    jrc = inputs.load_jrc(args.monetary_dir, args.jrc_workbook)
    print(f"{samples.index.get_level_values('buildingid').nunique()} buildings, {len(samples)} samples, "
          f"{len(stages)} stages, durations {durations} h, beta {parameters.DURATION_BETA}, "
          f"tau {parameters.DURATION_TAU_HOURS} h")

    if args.depths:
        long = pd.read_csv(args.depths, dtype={"buildingid": str})
        depth = long.pivot_table(index=["buildingid", "sample_index"], columns="stage_cm",
                                 values="depth_m", aggfunc="first").reindex(samples.index)
        depth = depth[[s for s in stages if s in depth.columns]]
    else:
        from dynamic_flood_costs import rem_depths  # pylint: disable=import-outside-toplevel
        depth = rem_depths.depths_at_stages(stages, samples.reset_index())

    results = stage_curve.curve(depth, samples, jrc, durations)
    args.out.mkdir(parents=True, exist_ok=True)
    table = stage_curve.totals(results)
    table.to_csv(args.out / "stage_damage_totals.csv")
    stage_curve.per_building(results).to_csv(args.out / "stage_damage_buildings.csv.gz", index=False)
    if args.save_depths:
        depth.stack(future_stack=True).rename("depth_m").reset_index().to_csv(
            args.out / "depth_by_stage.csv.gz", index=False)
    draw(args.out, table, references)
    print(table.to_string(float_format=lambda v: f"{v:,.0f}"))
    print(f"outputs written to {args.out}")


if __name__ == "__main__":
    main()
