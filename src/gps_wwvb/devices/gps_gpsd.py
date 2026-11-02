"""gpsd JSON reader over a localhost TCP socket.

The MAX-M8Q's UART is fed to ``gpsd``. Reading ``TPV`` and ``SKY`` reports
from ``gpsd`` avoids the Pi Zero's I2C clock-stretching bug on the DDC port
and lets ``cgps``, ``gpsmon``, and ``chrony`` share the receiver.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime

from gps_wwvb.devices.gps_ubx import GpsError, build_nav_pvt
from gps_wwvb.devices.types import GpsFix

logger = logging.getLogger(__name__)

_WATCH = b'?WATCH={"enable":true,"json":true,"timing":true,"pps":true};\n'
_FIX_LOG_INTERVAL_S = 30.0
_MAX_STALE_REPORT_S = 2.0
_TIMING_MAX_AGE_S = 5.0

ConnectFn = Callable[[tuple[str, int], float], socket.socket]


class GpsdGps:
    """Read fixes from a running ``gpsd`` daemon."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2947,
        *,
        connect: ConnectFn | None = None,
    ):
        self.host = host
        self.port = port
        self.exhausted = False
        self.last_count: int | None = None
        self._connect: ConnectFn = connect or socket.create_connection
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._num_sv: int | None = None
        self._pps_offset_s: float | None = None
        self._pps_tacc_ns: int | None = None
        self._pps_at: float | None = None
        self._toff_offset_s: float | None = None
        self._toff_at: float | None = None
        self._fix_logged_at: float | None = None

    def read_fix(self, timeout: float = 2.0) -> GpsFix | None:
        """Return the next fresh ``TPV`` report from ``gpsd``."""
        deadline = time.monotonic() + timeout
        dropped_stale = False
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            line = self._read_line(remaining)
            if line is None:
                return None
            self.last_count = len(line)
            msg = _parse_json(line)
            if msg is None:
                continue
            kind = msg.get("class")
            if kind == "SKY":
                sats = _count_used_satellites(msg)
                if sats is not None:
                    self._num_sv = sats
                continue
            if kind in ("PPS", "TOFF"):
                self._note_timing(kind, msg)
                continue
            if kind != "TPV":
                continue
            now = time.time()
            offset_s, pps_tacc_ns = self._current_timing()
            fix = parse_tpv(
                msg,
                now,
                num_sv=self._num_sv,
                clock_offset_s=offset_s,
                pps_tacc_ns=pps_tacc_ns,
            )
            if (
                not dropped_stale
                and fix.utc is not None
                and abs(now - fix.utc.timestamp()) > _MAX_STALE_REPORT_S
                and time.monotonic() + 0.2 < deadline
            ):
                dropped_stale = True
                continue
            self._log_fix(fix)
            return fix
        return None

    def read_raw(self, timeout: float = 2.0) -> bytes:
        """Serialize a ``read_fix`` result as a NAV-PVT frame for ``record``."""
        fix = self.read_fix(timeout=timeout)
        if fix is None or fix.utc is None or fix.lat is None or fix.lon is None:
            return b""
        return build_nav_pvt(
            lat=fix.lat,
            lon=fix.lon,
            alt_m=0.0 if fix.alt_m is None else fix.alt_m,
            when=fix.utc,
            nano_ns=fix.utc.microsecond * 1000,
            tacc_ns=50 if fix.time_accuracy_ns is None else fix.time_accuracy_ns,
            fix_type=fix.fix_type,
            num_sv=0 if fix.num_sv is None else fix.num_sv,
            gnss_fix_ok=int(fix.gnss_fix_ok),
        )

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        self._buf.clear()
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _ensure_socket(self, timeout: float) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            sock = self._connect((self.host, self.port), timeout)
            sock.settimeout(timeout)
            sock.sendall(_WATCH)
        except OSError as exc:
            raise GpsError(f"gpsd {self.host}:{self.port} failed: {exc}") from exc
        self._sock = sock
        self._buf.clear()
        return sock

    def _read_line(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline]).strip()
                del self._buf[: newline + 1]
                if line:
                    return line
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock = self._ensure_socket(remaining)
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
            except TimeoutError:
                return None
            except OSError as exc:
                self.close()
                raise GpsError(f"gpsd read failed: {exc}") from exc
            if not chunk:
                self.close()
                raise GpsError("gpsd closed the connection")
            self._buf.extend(chunk)

    def _note_timing(self, kind: str, msg: dict) -> None:
        offset_s = parse_timing_offset(msg)
        if offset_s is None:
            return
        now = time.monotonic()
        if kind == "PPS":
            self._pps_offset_s = offset_s
            self._pps_tacc_ns = _precision_to_ns(msg.get("precision"))
            self._pps_at = now
        else:
            self._toff_offset_s = offset_s
            self._toff_at = now

    def _current_timing(self) -> tuple[float | None, int | None]:
        now = time.monotonic()
        if (
            self._pps_at is not None
            and self._pps_offset_s is not None
            and now - self._pps_at <= _TIMING_MAX_AGE_S
        ):
            return self._pps_offset_s, self._pps_tacc_ns
        if (
            self._toff_at is not None
            and self._toff_offset_s is not None
            and now - self._toff_at <= _TIMING_MAX_AGE_S
        ):
            return self._toff_offset_s, None
        return None, None

    def _log_fix(self, fix: GpsFix) -> None:
        now = time.monotonic()
        logged = self._fix_logged_at
        if logged is not None and now - logged < _FIX_LOG_INTERVAL_S:
            return
        self._fix_logged_at = now
        utc = "n/a" if fix.utc is None else fix.utc.isoformat()
        lat = "n/a" if fix.lat is None else f"{fix.lat:.6f}"
        lon = "n/a" if fix.lon is None else f"{fix.lon:.6f}"
        alt = "n/a" if fix.alt_m is None else f"{fix.alt_m:.1f}"
        acc = "n/a" if fix.time_accuracy_ns is None else f"{fix.time_accuracy_ns} ns"
        logger.debug(
            "GPS (gpsd) fix lat %s lon %s alt %s m time %s sv %s acc %s",
            lat,
            lon,
            alt,
            utc,
            fix.num_sv,
            acc,
        )


def parse_tpv(
    msg: dict,
    received_at: float,
    *,
    num_sv: int | None = None,
    clock_offset_s: float | None = None,
    pps_tacc_ns: int | None = None,
) -> GpsFix:
    """Convert a ``gpsd`` ``TPV`` object into a :class:`GpsFix`."""
    mode = _int_or(msg.get("mode"), 0)
    fix_type = mode if mode in (2, 3) else 0
    lat = _float_or_none(msg.get("lat"))
    lon = _float_or_none(msg.get("lon"))
    alt = _float_or_none(msg.get("altMSL"))
    if alt is None:
        alt = _float_or_none(msg.get("alt"))
    utc = _parse_utc(msg.get("time"))
    if pps_tacc_ns is not None:
        tacc_ns: int | None = pps_tacc_ns
    else:
        ept = _float_or_none(msg.get("ept"))
        tacc_ns = None if ept is None or ept < 0 else round(ept * 1_000_000_000)
    sats = _int_or_none(msg.get("numSV"))
    if sats is None:
        sats = _int_or_none(msg.get("sats"))
    if sats is None:
        sats = num_sv
    has_pos = fix_type in (2, 3) and lat is not None and lon is not None
    has_time = utc is not None and utc.year >= 2000 and mode >= 1
    if clock_offset_s is None and has_time and utc is not None:
        rtime = _float_or_none(msg.get("rtime"))
        if rtime is not None and abs(received_at - rtime) <= _TIMING_MAX_AGE_S:
            # TPV arrived after the NMEA burst; without PPS/TOFF, system clock
            # discipline (chrony/ntpd) is much closer than NMEA burst completion.
            clock_offset_s = 0.0
    return GpsFix(
        lat=lat,
        lon=lon,
        alt_m=alt,
        utc=utc,
        fix_type=fix_type,
        valid_date=has_time,
        valid_time=has_time,
        fully_resolved=has_time,
        gnss_fix_ok=has_pos,
        time_accuracy_ns=tacc_ns,
        num_sv=sats,
        received_at=received_at,
        clock_offset_s=clock_offset_s,
    )


def parse_timing_offset(msg: dict) -> float | None:
    """Return ``real - clock`` in seconds from a ``PPS`` or ``TOFF`` message."""
    real_sec = _int_or_none(msg.get("real_sec"))
    real_nsec = _float_or_none(msg.get("real_nsec"))
    clock_sec = _int_or_none(msg.get("clock_sec"))
    clock_nsec = _float_or_none(msg.get("clock_nsec"))
    if real_sec is None or real_nsec is None or clock_sec is None or clock_nsec is None:
        return None
    return (real_sec - clock_sec) + (real_nsec - clock_nsec) * 1e-9


def _precision_to_ns(value: object) -> int | None:
    prec = _int_or_none(value)
    if prec is None or not -40 <= prec <= 10:
        return None
    return max(1, round((2.0**prec) * 1_000_000_000))


def _count_used_satellites(msg: dict) -> int | None:
    u_sat = _int_or_none(msg.get("uSat"))
    if u_sat is not None:
        return u_sat
    sats = msg.get("satellites")
    if not isinstance(sats, list):
        return None
    return sum(1 for item in sats if isinstance(item, dict) and item.get("used") is True)


def _parse_json(line: bytes) -> dict | None:
    try:
        parsed = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year < 2000:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _int_or(value: object, default: int) -> int:
    parsed = _int_or_none(value)
    return default if parsed is None else parsed
