#!/usr/bin/env bash
# Build a wheel locally and install it on the Pi as the systemd service.
set -euo pipefail

cd "$(dirname "$0")/.."

PI_HOST="${PI_HOST:-tec-pi-wwv}"
PI_REL_DIR="${PI_REL_DIR:-~/src/gps_wwvb}"

poetry build
ssh -o BatchMode=yes "$PI_HOST" "mkdir -p ${PI_REL_DIR}/dist ${PI_REL_DIR}/deploy"
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete dist/ "${PI_HOST}:${PI_REL_DIR}/dist/"
  rsync -az deploy/ "${PI_HOST}:${PI_REL_DIR}/deploy/"
  rsync -az gps_wwvb.toml requirements.txt "${PI_HOST}:${PI_REL_DIR}/"
else
  ssh -o BatchMode=yes "$PI_HOST" "rm -rf ${PI_REL_DIR}/dist ${PI_REL_DIR}/deploy"
  tar -cf - dist deploy gps_wwvb.toml requirements.txt \
    | ssh -o BatchMode=yes "$PI_HOST" "cd ${PI_REL_DIR} && tar -xf -"
fi
ssh -o BatchMode=yes "$PI_HOST" "bash ${PI_REL_DIR}/deploy/install_on_pi.sh"
