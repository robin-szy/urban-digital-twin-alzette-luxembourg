"""Model 1 - peak-envelope JRC. Usage: see README.md."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dynamic_flood_costs import cli, model1_peak_envelope, outputs  # noqa: E402


def main():
    args = cli.parser(__doc__).parse_args()
    print("Model 1 - peak-envelope JRC")
    samples, depth, jrc = cli.load(args)

    series = {
        "model1_damage_eur2010": model1_peak_envelope.compute(depth, samples, jrc),
        "current_depth_exposure_eur2010": model1_peak_envelope.compute_current_depth_version(depth, samples, jrc),
    }
    out = outputs.write(args.out, series)
    outputs.plot(out, series, "Model 1: direct damage vs current-depth exposure")
    print(f"  outputs written to {out}")


if __name__ == "__main__":
    main()
