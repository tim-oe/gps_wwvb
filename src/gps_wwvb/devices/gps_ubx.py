"""UBX NAV-PVT framing for the simulator and ``record`` / ``replay`` files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pyubx2 import GET, UBXMessage

from gps_wwvb.devices.types import GpsFix


class GpsError(Exception):
    """The GPS reader could not complete a request."""


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
    """Serialize a NAV-PVT frame for tests, the simulator, and ``record``."""
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
