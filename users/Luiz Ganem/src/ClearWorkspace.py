from pathlib import Path
import shutil

OUTPUT_DIR = Path("terrain_outputs")
DOWNLOAD_DIR = Path("lidar_downloads")
EXTRACT_DIR = Path("lidar_tiles")

FILES_TO_DELETE = [
    Path("lidar_manifest.json"),
]

DIRS_TO_DELETE = [
    DOWNLOAD_DIR,
    EXTRACT_DIR,
    OUTPUT_DIR,
]


def clear_workspace():
    for file in FILES_TO_DELETE:
        if file.exists():
            file.unlink()
            print(f"Deleted file: {file}")

    for directory in DIRS_TO_DELETE:
        if directory.exists():
            shutil.rmtree(directory)
            print(f"Deleted directory: {directory}")

    print("\nWorkspace cleared.")


if __name__ == "__main__":
    clear_workspace()