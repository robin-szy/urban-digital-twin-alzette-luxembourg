#!/usr/bin/env bash
# Produce the runtime Terria config files that docker-compose.override.yml mounts.
#
# WHY THIS EXISTS
# ---------------
# terriajs/Dockerfile COPYs client_config.json and catalog.json into the image
# at build time, so editing them has no effect on a running container - and
# the image cannot currently be rebuilt anyway (the Dockerfile COPYs
# date-selector.js, which is absent from the repo).
#
# The container's entrypoint also substitutes these files IN PLACE. Mounting
# the tracked sources directly would therefore rewrite them in your working
# tree. So the placeholders are filled here instead, into *.generated.json,
# which are gitignored and safe to be rewritten.
#
# Run this after editing client_config.json or catalog.json, then:
#     ./up.sh up -d --force-recreate terria_map
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import json, pathlib

env = {}
for line in pathlib.Path(".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()

def url(host_key, port_key, default="http://localhost"):
    host = env.get(host_key, default)
    port = env.get(port_key)
    return f"{host}:{port}" if port else host

replacements = {
    "$BACKEND_URL": url("BACKEND_HOST", "BACKEND_PORT"),
    "$GEOSERVER_URL": url("GEOSERVER_HOST", "GEOSERVER_PORT"),
    "$CESIUM_ACCESS_TOKEN": env.get("CESIUM_ACCESS_TOKEN", ""),
    "$LINZ_BASEMAPS_API_KEY": env.get("LINZ_BASEMAPS_API_KEY", ""),
}

for source, target in [
    ("terriajs/client_config.json", "terriajs/config.generated.json"),
    ("terriajs/catalog.json", "terriajs/catalog.generated.json"),
]:
    text = pathlib.Path(source).read_text()
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    json.loads(text)  # fail loudly rather than hand Terria broken JSON
    pathlib.Path(target).write_text(text)
    print(f"  {source} -> {target}")
PY
echo "Generated. Now: ./up.sh up -d --force-recreate terria_map"
