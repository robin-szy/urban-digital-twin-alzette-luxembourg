#!/usr/bin/env bash
# Bring the stack up, using the GPU only if this host actually has one.
#
# Docker Compose has no "use a GPU if one exists" conditional, so the choice
# is made here instead. docker-compose.yml reserves an NVIDIA GPU by default
# (so a plain `docker compose up -d` uses it on the project host); this script
# adds docker-compose.cpu.yml to clear that reservation on a host without the
# NVIDIA container runtime, where it would otherwise stop the worker starting.
#
# Usage - any docker compose arguments are passed straight through:
#
#     ./up.sh up -d
#     ./up.sh up -d --build
#     ./up.sh logs -f celery_worker
#     ./up.sh down
#
# Running plain `docker compose up -d` instead is correct on a GPU host and
# will fail on a CPU-only one. This script is correct on both.

set -euo pipefail

cd "$(dirname "$0")"

# docker-compose.override.yml carries this project's plugin bind mounts. It is
# loaded automatically ONLY when no -f flag is given, so it must be listed
# explicitly here or the plugins silently disappear from the containers.
files=(-f docker-compose.yml -f docker-compose.override.yml)

has_nvidia_runtime() {
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

has_nvidia_smi() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

if has_nvidia_runtime && has_nvidia_smi; then
    echo "[up.sh] NVIDIA runtime and GPU detected -> keeping the GPU reservation" >&2
else
    if has_nvidia_runtime || has_nvidia_smi; then
        echo "[up.sh] Partial NVIDIA setup detected (runtime or driver, not both)." >&2
        echo "[up.sh] Treating this host as CPU-only to avoid a half-working GPU." >&2
    else
        echo "[up.sh] No NVIDIA GPU runtime -> clearing the GPU reservation (CPU only)" >&2
    fi
    files+=(-f docker-compose.cpu.yml)

    # docker-compose.cpu.yml needs the !reset tag, added in Compose 2.24.
    version="$(docker compose version --short 2>/dev/null || echo 0)"
    major="${version%%.*}"
    rest="${version#*.}"
    minor="${rest%%.*}"
    if [[ "${major:-0}" -lt 2 ]] || { [[ "${major:-0}" -eq 2 ]] && [[ "${minor:-0}" -lt 24 ]]; }; then
        echo "[up.sh] ERROR: Docker Compose $version is too old for the !reset tag." >&2
        echo "[up.sh] Upgrade to 2.24 or newer, or remove the deploy.resources" >&2
        echo "[up.sh] block from docker-compose.yml by hand." >&2
        exit 1
    fi
fi

echo "[up.sh] docker compose ${files[*]} $*" >&2
exec docker compose "${files[@]}" "$@"
