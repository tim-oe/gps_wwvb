"""Ncurses terminal UI for viewing and configuring the u-blox MAX-M8Q over serial."""

from __future__ import annotations

import contextlib
import curses
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DYN_MODELS: tuple[tuple[int, str], ...] = (
    (0, "Portable"),
    (2, "Stationary"),
    (3, "Pedestrian"),
    (4, "Automotive"),
    (5, "Sea"),
    (6, "Airborne <1g"),
    (7, "Airborne <2g"),
    (8, "Airborne <4g"),
)
DYN_MODEL_NAMES: dict[int, str] = dict(DYN_MODELS)

CONSTELLATION_ORDER: tuple[str, ...] = (
    "GPS",
    "Galileo",
    "GLONASS",
    "BeiDou",
    "SBAS",
    "QZSS",
)
DEFAULT_CONSTELLATION_ENABLED: dict[str, bool] = {
    "GPS": True,
    "Galileo": False,
    "GLONASS": True,
    "BeiDou": False,
    "SBAS": True,
    "QZSS": True,
}
DEFAULT_DYN_MODEL = 0
DEFAULT_ANT_CABLE_DELAY_NS = 50
DEFAULT_PPS_MODE = "normal"

UBXTOOL_CONSTELLATION_ARG: dict[str, str] = {
    "Galileo": "GALILEO",
    "GLONASS": "GLONASS",
    "BeiDou": "BEIDOU",
    "SBAS": "SBAS",
}
# ubxtool has no -e/-d QZSS option, and its -e/-d GPS toggles both GPS and QZSS
# together. Sending a single-block UBX-CFG-GNSS (0x06, 0x3e) command via -c lets
# GPS and QZSS be toggled independently: (gnssId, resTrkCh, maxTrkCh, sigCfgMask).
UBX_RAW_GNSS_BLOCK: dict[str, tuple[int, int, int, int]] = {
    "GPS": (0, 8, 16, 0x01),
    "QZSS": (5, 0, 3, 0x05),
}

PPS_MODES: tuple[tuple[str, str], ...] = (
    ("normal", "Normal (100 ms flash, /dev/pps0 active)"),
    ("dim", "Dimmed (50 us pulse, LED dark, /dev/pps0 active)"),
    ("off", "Off (TIMEPULSE & /dev/pps0 disabled)"),
)

_GNSS_HEADER_RE = re.compile(
    r"gnssId\s+(\d+)\s+TrkCh\s+(\d+)\s+maxTrCh\s+(\d+)\s+reserved\s+\d+\s+Flags\s+x([0-9a-fA-F]+)"
)
_NAV5_MODEL_RE = re.compile(r"\bdynModel\s+(\d+)\b")
_TP5_DELAY_RE = re.compile(r"\bantCableDelay\s+(-?\d+)\s+rfGroupDelay\s+(-?\d+)")
_TP5_FREQ_RE = re.compile(r"\bfreqPeriod\s+(\d+)\s+freqPeriodLock\s+(\d+)")
_TP5_LEN_RE = re.compile(r"\bpulseLenRatio\s+(\d+)\s+pulseLenRatioLock\s+(\d+)")
_TP5_FLAGS_RE = re.compile(r"\bflags\s+x([0-9a-fA-F]+)\b")

_GNSS_ID_NAMES: dict[int, str] = {
    0: "GPS",
    1: "SBAS",
    2: "Galileo",
    3: "BeiDou",
    5: "QZSS",
    6: "GLONASS",
}


@dataclass
class ConstellationEntry:
    name: str
    enabled: bool
    trk_ch: int = 0
    max_ch: int = 0


@dataclass
class GnssState:
    device: str = "/dev/ttyS0"
    speed: int = 9600
    sw_version: str = "n/a"
    hw_version: str = "n/a"
    fw_version: str = "n/a"
    prot_ver: str = "18.00"
    supported_gnss: str = "n/a"
    fix_summary: str = "n/a"
    constellations: dict[str, ConstellationEntry] = field(default_factory=dict)
    dyn_model: int = 0
    ant_cable_delay_ns: int = 50
    freq_period_us: int = 1_000_000
    freq_period_lock_us: int = 1_000_000
    pulse_len_us: int = 0
    pulse_len_lock_us: int = 100_000
    tp5_flags: int = 0x77
    pi_act_led_on: bool | None = None
    dirty: bool = False

    @property
    def pps_active(self) -> bool:
        return bool(self.tp5_flags & 0x01) and self.pulse_len_lock_us > 0

    @property
    def pps_mode(self) -> str:
        if not self.pps_active:
            return "off"
        if self.pulse_len_lock_us <= 500:
            return "dim"
        return "normal"

    @property
    def pps_mode_label(self) -> str:
        mode = self.pps_mode
        for key, label in PPS_MODES:
            if key == mode:
                return label
        return mode

    @property
    def dyn_model_label(self) -> str:
        name = DYN_MODEL_NAMES.get(self.dyn_model, "Custom")
        return f"{self.dyn_model} ({name})"


def default_constellations() -> dict[str, ConstellationEntry]:
    return {
        "GPS": ConstellationEntry("GPS", True, 8, 16),
        "Galileo": ConstellationEntry("Galileo", True, 4, 8),
        "GLONASS": ConstellationEntry("GLONASS", True, 8, 14),
        "BeiDou": ConstellationEntry("BeiDou", False, 8, 16),
        "SBAS": ConstellationEntry("SBAS", True, 1, 3),
        "QZSS": ConstellationEntry("QZSS", True, 0, 3),
    }


def parse_ubxtool_output(text: str, state: GnssState | None = None) -> GnssState:
    """Parse ``ubxtool -v 2`` output for ``MON-VER`` and ``CFG-*`` polls."""
    target = state if state is not None else GnssState(constellations=default_constellations())
    if not target.constellations:
        target.constellations = default_constellations()

    extensions: list[str] = []
    in_sent = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "sent:":
            in_sent = True
            continue
        if not line:
            in_sent = False
            continue
        if in_sent:
            continue
        _parse_mon_ver_line(line, target, extensions)
        _parse_cfg_line(line, target)

    if extensions:
        target.supported_gnss = ";".join(extensions)
    return target


def _parse_mon_ver_line(line: str, target: GnssState, extensions: list[str]) -> None:
    if line.startswith("swVersion "):
        target.sw_version = line.removeprefix("swVersion ").strip()
    elif line.startswith("hwVersion "):
        target.hw_version = line.removeprefix("hwVersion ").strip()
    elif line.startswith("extension FWVER="):
        target.fw_version = line.removeprefix("extension FWVER=").strip()
    elif line.startswith("extension PROTVER="):
        target.prot_ver = line.removeprefix("extension PROTVER=").strip()
    elif line.startswith("extension ") and ";" in line:
        extensions.append(line.removeprefix("extension ").strip())
    elif line.startswith(("$GNGGA,", "$GPGGA,")):
        summary = _parse_gga_summary(line)
        if summary is not None:
            target.fix_summary = summary


def _parse_cfg_line(line: str, target: GnssState) -> None:
    gnss_match = _GNSS_HEADER_RE.search(line)
    if gnss_match:
        name = _GNSS_ID_NAMES.get(int(gnss_match.group(1)))
        if name is not None:
            target.constellations[name] = ConstellationEntry(
                name=name,
                enabled=bool(int(gnss_match.group(4), 16) & 0x01),
                trk_ch=int(gnss_match.group(2)),
                max_ch=int(gnss_match.group(3)),
            )

    nav_match = _NAV5_MODEL_RE.search(line)
    if nav_match:
        target.dyn_model = int(nav_match.group(1))

    delay_match = _TP5_DELAY_RE.search(line)
    if delay_match:
        target.ant_cable_delay_ns = int(delay_match.group(1))

    freq_match = _TP5_FREQ_RE.search(line)
    if freq_match:
        target.freq_period_us = int(freq_match.group(1))
        target.freq_period_lock_us = int(freq_match.group(2))

    len_match = _TP5_LEN_RE.search(line)
    if len_match:
        target.pulse_len_us = int(len_match.group(1))
        target.pulse_len_lock_us = int(len_match.group(2))

    flags_match = _TP5_FLAGS_RE.search(line)
    if flags_match:
        target.tp5_flags = int(flags_match.group(1), 16)


def _parse_gga_summary(sentence: str) -> str | None:
    body = sentence.split("*", 1)[0]
    fields = body.split(",")
    if len(fields) < 10:
        return None
    qual = fields[6]
    sats = fields[7] or "0"
    alt = fields[9]
    fix_label = "No Fix" if qual in ("", "0") else f"Fix Q={qual}"
    lat = _format_nmea_coord(fields[2], fields[3], 2)
    lon = _format_nmea_coord(fields[4], fields[5], 3)
    if lat and lon:
        alt_str = f"  alt {alt}m" if alt else ""
        return f"{fix_label}  {lat} {lon}{alt_str}  SVs {sats}"
    return f"{fix_label}  SVs {sats}"


def _format_nmea_coord(raw: str, hemi: str, deg_digits: int) -> str | None:
    if not raw or not hemi or len(raw) <= deg_digits:
        return None
    try:
        deg = int(raw[:deg_digits])
        minutes = float(raw[deg_digits:])
    except ValueError:
        return None
    value = deg + minutes / 60.0
    if hemi in ("S", "W"):
        value = -value
    return f"{value:.5f}"


class SimGnssBackend:
    """In-memory GNSS configuration backend for ``--simulate`` and unit tests."""

    def __init__(self, device: str = "/dev/ttyS0", speed: int = 9600):
        self.state = GnssState(
            device=device,
            speed=speed,
            sw_version="ROM CORE 3.01 (107888)",
            hw_version="00080000",
            fw_version="SPG 3.01",
            prot_ver="18.00",
            supported_gnss="GPS;GLO;GAL;BDS;SBAS;IMES;QZSS",
            fix_summary="Fix Q=1  37.36306 -121.92861  alt 18.9m  SVs 10",
            constellations=default_constellations(),
            dyn_model=0,
            ant_cable_delay_ns=50,
            pulse_len_lock_us=100_000,
            tp5_flags=0x77,
            pi_act_led_on=True,
        )

    def open(self) -> str:
        return "Connected to simulated MAX-M8Q."

    def close(self) -> None:
        """Nothing to release in simulation."""

    def refresh(self) -> str:
        return "Polled MON-VER, CFG-GNSS, CFG-NAV5, CFG-TP5 (simulated)."

    def toggle_constellation(self, name: str) -> str:
        entry = self.state.constellations[name]
        return self.set_constellation(name, not entry.enabled)

    def set_constellation(self, name: str, enabled: bool) -> str:
        entry = self.state.constellations[name]
        entry.enabled = enabled
        self.state.dirty = True
        verb = "Enabled" if entry.enabled else "Disabled"
        return f"{verb} {name} (simulated)."

    def set_dyn_model(self, model: int) -> str:
        self.state.dyn_model = model
        self.state.dirty = True
        return f"Set dynamic model to {self.state.dyn_model_label} (simulated)."

    def set_pps_mode(self, mode: str) -> str:
        if mode == "off":
            self.state.tp5_flags &= ~0x01
            self.state.pulse_len_lock_us = 0
        elif mode == "dim":
            self.state.tp5_flags |= 0x77
            self.state.pulse_len_lock_us = 50
        else:
            self.state.tp5_flags |= 0x77
            self.state.pulse_len_lock_us = 100_000
        self.state.dirty = True
        return f"Set PPS mode to {self.state.pps_mode_label} (simulated)."

    def set_ant_cable_delay(self, delay_ns: int) -> str:
        clamped = max(-32768, min(32767, delay_ns))
        self.state.ant_cable_delay_ns = clamped
        self.state.dirty = True
        return f"Set antenna cable delay to {clamped} ns (simulated)."

    def toggle_pi_act_led(self) -> str:
        current = True if self.state.pi_act_led_on is None else self.state.pi_act_led_on
        return self.set_pi_act_led(not current)

    def set_pi_act_led(self, enabled: bool) -> str:
        self.state.pi_act_led_on = enabled
        state_word = "ON" if self.state.pi_act_led_on else "OFF"
        return f"Set Pi ACT LED {state_word} (simulated)."

    def save(self) -> str:
        self.state.dirty = False
        return "Saved configuration to battery-backed RAM (simulated)."

    def reset_defaults(self) -> str:
        self.state.constellations = default_constellations()
        self.state.constellations["Galileo"].enabled = False
        self.state.dyn_model = 0
        self.state.ant_cable_delay_ns = 50
        self.state.pulse_len_lock_us = 100_000
        self.state.tp5_flags = 0x77
        self.state.dirty = True
        return "Reset receiver configuration to defaults (simulated)."


class UbxtoolBackend:
    """Hardware backend that configures the MAX-M8Q via ``ubxtool`` on ``/dev/ttyS0``."""

    def __init__(
        self,
        device: str = "/dev/ttyS0",
        speed: int = 9600,
        *,
        prot_ver: str = "18.00",
        act_led_path: Path = Path("/sys/class/leds/ACT/brightness"),
    ):
        self.state = GnssState(
            device=device,
            speed=speed,
            prot_ver=prot_ver,
            constellations=default_constellations(),
        )
        self._act_led_path = act_led_path
        self._gpsd_stopped = False

    def open(self) -> str:
        if shutil.which("ubxtool") is None:
            raise RuntimeError("ubxtool not found in PATH (install gpsd-clients / gpsd-tools)")
        self._stop_gpsd()
        return self.refresh()

    def close(self) -> None:
        if self._gpsd_stopped:
            self._start_gpsd()
            self._gpsd_stopped = False

    def refresh(self) -> str:
        out = self._run_ubxtool(
            "-p",
            "MON-VER",
            "-p",
            "CFG-GNSS",
            "-p",
            "CFG-NAV5",
            "-p",
            "CFG-TP5",
            "-v",
            "2",
        )
        parse_ubxtool_output(out, self.state)
        self.state.pi_act_led_on = self._read_act_led()
        return f"Polled {self.state.device} @ {self.state.speed} bps."

    def toggle_constellation(self, name: str) -> str:
        entry = self.state.constellations[name]
        return self.set_constellation(name, not entry.enabled)

    def set_constellation(self, name: str, enabled: bool) -> str:
        if name in UBX_RAW_GNSS_BLOCK:
            gnss_id, res_ch, max_ch, sig_mask = UBX_RAW_GNSS_BLOCK[name]
            en_byte = 1 if enabled else 0
            raw_cmd = (
                f"06,3e,00,00,ff,01,{gnss_id:02x},{res_ch:02x},"
                f"{max_ch:02x},00,{en_byte:02x},00,{sig_mask:02x},00"
            )
            out_cmd = self._run_ubxtool("-c", raw_cmd, "-v", "2")
            out_poll = self._run_ubxtool("-p", "CFG-GNSS", "-v", "2")
            out = f"{out_cmd}\n{out_poll}"
        else:
            ubx_name = UBXTOOL_CONSTELLATION_ARG[name]
            flag = "-e" if enabled else "-d"
            out = self._run_ubxtool(flag, ubx_name, "-p", "CFG-GNSS", "-v", "2")
        parse_ubxtool_output(out, self.state)
        self.state.dirty = True
        if "UBX-ACK-NAK" in out:
            return f"Receiver rejected {name} change (check concurrent constellation limit)."
        verb = "Enabled" if self.state.constellations[name].enabled else "Disabled"
        return f"{verb} {name} (press 's' to save to BBR)."

    def set_dyn_model(self, model: int) -> str:
        out = self._run_ubxtool("-p", f"MODEL,{model}", "-p", "CFG-NAV5", "-v", "2")
        parse_ubxtool_output(out, self.state)
        self.state.dirty = True
        return f"Set dynamic model to {self.state.dyn_model_label} (press 's' to save to BBR)."

    def set_pps_mode(self, mode: str) -> str:
        if mode == "off":
            out = self._run_ubxtool("-d", "PPS", "-p", "CFG-TP5", "-v", "2")
            parse_ubxtool_output(out, self.state)
            self.state.dirty = True
            return "Disabled TIMEPULSE / PPS LED (press 's' to save to BBR)."
        pulse_us = 50 if mode == "dim" else 100_000
        preset = self._tp5_preset(pulse_len_lock_us=pulse_us, flags=0x77)
        out = self._run_ubxtool("-e", "PPS", "-p", preset, "-p", "CFG-TP5", "-v", "2")
        parse_ubxtool_output(out, self.state)
        self.state.dirty = True
        return f"Set PPS to {self.state.pps_mode_label} (press 's' to save to BBR)."

    def set_ant_cable_delay(self, delay_ns: int) -> str:
        clamped = max(-32768, min(32767, delay_ns))
        preset = self._tp5_preset(ant_cable_delay_ns=clamped)
        out = self._run_ubxtool("-p", preset, "-p", "CFG-TP5", "-v", "2")
        parse_ubxtool_output(out, self.state)
        self.state.dirty = True
        return f"Set antenna cable delay to {self.state.ant_cable_delay_ns} ns."

    def toggle_pi_act_led(self) -> str:
        current = self._read_act_led()
        if current is None:
            return f"{self._act_led_path} is not available on this machine."
        return self.set_pi_act_led(not current)

    def set_pi_act_led(self, enabled: bool) -> str:
        if self._read_act_led() is None:
            return f"{self._act_led_path} is not available on this machine."
        target = "1" if enabled else "0"
        cmd = [*self._sudo_prefix(), "sh", "-c", f"echo {target} > {self._act_led_path}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5.0)
        if res.returncode != 0:
            err = res.stderr.strip() or "permission denied"
            return f"Failed to set Pi ACT LED: {err}"
        self.state.pi_act_led_on = self._read_act_led()
        label = "ON" if self.state.pi_act_led_on else "OFF"
        return f"Set Pi Zero ACT LED to {label}."

    def save(self) -> str:
        out = self._run_ubxtool("-p", "SAVE")
        if "UBX-ACK-NAK" in out:
            return "Receiver rejected SAVE command."
        self.state.dirty = False
        return "Saved configuration to battery-backed RAM (UBX-CFG-CFG)."

    def reset_defaults(self) -> str:
        self._run_ubxtool("-p", "RESET")
        self.refresh()
        self.state.dirty = True
        return "Reset receiver configuration to defaults (press 's' to save to BBR)."

    def _tp5_preset(
        self,
        *,
        ant_cable_delay_ns: int | None = None,
        pulse_len_lock_us: int | None = None,
        flags: int | None = None,
    ) -> str:
        delay = self.state.ant_cable_delay_ns if ant_cable_delay_ns is None else ant_cable_delay_ns
        pulse_lock = (
            self.state.pulse_len_lock_us if pulse_len_lock_us is None else pulse_len_lock_us
        )
        flag_val = self.state.tp5_flags if flags is None else flags
        return (
            f"CFG-TP5,0,{delay},0,{self.state.freq_period_us},"
            f"{self.state.freq_period_lock_us},{self.state.pulse_len_us},"
            f"{pulse_lock},0,0x{flag_val:02x}"
        )

    def _read_act_led(self) -> bool | None:
        if not self._act_led_path.is_file():
            return None
        try:
            value = int(self._act_led_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value > 0

    def _run_ubxtool(self, *ubx_args: str) -> str:
        cmd = [
            *self._sudo_prefix(),
            "ubxtool",
            "-P",
            self.state.prot_ver,
            "-w",
            "1.0",
            "-f",
            self.state.device,
            "-s",
            str(self.state.speed),
            *ubx_args,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15.0)
        if res.returncode != 0:
            detail = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            raise RuntimeError(f"ubxtool failed: {detail}")
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    def _stop_gpsd(self) -> None:
        if shutil.which("systemctl") is None:
            return
        cmd = [*self._sudo_prefix(), "systemctl", "stop", "gpsd.socket", "gpsd.service"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10.0)
        if res.returncode == 0:
            self._gpsd_stopped = True

    def _start_gpsd(self) -> None:
        if shutil.which("systemctl") is None:
            return
        cmd = [*self._sudo_prefix(), "systemctl", "start", "gpsd.socket", "gpsd.service"]
        subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10.0)

    @staticmethod
    def _sudo_prefix() -> list[str]:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return []
        if shutil.which("sudo") is not None:
            return ["sudo", "-n"]
        return []


@dataclass(frozen=True)
class MenuRow:
    key: str
    category: str
    label: str
    value: str
    hint: str


def build_menu_rows(state: GnssState) -> list[MenuRow]:
    """Build the interactive rows displayed in the ncurses UI."""
    rows: list[MenuRow] = []
    for name in CONSTELLATION_ORDER:
        entry = state.constellations.get(name, ConstellationEntry(name, False))
        badge = "[ON] " if entry.enabled else "[OFF]"
        channels = f"(trk {entry.trk_ch:2d} / max {entry.max_ch:2d} ch)"
        rows.append(
            MenuRow(
                key=f"gnss:{name}",
                category="Constellation",
                label=name,
                value=f"{badge}  {channels}",
                hint="Space/Enter: toggle constellation",
            )
        )
    rows.append(
        MenuRow(
            key="dyn_model",
            category="Navigation",
            label="Dynamic Platform Model",
            value=state.dyn_model_label,
            hint="Space/Enter or Left/Right: cycle model (2=Stationary recommended)",
        )
    )
    rows.append(
        MenuRow(
            key="pps_mode",
            category="PPS / LED",
            label="HAT PPS LED & Pulse Width",
            value=state.pps_mode_label,
            hint="Space/Enter or Left/Right: cycle Normal (100ms) / Dimmed (50us) / Off",
        )
    )
    rows.append(
        MenuRow(
            key="ant_delay",
            category="Timing",
            label="Antenna Cable Delay",
            value=f"{state.ant_cable_delay_ns} ns",
            hint="Left/Right or -/+: adjust by 5 ns (RG-174 is ~5 ns/m)",
        )
    )
    if state.pi_act_led_on is None:
        led_value = "n/a (not on Raspberry Pi)"
    else:
        led_value = "[ON]" if state.pi_act_led_on else "[OFF]"
    rows.append(
        MenuRow(
            key="pi_act_led",
            category="Board LED",
            label="Pi Zero Green ACT LED",
            value=led_value,
            hint="Space/Enter: toggle /sys/class/leds/ACT/brightness",
        )
    )
    return rows


def apply_key_action(backend: SimGnssBackend | UbxtoolBackend, row_key: str, direction: int) -> str:
    """Execute the action for ``row_key`` when toggled or adjusted by ``direction`` (-1 or +1)."""
    if row_key.startswith("gnss:"):
        name = row_key.split(":", 1)[1]
        return backend.toggle_constellation(name)
    if row_key == "dyn_model":
        codes = [code for code, _ in DYN_MODELS]
        try:
            idx = codes.index(backend.state.dyn_model)
        except ValueError:
            idx = 0
        next_code = codes[(idx + direction) % len(codes)]
        return backend.set_dyn_model(next_code)
    if row_key == "pps_mode":
        modes = [mode for mode, _ in PPS_MODES]
        try:
            idx = modes.index(backend.state.pps_mode)
        except ValueError:
            idx = 0
        next_mode = modes[(idx + direction) % len(modes)]
        return backend.set_pps_mode(next_mode)
    if row_key == "ant_delay":
        return backend.set_ant_cable_delay(backend.state.ant_cable_delay_ns + (direction * 5))
    if row_key == "pi_act_led":
        return backend.toggle_pi_act_led()
    return "No action for selected row."


def reset_row_default(backend: SimGnssBackend | UbxtoolBackend, row_key: str) -> str:
    """Reset only the selected ``row_key`` back to its factory default."""
    if row_key.startswith("gnss:"):
        name = row_key.split(":", 1)[1]
        default_enabled = DEFAULT_CONSTELLATION_ENABLED.get(name, False)
        return backend.set_constellation(name, default_enabled)
    if row_key == "dyn_model":
        return backend.set_dyn_model(DEFAULT_DYN_MODEL)
    if row_key == "pps_mode":
        return backend.set_pps_mode(DEFAULT_PPS_MODE)
    if row_key == "ant_delay":
        return backend.set_ant_cable_delay(DEFAULT_ANT_CABLE_DELAY_NS)
    if row_key == "pi_act_led":
        return backend.set_pi_act_led(True)
    return "No default for selected row."


def render_screen_lines(
    state: GnssState,
    selected: int,
    status: str,
    width: int = 80,
) -> list[str]:
    """Render the UI text lines (used by both curses and non-interactive tests)."""
    rows = build_menu_rows(state)
    selected = max(0, min(selected, len(rows) - 1))
    sep = "-" * max(40, width - 2)
    dirty_tag = "  [UNSAVED CHANGES]" if state.dirty else ""
    lines = [
        f"u-blox MAX-M8Q Configuration ({state.device} @ {state.speed} bps){dirty_tag}",
        sep,
        f"Firmware : {state.sw_version}  |  FW: {state.fw_version}  |  Prot: {state.prot_ver}",
        f"Hardware : {state.hw_version}  |  Supported: {state.supported_gnss}",
        f"GNSS Fix : {state.fix_summary}",
        sep,
    ]
    for idx, row in enumerate(rows):
        cursor = ">" if idx == selected else " "
        lines.append(f"{cursor} [{row.category:<13}] {row.label:<26} : {row.value}")
    lines.extend(
        [
            sep,
            f"Hint   : {rows[selected].hint}",
            "Keys   : [Up/Down] Select   [Space/Enter] Toggle   [Left/Right] Adjust",
            "         [d] Field Default  [D] Reset All  [s] Save BBR  [r] Refresh  [q] Quit",
            f"Status : {status}",
        ]
    )
    return lines


def run_gnss_ui(backend: SimGnssBackend | UbxtoolBackend) -> int:
    """Open ``backend``, run the interactive curses loop, and ensure cleanup on exit."""
    try:
        initial_status = backend.open()
    except Exception as exc:
        backend.close()
        raise RuntimeError(str(exc)) from exc
    try:
        curses.wrapper(_curses_loop, backend, initial_status)
    finally:
        backend.close()
    return 0


def _curses_loop(
    stdscr,
    backend: SimGnssBackend | UbxtoolBackend,
    initial_status: str,
) -> None:
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    stdscr.keypad(True)
    selected = 0
    status = initial_status

    while True:
        rows = build_menu_rows(backend.state)
        selected = max(0, min(selected, len(rows) - 1))
        _draw_window(stdscr, backend.state, selected, status)
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            break
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(rows)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(rows)
        elif ch in (ord(" "), ord("\n"), ord("\r"), curses.KEY_ENTER, curses.KEY_RIGHT, ord("+")):
            row_key = rows[selected].key
            status = _safe_call(
                lambda key=row_key: apply_key_action(backend, key, 1),
                stdscr,
                backend.state,
                selected,
            )
        elif ch in (curses.KEY_LEFT, ord("-")):
            row_key = rows[selected].key
            status = _safe_call(
                lambda key=row_key: apply_key_action(backend, key, -1),
                stdscr,
                backend.state,
                selected,
            )
        elif ch in (ord("r"), ord("R")):
            status = _safe_call(backend.refresh, stdscr, backend.state, selected)
        elif ch in (ord("s"), ord("S")):
            status = _safe_call(backend.save, stdscr, backend.state, selected)
        elif ch in (ord("d"), curses.KEY_BACKSPACE, 127, 8):
            row_key = rows[selected].key
            status = _safe_call(
                lambda key=row_key: reset_row_default(backend, key),
                stdscr,
                backend.state,
                selected,
            )
        elif ch == ord("D"):
            status = _safe_call(backend.reset_defaults, stdscr, backend.state, selected)


def _safe_call(action, stdscr, state: GnssState, selected: int) -> str:
    _draw_window(stdscr, state, selected, "Communicating with MAX-M8Q over serial...")
    try:
        return action()
    except Exception as exc:
        return f"Error: {exc}"


def _draw_window(stdscr, state: GnssState, selected: int, status: str) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    lines = render_screen_lines(state, selected, status, width=max(40, width))
    for row_idx, line in enumerate(lines):
        if row_idx >= height:
            break
        clipped = line[: max(1, width - 1)]
        attr = curses.A_BOLD if row_idx == 0 or line.startswith("> ") else curses.A_NORMAL
        with contextlib.suppress(curses.error):
            stdscr.addstr(row_idx, 0, clipped, attr)
    stdscr.refresh()
