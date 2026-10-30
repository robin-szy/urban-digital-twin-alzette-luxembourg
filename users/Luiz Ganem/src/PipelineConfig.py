"""
Pipeline configuration (TOML, see config/config.toml).

Every script fills its constants block from load(). The file used is,
in order: the one selected with use() (RunPipeline.py does this before
importing the other scripts), the environment variable
DEM_PIPELINE_CONFIG, or config/config.toml.
"""

import os
import tomllib
from pathlib import Path


DEFAULT_CONFIG = Path("config/config.toml")
ENV_VAR = "DEM_PIPELINE_CONFIG"

_selected = None


def use(path):

    # Select the config for every script imported after this call
    global _selected
    _selected = Path(path)
    os.environ[ENV_VAR] = str(_selected)


def config_path():

    return Path(_selected or os.environ.get(ENV_VAR) or DEFAULT_CONFIG)


def load(path=None):

    path = Path(path) if path is not None else config_path()

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path} (run from the project root, "
            f"or pass a config path)"
        )

    with open(path, "rb") as f:
        return tomllib.load(f)


def optional_path(value):

    # "" in the config means "not set"
    return Path(value) if value else None
