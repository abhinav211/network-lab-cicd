#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."
: "${LAB_NAME:?Set LAB_NAME to netci-<run-id>-<attempt>}"
: "${LAB_USERNAME:?Set LAB_USERNAME}"
: "${LAB_PASSWORD:?Set LAB_PASSWORD}"
[[ "$LAB_NAME" =~ ^netci-[0-9]+-[0-9]+$ ]] || {
  echo "Invalid LAB_NAME" >&2
  exit 1
}

command -v containerlab
docker info > /dev/null
docker image inspect ceos:4.32.0F > /dev/null
containerlab version
python -m lab prepare --lab-name "$LAB_NAME"

# Refuse collisions rather than replacing another run's resources.
if docker network inspect "$LAB_NAME" > /dev/null 2>&1; then
  echo "Network $LAB_NAME already exists; refusing to deploy or destroy it" >&2
  exit 1
fi
if [[ -n "$(docker ps -aq --filter "name=^/clab-${LAB_NAME}-")" ]]; then
  echo "Containers for $LAB_NAME already exist; refusing to continue" >&2
  exit 1
fi

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if ! bash scripts/cleanup-lab.sh; then
    echo "Cleanup failed; use build/runtime.clab.yml for manual recovery" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$LAB_NAME" > build/lab-owned

sudo -A containerlab deploy --topo build/runtime.clab.yml 2>&1 | tee build/deploy.log
python -m lab wait --timeout 600 2>&1 | tee build/readiness.log
python -m lab configure 2>&1 | tee build/configure.log
python -m lab verify --timeout 180 2>&1 | tee build/verify.log
