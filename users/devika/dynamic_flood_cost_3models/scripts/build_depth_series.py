"""
Build the depth time series for a period from the twin's gauge history and REM.

Run INSIDE the EDDIE backend container (needs the gauge history database and the DEM):

    python users/devika/dynamic_flood_costs/scripts/build_depth_series.py \
        --start 2021-07-14T00:00:00Z --end 2021-07-18T00:00:00Z \
        --monetary-dir <monetary stage> --out depth_series.csv.gz
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dynamic_flood_costs import inputs, rem_depths  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="ISO start, e.g. 2021-07-14T00:00:00Z")
    p.add_argument("--end", required=True, help="ISO end")
    p.add_argument("--monetary-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    samples = inputs.load_samples(args.monetary_dir / "sample_damage_weights.csv.gz",
                                  args.monetary_dir / "building_flood_damage.csv")
    series = rem_depths.sample_depth_series(args.start, args.end, samples.reset_index())
    series.to_csv(args.out, index=False)
    print(f"{series['timestamp'].nunique()} timesteps x {len(samples)} samples -> {args.out}")


if __name__ == "__main__":
    main()
