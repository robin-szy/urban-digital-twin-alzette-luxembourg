"""
Workspace housekeeping ([workspace] in config/config.toml):

    copy_to_repo = false  ->  clear: delete everything the scripts created
                              that is switched on in [workspace.clear]
    copy_to_repo = true   ->  copy the workspace into the GitHub repo folder
                              (nothing is deleted, no git command is run)

    python src/MetaWorkspace.py
"""

from pathlib import Path
import fnmatch
import os
import shutil

import PipelineConfig


# ============================================================
# SETTINGS (from config/config.toml)
# ============================================================

CFG = PipelineConfig.load()
_WS = CFG["workspace"]
_PATHS = CFG["paths"]

# False: clear the workspace. True: copy it to the repo instead.
COPY_TO_REPO = _WS["copy_to_repo"]

# What clearing deletes: {config switch: paths created by the scripts}
CLEAR_ITEMS = {
    "manifest": [Path(_PATHS["manifest"])],
    "downloads": [Path(_PATHS["download_dir"])],
    "tiles": [Path(_PATHS["extract_dir"])],
    "terrain_outputs": [Path(_PATHS["output_dir"])],
    "comparison_outputs": [Path(_PATHS["comparison_output_dir"])],
    "legacy": [Path("lidar_manifest.json"), Path("terrain_outputs")],
    "pycache": [Path("src/__pycache__")],
}
CLEAR_ON = _WS["clear"]

REPO_DIR = Path(_WS["repo_dir"])
REPO_SUBFOLDER = Path(_WS["repo_subfolder"])
COPY_ITEMS = [Path(p) for p in _WS["copy_items"]]
COPY_EXCLUDE = list(_WS["copy_exclude"])
MAX_FILE_BYTES = _WS["max_file_mb"] * 1024 * 1024

# Never deleted, nor any folder containing them: code, config,
# environment, the repo and every input file named in [paths]
PROTECTED = [
    Path("src") / "MetaWorkspace.py",
    Path("config"),
    Path(".venv"),
    REPO_DIR,
    *[
        Path(_PATHS[key]) for key in
        ("tile_index_gpkg", "river_km_gpkg", "comparison_dem",
         "comparison_water", "comparison_km_gpkg")
        if _PATHS.get(key)
    ],
]


# ============================================================
# CLEAR
# ============================================================

def _check_deletable(path):

    # Raise if deleting `path` could remove anything that is not a
    # script output (guards against a wrong path in the config)
    root = Path.cwd().resolve()
    target = path.resolve()

    if target == root or root not in target.parents:
        raise ValueError(f"Refusing to delete {path}: not inside the project folder.")

    for protected in PROTECTED:
        p = protected.resolve()
        if p == target or target in p.parents:
            raise ValueError(f"Refusing to delete {path}: it contains {protected}.")


def clear_workspace():

    targets = []

    for key, paths in CLEAR_ITEMS.items():

        if not CLEAR_ON.get(key, False):
            print(f"Kept ({key} = false): {', '.join(map(str, paths))}")
            continue

        targets += paths

    # Check everything first, so a bad path deletes nothing at all
    for path in targets:
        _check_deletable(path)

    for path in targets:

        if path.is_dir():
            shutil.rmtree(path)
            print(f"Deleted directory: {path}")
        elif path.exists():
            path.unlink()
            print(f"Deleted file: {path}")

    print("\nWorkspace cleared.")


# ============================================================
# COPY TO REPO
# ============================================================

def _excluded(relative):

    return any(
        fnmatch.fnmatch(part, pattern)
        for part in relative.parts
        for pattern in COPY_EXCLUDE
    )


def _files(item):

    # Every file of a copy item (a file or a folder), as paths
    # relative to the project root
    root = Path.cwd().resolve()

    paths = [item] if item.is_file() else sorted(f for f in item.rglob("*") if f.is_file())

    for path in paths:
        relative = Path(os.path.relpath(path.resolve(), root))
        if not relative.parts or relative.parts[0] == "..":
            continue
        yield path, relative


def copy_to_repo():

    # Copies COPY_ITEMS to REPO_DIR / REPO_SUBFOLDER, keeping their
    # paths relative to the project root. Existing files there are
    # overwritten when the source differs; nothing is ever deleted.
    repo = REPO_DIR.resolve()
    target = (repo / REPO_SUBFOLDER).resolve()

    if not repo.is_dir():
        raise FileNotFoundError(f"Repo folder not found: {REPO_DIR}")

    if target != repo and repo not in target.parents:
        raise ValueError(f"repo_subfolder must stay inside {REPO_DIR}: {REPO_SUBFOLDER}")

    if ".git" in target.relative_to(repo).parts:
        raise ValueError("Refusing to copy into the .git folder.")

    print(f"Copying workspace to: {target}\n")

    copied = unchanged = 0
    too_big = []
    missing = []

    for item in COPY_ITEMS:

        if not item.exists():
            missing.append(item)
            continue

        # Never copy the repo into itself
        resolved = item.resolve()
        if resolved == repo or repo in resolved.parents:
            print(f"  Skipped (inside the repo folder): {item}")
            continue

        for path, relative in _files(item):

            if _excluded(relative):
                continue

            size = path.stat().st_size

            if size > MAX_FILE_BYTES:
                too_big.append((relative, size))
                continue

            destination = target / relative

            if destination.exists():
                existing = destination.stat()
                if existing.st_size == size and int(existing.st_mtime) == int(path.stat().st_mtime):
                    unchanged += 1
                    continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            print(f"  Copied: {relative}")
            copied += 1

    for item in missing:
        print(f"  WARNING: not found, skipped: {item}")

    for relative, size in too_big:
        print(f"  WARNING: {relative} is {size / 2**20:.0f} MB, over the "
              f"{MAX_FILE_BYTES / 2**20:.0f} MB GitHub limit: skipped")

    print(f"\nCopied {copied} files ({unchanged} unchanged) to {target}.")
    print("Nothing was deleted and no git command was run: review, commit and push manually.")


if __name__ == "__main__":
    if COPY_TO_REPO:
        copy_to_repo()
    else:
        clear_workspace()
