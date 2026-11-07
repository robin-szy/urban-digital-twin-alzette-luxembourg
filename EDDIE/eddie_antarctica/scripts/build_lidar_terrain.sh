#!/bin/bash

# Builds the 3D terrain of the map from the LiDAR DEM (data/terrain/dem_merged.tif), once, with
# ellipsoid heights (NG95 + EGM2008 geoid, see scripts/lidar_terrain_input.py), into
# data/terrain/lidar_egm2008/ (git-ignored):
#   terrain/              quantized-mesh tiles (Cesium Terrain Builder, scripts/build_terrain.sh),
#                         served by the backend at /terrain (TERRAIN_DATA_DIR)
#   geoid_egm2008.tif     the geoid undulation used, read by the backend (TERRAIN_GEOID_GRID)
#   terrain_input.json    geoid model and undulation range
#   build.stamp           the DEM it was built from; a later run with the same DEM does nothing
#
# Usage: scripts/build_lidar_terrain.sh [--force]
# Needs Docker and the backend image (docker compose build backend). The DEM is only read.

set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
DEM="$PROJECT_DIR/data/terrain/dem_merged.tif"
OUT="$PROJECT_DIR/data/terrain/lidar_egm2008"
BACKEND_IMAGE=eddie_antarctica-backend
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ ! -f "$DEM" ]]; then
  echo "Error: DEM not found: $DEM" >&2
  exit 1
fi

STAMP="$(basename "$DEM") $(stat -c '%s %Y' "$DEM") egm2008"
if [[ $FORCE == 0 && -f "$OUT/terrain/layer.json" && -f "$OUT/build.stamp" && "$(cat "$OUT/build.stamp")" == "$STAMP" ]]; then
  echo "==> Terrain already built from this DEM ($OUT); use --force to rebuild."
  exit 0
fi

mkdir -p "$OUT"
DATA_DIR="$PROJECT_DIR/data/terrain"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
OUT_HOST="$OUT"
# Docker Desktop on Windows needs C:/... style paths, and Git Bash must not rewrite container paths.
if command -v cygpath > /dev/null; then
  DATA_DIR=$(cygpath -m "$DATA_DIR")
  SCRIPTS_DIR=$(cygpath -m "$SCRIPTS_DIR")
  OUT_HOST=$(cygpath -m "$OUT")
fi
export MSYS_NO_PATHCONV=1

echo "==> Reprojecting to EPSG:4326 and adding the EGM2008 geoid"
docker run --rm -e PROJ_NETWORK=ON --entrypoint /venv/bin/python \
  -v "$DATA_DIR:/data" -v "$SCRIPTS_DIR:/scripts:ro" "$BACKEND_IMAGE" \
  /scripts/lidar_terrain_input.py /data/dem_merged.tif /data/lidar_egm2008

echo "==> Tiling"
bash "$PROJECT_DIR/scripts/build_terrain.sh" "$OUT/dem_4326_ellipsoid.tif" --volume "$OUT_HOST"
rm -f "$OUT/dem_4326_ellipsoid.tif"
echo "$STAMP" > "$OUT/build.stamp"
echo "==> Done: $OUT/terrain"
