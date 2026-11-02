"""Parse recorded NAV-PVT bytes through the I2C reader."""

import ctypes
import errno
import logging
from datetime import UTC, datetime

import pytest

from gps_wwvb.devices.gps_ubx import (
    GpsError,
    UbxGps,
    build_nav_pvt,
    cfg_msg_rate,
    extract_ubx_frames,
)
from gps_wwvb.devices.i2c import BufferLink, Smbus2Link
from gps_wwvb.devices.types import GpsFix

# A NAV-PVT captured from pyubx2 for 2026-11-01 12:00:00.123 UTC at San Jose.
RECORDED_NAV_PVT = bytes.fromhex(
    "b56201075c0000000000ea070b010c00000732000000c0d4540703010008"
    "a22b53b760264516d4490000d44900000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000"
    "00000000000000009310"
)


def test_cfg_msg_rate_is_the_three_byte_form():
    frame = cfg_msg_rate(0x01, 0x07, 1)
    assert frame == bytes.fromhex("b562060103000107011351")


def test_read_fix_enables_one_nav_pvt_and_then_silences_the_port():
    link = BufferLink(RECORDED_NAV_PVT)
    gps = UbxGps(link, configure=False)
    assert gps.read_fix(timeout=1.0) is not None
    assert link.writes[0] == cfg_msg_rate(0x01, 0x07, 1)
    assert link.writes[-1] == cfg_msg_rate(0x01, 0x07, 0)


def test_a_nav_pvt_logs_the_fix_and_dumps_the_frame(caplog):
    link = BufferLink(RECORDED_NAV_PVT)
    gps = UbxGps(link, configure=False)
    with caplog.at_level(logging.DEBUG, logger="gps_wwvb.devices.gps_ubx"):
        fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    debug = [record.message for record in caplog.records if record.levelname == "DEBUG"]
    assert any("GPS fix lat 37.363056" in message for message in debug)
    assert any(RECORDED_NAV_PVT.hex() in message for message in debug)
    caplog.clear()
    link.feed(RECORDED_NAV_PVT)
    assert gps.read_fix(timeout=1.0) is not None
    assert not [record for record in caplog.records if record.levelname in ("INFO", "DEBUG")]


def test_recorded_nav_pvt_round_trip():
    link = BufferLink()
    gps = UbxGps(link, address=0x42, configure=True)
    assert cfg_msg_rate(0x01, 0x07, 0) in link.writes
    assert cfg_msg_rate(0x01, 0x07, 1) not in link.writes
    link.feed(RECORDED_NAV_PVT)
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.lat == pytest.approx(37.363056, abs=1e-6)
    assert fix.lon == pytest.approx(-121.928611, abs=1e-6)
    assert fix.alt_m == pytest.approx(18.9, abs=1e-3)
    assert fix.utc == datetime(2026, 11, 1, 12, 0, 0, 123000, tzinfo=UTC)
    assert fix.fix_type == 3
    assert fix.num_sv == 8
    assert fix.time_accuracy_ns == 50
    assert fix.time_ok
    assert fix.position_ok


def test_builder_matches_the_parser():
    when = datetime(2026, 6, 1, 3, 4, 5, tzinfo=UTC)
    raw = build_nav_pvt(lat=40.0, lon=-105.0, alt_m=1609.0, when=when, nano_ns=250_000_000)
    link = BufferLink(raw)
    gps = UbxGps(link, configure=False)
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.lat == pytest.approx(40.0)
    assert fix.lon == pytest.approx(-105.0)
    assert fix.alt_m == pytest.approx(1609.0)
    assert fix.utc == datetime(2026, 6, 1, 3, 4, 5, 250000, tzinfo=UTC)


def test_frame_extractor_skips_noise():
    buf = bytearray(b"\xff\x00" + RECORDED_NAV_PVT + b"\xb5")
    frames = extract_ubx_frames(buf)
    assert frames == [RECORDED_NAV_PVT]
    assert buf == b"\xb5"


def test_frame_extractor_drops_a_bad_checksum_and_an_absurd_length():
    bad = bytearray(RECORDED_NAV_PVT)
    bad[-1] ^= 0xFF
    assert not extract_ubx_frames(bad)
    huge = bytearray(b"\xb5\x62\x01\x07\x01\x08" + b"\x00" * 8)
    assert not extract_ubx_frames(huge)


def test_unresolved_year_is_not_a_time_fix():
    when = datetime(1999, 1, 1, tzinfo=UTC)
    raw = build_nav_pvt(lat=40.0, lon=-105.0, alt_m=1.0, when=when)
    link = BufferLink(raw)
    gps = UbxGps(link, configure=False)
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.utc is None
    assert not fix.time_ok


def test_fix_flags_reject_time_only_and_an_unresolved_clock():
    base = {
        "lat": 1.0,
        "lon": 2.0,
        "alt_m": 3.0,
        "utc": datetime(2026, 1, 1, tzinfo=UTC),
        "valid_date": True,
        "valid_time": True,
        "fully_resolved": True,
        "gnss_fix_ok": True,
        "time_accuracy_ns": 1,
        "num_sv": 4,
        "received_at": 0.0,
    }
    assert not GpsFix(fix_type=1, **base).position_ok
    assert not GpsFix(fix_type=5, **base).position_ok
    assert GpsFix(fix_type=3, **base).position_ok
    assert not GpsFix(fix_type=3, **(base | {"fully_resolved": False})).time_ok


def test_remote_io_on_the_count_register_is_retried():
    link = _NackLink(BufferLink(RECORDED_NAV_PVT), nacks=1)
    gps = UbxGps(link, configure=False)
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.position_ok
    assert link.nacks == 1


def test_remote_io_that_persists_is_still_an_error():
    link = _NackLink(BufferLink(RECORDED_NAV_PVT), nacks=10)
    gps = UbxGps(link, configure=False)
    with pytest.raises(GpsError, match="Remote I/O error"):
        gps.read_fix(timeout=1.0)
    # Four tries for the sample, then four more while silencing the port.
    assert link.nacks == 8


def test_smbus2_link_addresses_the_ddc_registers():
    bus = _FakeBus()
    link = Smbus2Link(bus)
    assert link.read_at(0x42, 0xFD, 2) == b"\x00\x08"
    assert bus.writes[0] == bytes([0xFD])
    link.write_at(0x42, 0xFF, b"\xb5\x62")
    assert bus.writes[1] == bytes([0xFF, 0xB5, 0x62])


class _NackLink:
    """Fail the byte-count read with errno 121, then pass through."""

    def __init__(self, inner: BufferLink, nacks: int):
        self.inner = inner
        self.remaining = nacks
        self.nacks = 0

    def read_at(self, addr: int, reg: int, n: int) -> bytes:
        if reg == 0xFD and self.remaining:
            self.remaining -= 1
            self.nacks += 1
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        return self.inner.read_at(addr, reg, n)

    def write_at(self, addr: int, reg: int, data: bytes) -> None:
        self.inner.write_at(addr, reg, data)


class _FakeBus:
    def __init__(self):
        self.writes: list[bytes] = []
        self.read_data = b"\x00\x08"

    def i2c_rdwr(self, *messages):
        for message in messages:
            if message.flags & 1:
                data = self.read_data[: len(message)]
                ctypes.memmove(message.buf, data, len(data))
            else:
                self.writes.append(bytes(message))
