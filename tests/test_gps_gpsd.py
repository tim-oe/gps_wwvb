"""Test the gpsd JSON socket reader."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from pyubx2 import UBXReader

from gps_wwvb.config import ConfigError, load_config
from gps_wwvb.devices.gps_gpsd import GpsdGps, parse_tpv
from gps_wwvb.devices.gps_ubx import GpsError, parse_nav_pvt


class _FakeSocket:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, _n: int) -> bytes:
        if not self._chunks:
            raise TimeoutError("timed out")
        chunk = self._chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def close(self) -> None:
        self.closed = True


def _tpv_line(
    *,
    when: datetime | None = None,
    mode: int = 3,
    lat: float | None = 38.894018,
    lon: float | None = -90.191901,
    alt_msl: float | None = 184.3,
    ept: float | None = 0.000000016,
) -> bytes:
    stamp = (when or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    body: dict[str, object] = {"class": "TPV", "mode": mode, "time": stamp}
    if lat is not None:
        body["lat"] = lat
    if lon is not None:
        body["lon"] = lon
    if alt_msl is not None:
        body["altMSL"] = alt_msl
    if ept is not None:
        body["ept"] = ept
    return (json.dumps(body) + "\n").encode("utf-8")


def _sky_line(used: int = 12, total: int = 15) -> bytes:
    sats = [{"PRN": idx + 1, "used": idx < used} for idx in range(total)]
    body = {"class": "SKY", "uSat": used, "satellites": sats}
    return (json.dumps(body) + "\n").encode("utf-8")


def test_gpsd_reader_sends_watch_and_parses_sky_and_tpv(caplog):
    version = b'{"class":"VERSION","release":"3.25"}\n'
    sock = _FakeSocket([version + _sky_line(14), _tpv_line()])
    gps = GpsdGps(connect=lambda _addr, _timeout: sock)  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG, logger="gps_wwvb.devices.gps_gpsd"):
        fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert sock.sent == [b'?WATCH={"enable":true,"json":true,"timing":true,"pps":true};\n']
    assert fix.position_ok
    assert fix.time_ok
    assert fix.lat == pytest.approx(38.894018)
    assert fix.lon == pytest.approx(-90.191901)
    assert fix.alt_m == pytest.approx(184.3)
    assert fix.num_sv == 14
    assert fix.time_accuracy_ns == 16
    assert any("GPS (gpsd) fix" in rec.message for rec in caplog.records)
    gps.close()
    assert sock.closed


def test_gpsd_uses_pps_and_toff_offsets_over_serial_arrival_delay():
    pps = (
        json.dumps(
            {
                "class": "PPS",
                "real_sec": 1793596837,
                "real_nsec": 0,
                "clock_sec": 1793596837,
                "clock_nsec": 3_687_681,
                "precision": -20,
            }
        )
        + "\n"
    ).encode("utf-8")
    toff = (
        json.dumps(
            {
                "class": "TOFF",
                "real_sec": 1793596837,
                "real_nsec": 0,
                "clock_sec": 1793596837,
                "clock_nsec": 118_824_059,
                "precision": -1,
            }
        )
        + "\n"
    ).encode("utf-8")
    sock = _FakeSocket([pps + toff + _tpv_line(ept=0.005)])
    gps = GpsdGps(connect=lambda _addr, _timeout: sock)  # type: ignore[arg-type]
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.clock_offset_s == pytest.approx(-0.003687681)
    assert fix.time_accuracy_ns == 954

    sock_toff_only = _FakeSocket([toff + _tpv_line(ept=0.005)])
    gps_toff = GpsdGps(connect=lambda _addr, _timeout: sock_toff_only)  # type: ignore[arg-type]
    fix_toff = gps_toff.read_fix(timeout=1.0)
    assert fix_toff is not None
    assert fix_toff.clock_offset_s == pytest.approx(-0.118824059)
    assert fix_toff.time_accuracy_ns == 5_000_000


def test_gpsd_skips_initial_stale_tpv_and_keeps_fresh_one():
    stale = _tpv_line(when=datetime(2026, 1, 1, 0, 0, tzinfo=UTC), lat=10.0, lon=20.0)
    fresh = _tpv_line(when=datetime.now(UTC), lat=38.89, lon=-90.19)
    sock = _FakeSocket([stale + fresh])
    gps = GpsdGps(connect=lambda _addr, _timeout: sock)  # type: ignore[arg-type]
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.lat == pytest.approx(38.89)


def test_gpsd_read_raw_produces_a_valid_nav_pvt():
    sock = _FakeSocket([_sky_line(12) + _tpv_line()])
    gps = GpsdGps(connect=lambda _addr, _timeout: sock)  # type: ignore[arg-type]
    raw = gps.read_raw(timeout=1.0)
    assert raw.startswith(b"\xb5\x62\x01\x07")
    decoded = parse_nav_pvt(UBXReader.parse(raw), 100.0)
    assert decoded is not None
    assert decoded.lat == pytest.approx(38.894018, abs=1e-5)
    assert decoded.num_sv == 12


def test_gpsd_timeout_and_disconnect():
    empty = _FakeSocket([])
    gps = GpsdGps(connect=lambda _addr, _timeout: empty)  # type: ignore[arg-type]
    assert gps.read_fix(timeout=0.1) is None

    closed = _FakeSocket([b""])
    gps2 = GpsdGps(connect=lambda _addr, _timeout: closed)  # type: ignore[arg-type]
    with pytest.raises(GpsError, match="closed"):
        gps2.read_fix(timeout=0.1)


def test_parse_tpv_handles_no_fix_and_satellite_list_fallback():
    no_fix = parse_tpv({"class": "TPV", "mode": 1}, 100.0)
    assert not no_fix.position_ok
    assert not no_fix.time_ok

    sky_no_usat = {
        "class": "SKY",
        "satellites": [{"PRN": 1, "used": True}, {"PRN": 2, "used": False}],
    }
    sock = _FakeSocket([(json.dumps(sky_no_usat) + "\n").encode("utf-8") + _tpv_line()])
    gps = GpsdGps(connect=lambda _addr, _timeout: sock)  # type: ignore[arg-type]
    fix = gps.read_fix(timeout=1.0)
    assert fix is not None
    assert fix.num_sv == 1


def test_config_rejects_invalid_gpsd_port(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[gps]\ngpsd_port = 70000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"gps\.gpsd_port"):
        load_config(bad)
