#!/bin/bash

# Sets up everything the twin needs from a DEM, in one go (e.g. on a fresh checkout, where the
# git-ignored data/terrain/ is missing, or after changing the DEM):
#   1. scripts/prepare_dem.py, in the backend image (no local Python packages needed): the DEM
#      becomes the master DEM data/terrain/dem_merged.tif, the DEM the backend reads, and the map's
#      home camera is set to it.
#   2. scripts/build_lidar_terrain.sh: the 3D terrain tiles and geoid grid (data/terrain/lidar_egm2008/).
#   3. The stack is started (or the backend recreated, so it drops the old DEM), and the Relative
#      Elevation Model (data/rem/) is rebuilt from the new DEM.
#
# Usage: scripts/setup_terrain.sh <dem.tif> [--merge] [prepare_dem.py options, e.g. --resolution 1]
#   dem.tif   The DEM, e.g. ../../data/raw/filled_dem.tif. Only read.
#   --merge   Merge it into the current master DEM instead of starting from it alone.
# Needs Docker Desktop running. Builds the backend image first if it is missing.

set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
BACKEND_IMAGE=eddie_antarctica-backend
COMPOSE=(docker compose --env-file .env --env-file api_keys.env)

DEM=""
NEW=(--new)
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --merge) NEW=(); shift ;;
    -h|--help) sed -n '3,16p' "$0"; exit 0 ;;
    --*) EXTRA+=("$1"); [[ $# -gt 1 && "$2" != --* ]] && { EXTRA+=("$2"); shift; }; shift ;;
    *) DEM="$1"; shift ;;
  esac
done
if [[ -z "$DEM" ]]; then
  echo "Usage: scripts/setup_terrain.sh <dem.tif> [--merge] [prepare_dem.py options]" >&2
  exit 1
fi
if [[ ! -f "$DEM" ]]; then
  echo "Error: DEM not found: $DEM" >&2
  exit 1
fi
DEM=$(cd "$(dirname "$DEM")" && pwd)/$(basename "$DEM")
if ! docker info > /dev/null 2>&1; then
  echo "Error: Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

cd "$PROJECT_DIR"
if [[ -z "$(docker images -q "$BACKEND_IMAGE" 2> /dev/null)" ]]; then
  echo "==> Building the backend image (first time only)"
  "${COMPOSE[@]}" build backend
fi

DEM_DIR=$(dirname "$DEM")
PROJECT_HOST="$PROJECT_DIR"
DEM_RECORD="$DEM"
USER_ARGS=()
# Docker Desktop on Windows needs C:/... style paths, and Git Bash must not rewrite container paths.
if command -v cygpath > /dev/null; then
  DEM_DIR=$(cygpath -m "$DEM_DIR")
  PROJECT_HOST=$(cygpath -m "$PROJECT_DIR")
  DEM_RECORD=$(cygpath -m "$DEM")
else
  # Linux: files written into the project belong to you, not root.
  USER_ARGS=(--user "$(id -u):$(id -g)")
fi
export MSYS_NO_PATHCONV=1

echo "==> 1/3 Master DEM from $DEM"
docker run --rm "${USER_ARGS[@]}" --entrypoint /venv/bin/python \
  -v "$PROJECT_HOST:/project" -v "$DEM_DIR:/input:ro" "$BACKEND_IMAGE" \
  /project/scripts/prepare_dem.py "/input/$(basename "$DEM")" "${NEW[@]}" "${EXTRA[@]}" \
  --no-tiles --record-as "$DEM_RECORD"

echo "==> 2/3 3D terrain tiles"
bash "$PROJECT_DIR/scripts/build_lidar_terrain.sh" --force

echo "==> 3/3 Starting the twin"
if [[ ! -f .env || ! -f api_keys.env ]]; then
  echo "    .env or api_keys.env missing: start the stack yourself once they exist:"
  echo "    ${COMPOSE[*]} up -d"
  exit 0
fi
"${COMPOSE[@]}" up -d
# The backend keeps the DEM it loaded in memory; a new container reads the new one.
"${COMPOSE[@]}" up -d --force-recreate backend terria_map
echo "    Rebuilding the Relative Elevation Model (about 30 s)"
docker exec backend_digital_twin /venv/bin/python -c "from eddie_water_level import rem; print(rem.build())" \
  2>&1 | grep -v " INFO " || echo "    (the backend rebuilds it on the first water level instead)"

echo "==> Done. Open the map and hard-refresh (Ctrl+F5)."
