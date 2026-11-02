"""Test UBX NAV-PVT framing."""

from datetime import UTC, datetime

import pytest
from pyubx2 import UBXReader

from gps_wwvb.devices.gps_ubx import (
    build_nav_pvt,
    extract_ubx_frames,
    parse_nav_pvt,
)
from gps_wwvb.devices.types import GpsFix

# A NAV-PVT captured from pyubx2 for 2026-11-01 12:00:00.123 UTC at San Jose.
RECORDED_NAV_PVT = bytes.fromhex(
    "b56201075c0000000000ea070b010c00000732000000c0d4540703010008"
    "a22b53b760264516d4490000d44900000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000"
    "00000000000000009310"
)


def test_recorded_nav_pvt_round_trip():
    fix = parse_nav_pvt(UBXReader.parse(RECORDED_NAV_PVT), 100.0)
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
    fix = parse_nav_pvt(UBXReader.parse(raw), 100.0)
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
    fix = parse_nav_pvt(UBXReader.parse(raw), 100.0)
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
