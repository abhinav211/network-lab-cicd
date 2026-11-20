#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."
if [[ ! -f build/lab-owned ]]; then
  exit 0
fi
: "${LAB_NAME:?Set LAB_NAME to the run being cleaned up}"
if [[ ! "$LAB_NAME" =~ ^netci-[0-9]+-[0-9]+$ ]] \
  || [[ "$( < build/lab-owned)" != "$LAB_NAME" ]]; then
  echo "Lab ownership mismatch; refusing cleanup" >&2
  exit 1
fi
sudo -A containerlab destroy --topo build/runtime.clab.yml --cleanup \
  2>&1 | tee build/cleanup.log
if [[ -n "$(docker ps -aq --filter "name=^/clab-${LAB_NAME}-")" ]] \
  || docker network inspect "$LAB_NAME" > /dev/null 2>&1; then
  echo "Lab resources remain after destroy" >&2
  exit 1
fi
rm -- build/lab-owned
