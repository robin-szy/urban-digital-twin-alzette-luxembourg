"""EDDIE compatibility mapping; optional offline wheel build in a temporary copy."""
import importlib
import importlib.util
from pathlib import Path
import pkgutil
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_package_configuration_matches_canonical_and_eddie_import_names():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packaging = config["tool"]["setuptools"]
    assert config["project"]["name"] == "eddie_flood_costs"
    assert packaging["packages"] == ["flood_costs", "eddie_flood_costs"]
    assert packaging["package-dir"] == {"eddie_flood_costs": "flood_costs"}
    assert "flood_costs" in {name for _, name, _ in pkgutil.iter_modules([str(ROOT)])}
    for name in packaging["packages"]:
        source = ROOT / packaging["package-dir"].get(name, name)
        for required in ("__init__.py", "blueprint.py", "tasks.py", "cost_lookup.py",
                         "depth_reconstruction.py", "damage_calculation.py", "popup_context.py"):
            assert (source / required).is_file()
    assert "flask>=3.0" in config["project"]["dependencies"]
    assert importlib.import_module("flood_costs.tasks").__name__ == "flood_costs.tasks"


def test_built_wheel_exposes_eddie_discovery_name_without_extra_source_package(tmp_path):
    missing = [name for name in ("setuptools", "wheel", "pip") if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip("Offline wheel check requires installed build tools: " + ", ".join(missing))
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info", "build", "dist"))
    wheels = tmp_path / "wheels"
    # No network or build isolation; build metadata is written only in the copy.
    result = subprocess.run([
        sys.executable, "-B", "-m", "pip", "--disable-pip-version-check", "--no-cache-dir",
        "wheel", "--no-index", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(source),
    ], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = wheels.glob("*.whl")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for package in ("flood_costs", "eddie_flood_costs"):
            for filename in ("__init__.py", "blueprint.py", "tasks.py", "cost_lookup.py", "popup_context.py"):
                assert f"{package}/{filename}" in names
        assert not any(name.startswith("tests/") for name in names)
        archive.extractall(installed)
    script = """
import importlib, importlib.util, pkgutil, sys
sys.path.insert(0, sys.argv[1])
discovered = {name for _, name, _ in pkgutil.iter_modules([sys.argv[1]]) if name.startswith('eddie_')}
assert discovered == {'eddie_flood_costs'}
for name in ('flood_costs', 'eddie_flood_costs'):
    package = importlib.import_module(name)
    assert package.__version__ == '0.2.0'
    importlib.import_module(name + '.tasks')
    importlib.import_module(name + '.cost_lookup')
    if importlib.util.find_spec('flask') is not None:
        blueprint = importlib.import_module(name + '.blueprint').blueprint
        assert blueprint.name == 'eddie_flood_costs'
        assert blueprint.url_prefix == '/flood-costs'
"""
    imported = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(installed)],
                              capture_output=True, text=True, timeout=30)
    assert imported.returncode == 0, imported.stdout + imported.stderr
