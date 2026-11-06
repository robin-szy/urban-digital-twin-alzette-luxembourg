"""Flood-cost plugin and external workflow paths; no data loaded on import.

Path helpers live here to keep the agreed six-module application layout. The
lookup and Flask API do not import the optional geospatial dependencies.
"""
import ctypes.util
import json
import os
from pathlib import Path

__version__ = "0.2.0"

_INPUT_DEFAULTS = {
    "dem": "dem/dem_merged.tif",
    "buildings": "buildings/ACT2023v2_Buildings3d_Footprints.gpkg",
    "markers": "river/kilometrage-des-cours-deau.gpkg",
    "hq10": "flood_hazard/hwgk2021-hq10.geojson",
    "hq100": "flood_hazard/hwgk2021-hq100.geojson",
    "landuse": "landuse/alzette_35_50_landuse_ownership.geojson",
    "jrc_workbook": "jrc/copy_of_global_flood_depth-damage_functions__30102017.xlsx",
    "review_snapshot": "classification_review",
}
_STAGE_DEFAULTS = {
    "depth": "depth_reconstruction",
    "osm": "osm_classification",
    "candidates": "classification_candidates",
    "monetary": "monetary_damage",
    "lookup": "lookup",
}


def _resolve(value, base):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


class WorkflowPaths:
    """Resolve explicit external inputs and stages, without opening datasets.

    FLOOD_COSTS_PROJECT_ROOT or FLOOD_COSTS_PATH_CONFIG is required. JSON input
    paths are relative to project_root; stage paths are relative to output_root.
    An existing local project_paths.json (inputs/stages) is supported when an
    explicit project root is supplied. Historical provenance aliases are unused.
    """

    def __init__(self):
        config_name = os.environ.get("FLOOD_COSTS_PATH_CONFIG")
        config_path = Path(config_name).expanduser().resolve() if config_name else None
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else {}
        if not isinstance(config, dict):
            raise ValueError("Flood-cost path configuration must be a JSON object.")
        root = os.environ.get("FLOOD_COSTS_PROJECT_ROOT") or config.get("project_root")
        if not root:
            raise ValueError("Set FLOOD_COSTS_PROJECT_ROOT to an external data project, or configure project_root in FLOOD_COSTS_PATH_CONFIG.")
        self.project_root = _resolve(root, config_path.parent if config_path else Path.cwd())
        self.output_root = _resolve(
            os.environ.get("FLOOD_COSTS_OUTPUT_ROOT") or config.get("output_root", "outputs"), self.project_root)
        self.inputs_root = _resolve(os.environ.get("FLOOD_COSTS_INPUTS_ROOT", "inputs"), self.project_root)
        self.inputs = config.get("inputs", {})
        self.stages = config.get("stages", {})
        if not isinstance(self.inputs, dict) or not isinstance(self.stages, dict):
            raise ValueError("Configuration inputs and stages must be JSON objects.")

    def input_path(self, name):
        override = os.environ.get("FLOOD_COSTS_INPUT_" + name.upper())
        if override:
            return _resolve(override, self.project_root)
        if name in self.inputs:
            return _resolve(self.inputs[name], self.project_root)
        if name == "classification_manifest":
            return self.stage_path("osm") / "target_manifest.json"
        if name == "classification_candidates":
            return self.stage_path("candidates") / "building_jrc_osm_classification.csv"
        return self.inputs_root / _INPUT_DEFAULTS[name]

    def stage_path(self, stage):
        value = os.environ.get("FLOOD_COSTS_STAGE_" + stage.upper()) or self.stages.get(stage, _STAGE_DEFAULTS[stage])
        return _resolve(value, self.output_root)

    def guard_output(self, path):
        """Generated workflow artifacts must remain outside Git and this module."""
        path = Path(path).resolve()
        module_root = Path(__file__).resolve().parents[1]
        if path.is_relative_to(module_root) or any((p / ".git").exists() for p in (path, *path.parents)):
            raise ValueError("Generated outputs must stay outside Git checkouts and the installed module.")


def refuse_overwrite(paths):
    existing = [str(p) for p in paths if Path(p).exists()]
    if existing:
        raise FileExistsError("Existing outputs are protected; select a fresh external stage: " + ", ".join(existing))


def gdal_library_path(pyogrio_file):
    """Locate the read-only sampler's GDAL library, as in the local workflow."""
    configured = os.environ.get("FLOOD_COSTS_GDAL_LIBRARY")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        return str(path)
    package = Path(pyogrio_file).resolve().parent
    for directory in (package / ".dylibs", package / "libs", package.parent / "pyogrio.libs"):
        for pattern in ("*gdal*.dylib", "*gdal*.so*", "*gdal*.dll"):
            matches = sorted(directory.glob(pattern))
            if matches:
                return str(matches[0])
    library = ctypes.util.find_library("gdal")
    if library:
        return library
    raise RuntimeError("No GDAL shared library located. Set FLOOD_COSTS_GDAL_LIBRARY.")
