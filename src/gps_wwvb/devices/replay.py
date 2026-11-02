"""Play back files written by the ``record`` command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pyubx2 import UBXReader

from gps_wwvb.devices.compass import make_reading
from gps_wwvb.devices.gps_ubx import parse_nav_pvt
from gps_wwvb.devices.types import CompassReading, GpsFix, WwvbReading


class ReplayGps:
    def __init__(self, path: Path):
        self.frames = _load_gps(path)
        self.index = 0
        self.exhausted = not self.frames

    def read_raw(self, timeout: float = 2.0) -> bytes:
        del timeout
        if self.index >= len(self.frames):
            self.exhausted = True
            return b""
        _stamp, frame = self.frames[self.index]
        self.index += 1
        if self.index >= len(self.frames):
            self.exhausted = True
        return frame

    def read_fix(self, timeout: float = 2.0) -> GpsFix | None:
        del timeout
        if self.index >= len(self.frames):
            self.exhausted = True
            return None
        stamp, frame = self.frames[self.index]
        self.index += 1
        if self.index >= len(self.frames):
            self.exhausted = True
        message = UBXReader.parse(frame)
        return parse_nav_pvt(message, stamp)

    def fix_at(self, when: datetime) -> GpsFix | None:
        """Last NAV-PVT recorded at or before ``when``.

        The monitor uses this so a replay keeps the fix that was current at
        each reception. ``read_fix`` still walks the file in order for ``status``.
        """
        target = when.timestamp()
        chosen: tuple[float, bytes] | None = None
        while self.index < len(self.frames) and self.frames[self.index][0] <= target:
            chosen = self.frames[self.index]
            self.index += 1
        if self.index >= len(self.frames):
            self.exhausted = True
        if chosen is None:
            return None
        stamp, frame = chosen
        return parse_nav_pvt(UBXReader.parse(frame), stamp)

    def close(self) -> None:
        return None


class ReplayCompass:
    def __init__(
        self,
        path: Path,
        *,
        offset: tuple[float, float, float],
        scale: tuple[float, float, float],
    ):
        self.rows = _load_jsonl(path)
        self.index = 0
        self.offset = offset
        self.scale = scale
        self.variant = "replay"
        self.exhausted = not self.rows

    def read_counts(self) -> tuple[float, float, float, float, float, float]:
        row = self._next()
        return (
            float(row["ax"]),
            float(row["ay"]),
            float(row["az"]),
            float(row["mx"]),
            float(row["my"]),
            float(row["mz"]),
        )

    def read(self) -> CompassReading | None:
        if self.index >= len(self.rows):
            self.exhausted = True
            return None
        row = self.rows[self.index]
        self.index += 1
        if self.index >= len(self.rows):
            self.exhausted = True
        return self._reading_from(row)

    def read_at(self, when: datetime) -> CompassReading | None:
        """Last sample recorded at or before ``when``, or None if none qualifies."""
        target = when.timestamp()
        chosen: dict | None = None
        while self.index < len(self.rows) and float(self.rows[self.index]["t"]) <= target:
            chosen = self.rows[self.index]
            self.index += 1
        if self.index >= len(self.rows):
            self.exhausted = True
        if chosen is None:
            return None
        return self._reading_from(chosen)

    def _reading_from(self, row: dict) -> CompassReading:
        return make_reading(
            float(row["ax"]),
            float(row["ay"]),
            float(row["az"]),
            float(row["mx"]),
            float(row["my"]),
            float(row["mz"]),
            offset=self.offset,
            scale=self.scale,
            declination_deg=0.0,
            received_at=float(row["t"]),
        )

    def close(self) -> None:
        return None

    def _next(self) -> dict:
        if self.index >= len(self.rows):
            self.exhausted = True
            raise EOFError(f"no more compass samples (read {self.index})")
        row = self.rows[self.index]
        self.index += 1
        if self.index >= len(self.rows):
            self.exhausted = True
        return row


class ReplayWwvb:
    def __init__(self, path: Path):
        self.rows = _load_jsonl(path)
        self.index = 0
        self.exhausted = not self.rows

    def receive(self) -> WwvbReading | None:
        if self.index >= len(self.rows):
            self.exhausted = True
            return None
        row = self.rows[self.index]
        self.index += 1
        if self.index >= len(self.rows):
            self.exhausted = True
        return WwvbReading(
            wwvb_time=_parse_time(row["wwvb_time"]),
            system_time=_parse_time(row["system_time"]),
            antenna=row.get("antenna"),
            dst=row.get("dst"),
            leap_second=row.get("leap_second"),
            tracking=bool(row.get("tracking", False)),
        )

    def close(self) -> None:
        return None


def _load_gps(path: Path) -> list[tuple[float, bytes]]:
    frames: list[tuple[float, bytes]] = []
    for row in _load_jsonl(path):
        frames.append((float(row["t"]), bytes.fromhex(row["hex"])))
    return frames


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
