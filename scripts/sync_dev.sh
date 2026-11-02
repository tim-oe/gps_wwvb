#!/usr/bin/env bash
# Copy the dev tree to the Pi. Uses rsync when it is installed, otherwise tar over ssh.
set -euo pipefail

HOST="${1:?host}"
DEST="${2:?remote dir}"
FULL="${3:-}"

ssh -o BatchMode=yes "$HOST" "mkdir -p ${DEST}"
# pyproject.toml, poetry.lock, and requirements.txt go every time so a later
# install on the Pi sees the same dependency set as this tree.
if command -v rsync >/dev/null 2>&1; then
  if [[ "$FULL" == "--full" ]]; then
    rsync -az pyproject.toml poetry.lock README.md gps_wwvb.toml requirements.txt "${HOST}:${DEST}/"
  else
    rsync -az pyproject.toml poetry.lock gps_wwvb.toml requirements.txt "${HOST}:${DEST}/"
  fi
  rsync -az --delete src/ "${HOST}:${DEST}/src/"
else
  ssh -o BatchMode=yes "$HOST" "rm -rf ${DEST}/src"
  if [[ "$FULL" == "--full" ]]; then
    tar -cf - pyproject.toml poetry.lock README.md gps_wwvb.toml requirements.txt src \
      | ssh -o BatchMode=yes "$HOST" "cd ${DEST} && tar -xf -"
  else
    tar -cf - pyproject.toml poetry.lock gps_wwvb.toml requirements.txt src \
      | ssh -o BatchMode=yes "$HOST" "cd ${DEST} && tar -xf -"
  fi
fi
