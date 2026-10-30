from pathlib import Path

import yaml

# Anchor for building absolute paths to data/results/config from anywhere in the repo.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def load_config(path: str | Path = REPO_ROOT / "config.yaml") -> dict:
    # Load the project-wide settings (river, km range, AOI method, CRS, ...).
    with open(path) as f:
        return yaml.safe_load(f)
