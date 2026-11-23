"""Command-line arguments and input loading shared by the run_model*.py scripts."""

import argparse
from pathlib import Path

from . import inputs


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--monetary-dir", type=Path, required=True,
                   help="eddie_flood_costs monetary stage: holds sample_damage_weights.csv.gz "
                        "and building_flood_damage.csv")
    p.add_argument("--depth-series", type=Path, required=True,
                   help="CSV from scripts/build_depth_series.py")
    p.add_argument("--jrc-workbook", type=Path, default=None,
                   help="JRC 2017 workbook; default: resolved by eddie_flood_costs configuration")
    p.add_argument("--out", type=Path, required=True, help="output folder for this model")
    return p


def load(args) -> tuple:
    """(samples, depth, jrc) from the parsed arguments."""
    samples = inputs.load_samples(args.monetary_dir / "sample_damage_weights.csv.gz",
                                  args.monetary_dir / "building_flood_damage.csv")
    depth = inputs.load_depth_series(args.depth_series, samples)
    jrc = inputs.load_jrc(args.jrc_workbook)
    print(f"  {samples.index.get_level_values('buildingid').nunique()} buildings, "
          f"{len(samples)} samples, {depth.shape[1]} timesteps")
    return samples, depth, jrc
