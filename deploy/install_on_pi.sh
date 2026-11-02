#!/usr/bin/env bash
# Install gps-wwvb on the Pi. Safe to run more than once.
#   install_on_pi.sh              release install into ~/src/gps_wwvb and restart the service
#   install_on_pi.sh --deps-only  apt packages, I2C, and groups (used by make dev-setup)
#   install_on_pi.sh --no-restart install or upgrade without restarting the service
set -euo pipefail

DEPS_ONLY=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --deps-only) DEPS_ONLY=1 ;;
    --no-restart) RESTART=0 ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

install_deps() {
  sudo apt-get update
  sudo apt-get install -y \
    python3-venv \
    python3-pip \
    python3-smbus \
    python3-rpi-lgpio \
    python3-ephem \
    python3-sysv-ipc \
    i2c-tools \
    rsync
  if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_i2c 0
  fi
  if getent group i2c >/dev/null; then
    sudo usermod -aG i2c "$TARGET_USER"
  fi
  if getent group gpio >/dev/null; then
    sudo usermod -aG gpio "$TARGET_USER"
  fi
  echo "Packages installed for ${TARGET_USER}."
  echo "Group changes apply to new logins. If 'status' says permission denied, log out and back in."
  if [[ ! -e /dev/i2c-1 ]]; then
    echo "I2C is enabled in config. Reboot if /dev/i2c-1 is still missing."
  fi
}

install_deps

if [[ "$DEPS_ONLY" -eq 1 ]]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"

if [[ ! -d "$VENV" ]]; then
  python3 -m venv --system-site-packages "$VENV"
fi

shopt -s nullglob
wheels=("$ROOT"/dist/gps_wwvb-*.whl)
if [[ ${#wheels[@]} -eq 0 ]]; then
  echo "No wheel found in $ROOT/dist" >&2
  exit 1
fi
mapfile -t wheels < <(printf '%s\n' "${wheels[@]}" | sort -V)
wheel="${wheels[-1]}"

if [[ ! -f "$ROOT/requirements.txt" ]]; then
  echo "No requirements.txt in $ROOT" >&2
  exit 1
fi
# Versions and hashes come from poetry.lock via `make requirements`.
"$VENV/bin/pip" install --disable-pip-version-check --require-hashes -r "$ROOT/requirements.txt"
# es100-wwvb is Pi-only (RPi.GPIO). 0.4.4 is the release close() is written against.
"$VENV/bin/pip" install --disable-pip-version-check --no-deps 'es100-wwvb==0.4.4'
"$VENV/bin/pip" install --disable-pip-version-check --force-reinstall --no-deps "$wheel"

if [[ ! -f /etc/gps_wwvb.toml ]]; then
  sudo cp "$ROOT/gps_wwvb.toml" /etc/gps_wwvb.toml
  echo "Installed /etc/gps_wwvb.toml"
else
  echo "Leaving the existing /etc/gps_wwvb.toml in place"
fi
# Addresses stay in the root-owned file. Calibration is written to the state dir.
sudo chown root:root /etc/gps_wwvb.toml
sudo chmod 644 /etc/gps_wwvb.toml
sudo install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 755 /var/lib/gps-wwvb

sed -e "s|@USER@|${TARGET_USER}|g" -e "s|@HOME@|${TARGET_HOME}|g" \
  "$SCRIPT_DIR/gps-wwvb.service" | sudo tee /etc/systemd/system/gps-wwvb.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable gps-wwvb.service
if [[ "$RESTART" -eq 1 ]]; then
  sudo systemctl restart gps-wwvb.service
  echo "Restarted gps-wwvb.service"
else
  echo "Enabled gps-wwvb.service (not restarted)"
fi
