"""TOML configuration for device addresses, calibration, and logging."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


# The service reads this file. It stays root-owned; calibration is stored beside it.
SYSTEM_CONFIG = Path("/etc/gps_wwvb.toml")
SYSTEM_STATE = Path("/var/lib/gps-wwvb/calibration.toml")

_SECTION = re.compile(r"(?m)^\[([A-Za-z0-9_-]+)\][^\n]*$")


@dataclass(frozen=True)
class Config:
    path: Path | None
    i2c_bus: int
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    compass_accel_address: int = 0x19
    compass_mag_address: int = 0x1E
    mag_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mag_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    compass_calibrated: bool = False
    es100_address: int = 0x32
    es100_irq: int = 11
    es100_en: int = 7
    log_path: Path | None = None
    log_format: str = "jsonl"
    sim_lat: float = 37.363056
    sim_lon: float = -121.928611
    sim_alt_m: float = 18.9
    sim_gps_interval_s: float = 0.2
    sim_compass_interval_s: float = 0.05
    sim_wwvb_interval_s: float = 0.2


def search_paths() -> list[Path]:
    """Default files, in order.

    The working directory is not searched. Pass ``--config`` for a project
    file. ``tomllib`` cannot execute code, but an implicit file in whatever
    directory the program was started from is easy to pick up by accident.
    """
    return [
        Path.home() / ".config" / "gps_wwvb.toml",
        SYSTEM_CONFIG,
    ]


def config_file(config: Config) -> Path:
    """Config path to show in messages and to pass back to ``--config``."""
    if config.path is not None:
        return config.path
    return Path.home() / ".config" / "gps_wwvb.toml"


def load_config(path: Path | None = None) -> Config:
    """Load ``path`` or the first default location that exists.

    When nothing is found, built-in defaults are used and ``path`` is None.
    Calibration for ``/etc/gps_wwvb.toml`` is merged from the state file.
    """
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        chosen: Path | None = path
    else:
        chosen = next((candidate for candidate in search_paths() if candidate.is_file()), None)
    data: dict = {}
    if chosen is not None:
        data = _read_toml(chosen)
        overlay = _calibration_overlay(chosen)
        if overlay is not None and overlay.is_file():
            _merge(data, _read_toml(overlay))
    try:
        return _from_mapping(data, chosen)
    except (TypeError, ValueError, AttributeError) as exc:
        where = chosen if chosen is not None else "built-in defaults"
        raise ConfigError(f"invalid configuration ({where}): {exc}") from exc


def calibration_target(path: Path) -> Path:
    """Where compass calibration for ``path`` is written.

    The system config stays root-owned. Its offsets and ``calibrated`` flag
    live in ``/var/lib/gps-wwvb/calibration.toml`` (or ``GPS_WWVB_STATE``).
    Every other file keeps those values in itself.
    """
    if path != SYSTEM_CONFIG:
        return path
    override = os.environ.get("GPS_WWVB_STATE")
    if override:
        return Path(override)
    return SYSTEM_STATE


def update_compass_calibration(
    path: Path,
    offset: tuple[float, float, float],
    scale: tuple[float, float, float],
) -> Path:
    """Write hard-iron offsets and scales. A new sweep clears ``calibrated``.

    Returns the file that was written.
    """
    target = calibration_target(path)
    _write_compass_entries(
        target,
        {
            "mag_offset_x": f"{offset[0]:.6f}",
            "mag_offset_y": f"{offset[1]:.6f}",
            "mag_offset_z": f"{offset[2]:.6f}",
            "mag_scale_x": f"{scale[0]:.6f}",
            "mag_scale_y": f"{scale[1]:.6f}",
            "mag_scale_z": f"{scale[2]:.6f}",
            "calibrated": "false",
        },
    )
    return target


def set_compass_calibrated(path: Path, calibrated: bool) -> Path:
    """Record whether +X, the open end of the V, has been aimed at WWVB."""
    target = calibration_target(path)
    _write_compass_entries(target, {"calibrated": "true" if calibrated else "false"})
    return target


def _calibration_overlay(path: Path) -> Path | None:
    target = calibration_target(path)
    if target == path:
        return None
    return target


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"invalid TOML in {path}: expected a table")
    return loaded


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        current = base.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            current.update(value)
        else:
            base[key] = value


def _split_compass(text: str) -> tuple[str, str, str]:
    """Return ``(before, compass body, after)``.

    ``before`` ends at the ``[compass]`` heading. Keys are edited only inside
    the body, so a ``calibrated`` key in another table is left alone.
    """
    match = next((item for item in _SECTION.finditer(text) if item.group(1) == "compass"), None)
    if match is None:
        base = text.rstrip()
        prefix = f"{base}\n\n" if base else ""
        return f"{prefix}[compass]", "\n", ""
    start = match.end()
    nxt = _SECTION.search(text, start)
    end = nxt.start() if nxt else len(text)
    return text[:start], text[start:end], text[end:]


def _write_compass_entries(path: Path, entries: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    head, block, tail = _split_compass(text)
    missing: list[str] = []
    for key, rendered in entries.items():
        line = f"{key} = {rendered}"
        pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
        if pattern.search(block):
            block = pattern.sub(line, block, count=1)
        else:
            missing.append(line)
    if missing:
        if not block.endswith("\n"):
            block += "\n"
        block += "\n".join(missing) + "\n"
    text = head + block + tail
    if not text.endswith("\n"):
        text += "\n"
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _from_mapping(data: dict, path: Path | None) -> Config:
    i2c = data.get("i2c", {})
    gps = data.get("gps", {})
    compass = data.get("compass", {})
    es100 = data.get("es100", {})
    log = data.get("log", {})
    simulate = data.get("simulate", {})
    log_raw = log.get("path", "gps_wwvb.jsonl")
    log_path = None if log_raw in ("", None) else Path(str(log_raw))
    log_format = str(log.get("format", "jsonl")).lower()
    if log_format not in {"jsonl", "csv"}:
        raise ConfigError(f"log format must be jsonl or csv, not {log_format!r}")
    gpsd_port = _parse_int(gps.get("gpsd_port", gps.get("port", 2947)))
    if not 1 <= gpsd_port <= 65535:
        raise ConfigError(f"gps.gpsd_port must be 1-65535, not {gpsd_port}")
    return Config(
        path=path,
        i2c_bus=_address(i2c.get("bus", 1), "i2c.bus"),
        gpsd_host=str(gps.get("gpsd_host", gps.get("host", "127.0.0.1"))),
        gpsd_port=gpsd_port,
        compass_accel_address=_address(compass.get("accel_address", 0x19), "compass.accel_address"),
        compass_mag_address=_address(compass.get("mag_address", 0x1E), "compass.mag_address"),
        mag_offset=(
            float(compass.get("mag_offset_x", 0.0)),
            float(compass.get("mag_offset_y", 0.0)),
            float(compass.get("mag_offset_z", 0.0)),
        ),
        mag_scale=(
            float(compass.get("mag_scale_x", 1.0)),
            float(compass.get("mag_scale_y", 1.0)),
            float(compass.get("mag_scale_z", 1.0)),
        ),
        compass_calibrated=compass.get("calibrated") is True,
        es100_address=_address(es100.get("address", 0x32), "es100.address"),
        es100_irq=int(es100.get("irq", 11)),
        es100_en=int(es100.get("en", 7)),
        log_path=log_path,
        log_format=log_format,
        sim_lat=float(simulate.get("lat", 37.363056)),
        sim_lon=float(simulate.get("lon", -121.928611)),
        sim_alt_m=float(simulate.get("alt_m", 18.9)),
        sim_gps_interval_s=float(simulate.get("gps_interval_s", 0.2)),
        sim_compass_interval_s=float(simulate.get("compass_interval_s", 0.05)),
        sim_wwvb_interval_s=float(simulate.get("wwvb_interval_s", 0.2)),
    )


def _parse_int(value: object) -> int:
    if isinstance(value, str):
        return int(value, 0)
    if isinstance(value, int):
        return value
    raise TypeError(value)


def _address(value: object, label: str) -> int:
    try:
        parsed = _parse_int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} is not an integer: {value!r}") from exc
    if label != "i2c.bus" and not 0 <= parsed <= 127:
        raise ConfigError(f"{label} must be an I2C address 0-127, not {parsed}")
    return parsed
