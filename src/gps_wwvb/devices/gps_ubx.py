"""u-blox MAX-M8Q reader over the I2C DDC port.

The host reads the 16-bit count at registers 0xFD/0xFE, then pulls that many
bytes from register 0xFF. Those bytes are a UART-style stream, parsed with
pyubx2. At startup NMEA and NAV-PVT are turned off on this port so the small
DDC buffer stays empty. A reading turns NAV-PVT on, takes one message, and
turns it off again.
UART rates are left alone: the 3-byte CFG-MSG form applies only to the port
the command arrives on.
"""

from __future__ import annotations

import errno
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from pyubx2 import GET, SET, UBX_PROTOCOL, UBXMessage, UBXReader

from gps_wwvb.devices.types import GpsFix

logger = logging.getLogger(__name__)

# The MAX-M8Q sometimes NACKs a DDC register read (errno 121) while another
# device is using the bus. The following read usually succeeds.
_REMOTE_IO_ATTEMPTS = 4
_REMOTE_IO_PAUSE_S = 0.02
_FIX_LOG_INTERVAL_S = 30.0

# NMEA sentence ids on class 0xF0. Rate 0 disables them on the current port.
_NMEA_OFF = (
    (0xF0, 0x00),  # GGA
    (0xF0, 0x01),  # GLL
    (0xF0, 0x02),  # GSA
    (0xF0, 0x03),  # GSV
    (0xF0, 0x04),  # RMC
    (0xF0, 0x05),  # VTG
)


_T = TypeVar("_T")


class GpsError(Exception):
    """The GPS reader could not complete a request."""


class _Stream:
    """File-like view that blocks until the DDC buffer has the requested bytes."""

    def __init__(self, pull):
        self._pull = pull
        self._buf = bytearray()
        self.deadline: float | None = None

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise TimeoutError("GPS I2C read timed out")
            chunk = self._pull()
            if chunk:
                self._buf.extend(chunk)
            else:
                time.sleep(0.02)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class UbxGps:
    def __init__(self, link, address: int = 0x42, configure: bool = True):
        self.link = link
        self.address = address
        self.exhausted = False
        self._fix_logged_at: float | None = None
        if configure:
            self.drain()
            self.configure()
        self._stream = _Stream(self._pull)
        self._reader = self._new_reader()

    def configure(self) -> None:
        """Disable NMEA and NAV-PVT on this I2C port so the buffer stays empty."""
        for msg_class, msg_id in _NMEA_OFF:
            self._write(cfg_msg_rate(msg_class, msg_id, 0))
        self._set_nav_pvt(False)

    def drain(self) -> None:
        for _ in range(8):
            avail = self.available()
            if not avail:
                return
            self._read_register(0xFF, min(avail, 512))

    def available(self) -> int:
        data = self._read_register(0xFD, 2)
        if len(data) < 2:
            return 0
        count = (data[0] << 8) | data[1]
        if count > 4096:
            return 0
        return count

    def read_raw(self, timeout: float = 2.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            avail = self.available()
            if avail:
                return self._read_register(0xFF, min(avail, 512))
            time.sleep(0.02)
        return b""

    def read_fix(self, timeout: float = 2.0) -> GpsFix | None:
        """Enable NAV-PVT, return one message, then silence the port again."""
        self._reset_parser()
        self._set_nav_pvt(True)
        try:
            return self._read_one(timeout)
        finally:
            self._quiet()
            self._reset_parser()

    def _read_one(self, timeout: float) -> GpsFix | None:
        self._stream.deadline = time.monotonic() + timeout
        while time.monotonic() < self._stream.deadline:
            try:
                _raw, msg = self._reader.read()
            except TimeoutError:
                return None
            except Exception as exc:
                # A garbled frame or a dead bus used to look like "no fix".
                self._reader = self._new_reader()
                raise GpsError(f"NAV-PVT read failed: {exc}") from exc
            if msg is not None and getattr(msg, "identity", "") == "NAV-PVT":
                fix = parse_nav_pvt(msg, time.time())
                if fix is not None:
                    self._log_fix(fix, _raw)
                return fix
        return None

    def close(self) -> None:
        close = getattr(self.link, "close", None)
        if close:
            close()

    def _pull(self) -> bytes:
        avail = self.available()
        if not avail:
            return b""
        return self._read_register(0xFF, min(avail, 512))

    def _read_register(self, reg: int, n: int) -> bytes:
        return _retry_remote_io(lambda: self.link.read_at(self.address, reg, n))

    def _write(self, frame: bytes) -> None:
        _retry_remote_io(lambda: self.link.write_at(self.address, 0xFF, frame))

    def _set_nav_pvt(self, enabled: bool) -> None:
        self._write(cfg_msg_rate(0x01, 0x07, 1 if enabled else 0))

    def _quiet(self) -> None:
        try:
            self._set_nav_pvt(False)
            self.drain()
        except OSError as exc:
            logger.warning("failed to silence GPS output: %s", exc)

    def _reset_parser(self) -> None:
        self._stream = _Stream(self._pull)
        self._reader = self._new_reader()

    def _new_reader(self) -> UBXReader:
        return UBXReader(self._stream, protfilter=UBX_PROTOCOL, quitonerror=0)

    def _log_fix(self, fix: GpsFix, raw: bytes) -> None:
        """DEBUG names the fix and dumps the frame. At most once per 30 s."""
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
            "GPS fix lat %s lon %s alt %s m time %s sv %s acc %s",
            lat,
            lon,
            alt,
            utc,
            fix.num_sv,
            acc,
        )
        logger.debug(
            "GPS NAV-PVT %s type %s date %s time %s resolved %s gnss %s",
            raw.hex(),
            fix.fix_type,
            int(fix.valid_date),
            int(fix.valid_time),
            int(fix.fully_resolved),
            int(fix.gnss_fix_ok),
        )


def _retry_remote_io(action: Callable[[], _T]) -> _T:
    """Run ``action`` again when the module answers with errno 121."""
    attempt = 0
    while True:
        try:
            return action()
        except OSError as exc:
            attempt += 1
            if exc.errno != errno.EREMOTEIO or attempt >= _REMOTE_IO_ATTEMPTS:
                raise
            time.sleep(_REMOTE_IO_PAUSE_S)


def cfg_msg_rate(msg_class: int, msg_id: int, rate: int) -> bytes:
    """UBX-CFG-MSG that sets the rate on the port the command is sent to."""
    message = UBXMessage(
        "CFG",
        "CFG-MSG",
        SET,
        payload=bytes([msg_class & 0xFF, msg_id & 0xFF, rate & 0xFF]),
    )
    return message.serialize()


def build_nav_pvt(
    *,
    lat: float,
    lon: float,
    alt_m: float,
    when: datetime,
    nano_ns: int = 0,
    tacc_ns: int = 50,
    fix_type: int = 3,
    num_sv: int = 8,
    gnss_fix_ok: int = 1,
) -> bytes:
    """Serialize a NAV-PVT the way the receiver would, for tests and the simulator."""
    when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    alt_mm = round(alt_m * 1000)
    message = UBXMessage(
        "NAV",
        "NAV-PVT",
        GET,
        year=when.year,
        month=when.month,
        day=when.day,
        hour=when.hour,
        min=when.minute,
        second=when.second,
        nano=int(nano_ns),
        lat=float(lat),
        lon=float(lon),
        hMSL=alt_mm,
        height=alt_mm,
        fixType=int(fix_type),
        numSV=int(num_sv),
        tAcc=int(tacc_ns),
        validDate=1,
        validTime=1,
        fullyResolved=1,
        gnssFixOk=int(gnss_fix_ok),
    )
    return message.serialize()


def parse_nav_pvt(msg, received_at: float) -> GpsFix | None:
    if getattr(msg, "identity", "") != "NAV-PVT":
        return None
    utc = _utc_from_message(msg)
    hmsl = getattr(msg, "hMSL", None)
    alt_m = None if hmsl is None else float(hmsl) / 1000.0
    tacc = getattr(msg, "tAcc", None)
    return GpsFix(
        lat=float(msg.lat),
        lon=float(msg.lon),
        alt_m=alt_m,
        utc=utc,
        fix_type=int(msg.fixType),
        valid_date=bool(msg.validDate),
        valid_time=bool(msg.validTime),
        fully_resolved=bool(msg.fullyResolved),
        gnss_fix_ok=bool(getattr(msg, "gnssFixOk", 0)),
        time_accuracy_ns=None if tacc is None else int(tacc),
        num_sv=int(msg.numSV),
        received_at=received_at,
    )


def extract_ubx_frames(buf: bytearray) -> list[bytes]:
    """Pull complete, checksum-valid UBX frames out of ``buf``."""
    frames: list[bytes] = []
    while True:
        if len(buf) < 2:
            return frames
        start = buf.find(b"\xb5\x62")
        if start < 0:
            if buf[-1] == 0xB5:
                del buf[:-1]
            else:
                buf.clear()
            return frames
        if start:
            del buf[:start]
        if len(buf) < 6:
            return frames
        length = buf[4] | (buf[5] << 8)
        if length > 2048:
            del buf[:2]
            continue
        total = 6 + length + 2
        if len(buf) < total:
            return frames
        frame = bytes(buf[:total])
        ck_a, ck_b = ubx_checksum(frame[2:-2])
        if frame[-2] != ck_a or frame[-1] != ck_b:
            del buf[:2]
            continue
        del buf[:total]
        frames.append(frame)


def ubx_checksum(body: bytes) -> tuple[int, int]:
    ck_a = 0
    ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def _utc_from_message(msg) -> datetime | None:
    try:
        year = int(msg.year)
        if year < 2000:
            return None
        base = datetime(
            year,
            int(msg.month),
            int(msg.day),
            int(msg.hour),
            int(msg.min),
            int(msg.second),
            tzinfo=UTC,
        )
    except (TypeError, ValueError):
        return None
    nano = int(getattr(msg, "nano", 0) or 0)
    return base + timedelta(microseconds=nano / 1000)
