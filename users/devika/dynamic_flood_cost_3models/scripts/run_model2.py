"""Model 2 - duration-dependent damage. Usage: see README.md."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dynamic_flood_costs import cli, model1_peak_envelope, model2_duration, outputs  # noqa: E402


def main():
    args = cli.parser(__doc__).parse_args()
    print("Model 2 - duration-dependent damage (beta, tau are placeholders)")
    samples, depth, jrc = cli.load(args)

    model1 = model1_peak_envelope.compute(depth, samples, jrc)
    model2 = model2_duration.compute(depth, samples, jrc)
    series = {"model2_damage_eur2010": model2, "model1_damage_eur2010": model1}

    effect = (100 * (model2.iloc[:, -1] / model1.iloc[:, -1] - 1)).rename("duration_effect_pct")
    out = outputs.write(args.out, series, final_extra=effect.to_frame())
    outputs.plot(out, series, "Model 2: duration adds damage on top of Model 1")
    print(f"  outputs written to {out}")


if __name__ == "__main__":
    main()
