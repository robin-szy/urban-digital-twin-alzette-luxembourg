from pathlib import Path

import pyogrio
import yaml

from src.config import REPO_ROOT

# Table inside the land cover GDB mapping LC2024 codes to human-readable names.
LEGEND_LAYER: str = "LandCover_legend"
DEFAULT_GROUPS_PATH: Path = REPO_ROOT / "config/landcover_groups.yaml"


def load_legend(gdb_path: str | Path) -> dict[int, str]:
    # Read the code -> name table once; reused for plot labels and for the
    # class -> group mapping in Step 3.
    legend = pyogrio.read_dataframe(gdb_path, layer=LEGEND_LAYER)
    return dict(zip(legend["code"], legend["name"]))


def load_groups(path: str | Path = DEFAULT_GROUPS_PATH) -> dict[int, str]:
    # Explicit code -> group mapping (config/landcover_groups.yaml), a
    # modelling choice confirmed by Robin - not derived from the raw data.
    with open(path) as f:
        return yaml.safe_load(f)


def validate_groups(groups: dict[int, str], legend: dict[int, str]) -> None:
    # Catches the case where the legend gains a class (e.g. a future
    # landcover_year) that landcover_groups.yaml hasn't been updated for.
    missing: set[int] = set(legend) - set(groups)
    if missing:
        names = {code: legend[code] for code in missing}
        raise ValueError(f"Classes missing from landcover_groups.yaml: {names}")
