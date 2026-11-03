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
  local pkgs=(
    python3-venv
    python3-pip
    python3-smbus
    python3-rpi-lgpio
    python3-ephem
    python3-sysv-ipc
    i2c-tools
    rsync
  )
  local missing=()
  local pkg
  for pkg in "${pkgs[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
    echo "Packages installed for ${TARGET_USER}."
  fi

  if [[ ! -e /dev/i2c-1 ]] && command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_i2c 0
  fi

  local user_groups
  user_groups=" $(id -nG "$TARGET_USER") "
  local group_added=0
  local grp
  for grp in i2c gpio; do
    if getent group "$grp" >/dev/null && [[ "$user_groups" != *" ${grp} "* ]]; then
      sudo usermod -aG "$grp" "$TARGET_USER"
      group_added=1
    fi
  done
  if [[ "$group_added" -eq 1 ]]; then
    echo "Group changes apply to new logins. If 'status' says permission denied, log out and back in."
  fi
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
req_stamp="$VENV/.requirements.sha256"
req_hash="$(sha256sum "$ROOT/requirements.txt" | awk '{print $1}')"
if [[ ! -f "$req_stamp" || "$(cat "$req_stamp")" != "$req_hash" ]] \
  || ! "$VENV/bin/python" -c "import es100" >/dev/null 2>&1; then
  # Versions and hashes come from poetry.lock via `make requirements`.
  "$VENV/bin/pip" install --disable-pip-version-check --require-hashes -r "$ROOT/requirements.txt"
  # es100-wwvb is Pi-only (RPi.GPIO). 0.4.4 is the release close() is written against.
  "$VENV/bin/pip" install --disable-pip-version-check --no-deps 'es100-wwvb==0.4.4'
  printf '%s\n' "$req_hash" >"$req_stamp"
fi
"$VENV/bin/pip" install --disable-pip-version-check --no-index --force-reinstall --no-deps "$wheel"

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
