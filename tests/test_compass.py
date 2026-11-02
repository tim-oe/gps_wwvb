"""Tilt-compensated heading and the LSM303 register map."""

import math
from pathlib import Path

import pytest

from gps_wwvb.config import load_config, set_compass_calibrated, update_compass_calibration
from gps_wwvb.devices.compass import Lsm303, hard_iron_fit, heading_deg, make_reading

ROOT = Path(__file__).resolve().parents[1]


def test_flat_headings_are_clockwise_from_north():
    assert heading_deg(0, 0, 1, 1, 0, 0) == pytest.approx(0.0, abs=1e-6)
    assert heading_deg(0, 0, 1, 0, 1, 0) == pytest.approx(90.0, abs=1e-6)
    assert heading_deg(0, 0, 1, -1, 0, 0) == pytest.approx(180.0, abs=1e-6)
    assert heading_deg(0, 0, 1, 0, -1, 0) == pytest.approx(270.0, abs=1e-6)


def test_tilt_does_not_change_a_known_heading():
    pitch = math.radians(30)
    accel = (-math.sin(pitch), 0.0, math.cos(pitch))
    north = (math.cos(pitch), 0.0, math.sin(pitch))
    east = (0.0, 1.0, 0.0)
    assert heading_deg(*accel, *north) == pytest.approx(0.0, abs=1e-6)
    assert heading_deg(*accel, *east) == pytest.approx(90.0, abs=1e-6)


def test_declination_east_is_added():
    reading = make_reading(0, 0, 1, 1, 0, 0, offset=(0, 0, 0), scale=(1, 1, 1), declination_deg=10)
    assert reading.heading_mag == pytest.approx(0.0, abs=1e-6)
    assert reading.heading_true == pytest.approx(10.0, abs=1e-6)


def test_hard_iron_offsets_and_scales():
    samples = [(-50.0, -20.0, -30.0), (150.0, 20.0, 30.0)]
    samples.extend([samples[0]] * 6)
    offset, scale = hard_iron_fit(samples)
    assert offset == pytest.approx((50.0, 0.0, 0.0))
    assert scale == pytest.approx((0.5, 2.5, 50.0 / 30.0))


def test_hard_iron_is_removed_before_the_heading():
    # A north-pointing field sitting on a +100 count X bias.
    reading = make_reading(
        0,
        0,
        1000,
        500,
        0,
        0,
        offset=(100.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        declination_deg=0.0,
    )
    assert reading.heading_mag == pytest.approx(0.0, abs=1e-6)
    assert reading.mx == pytest.approx(400.0)


def test_agr_and_dlhc_register_reads():
    agr = _compass(agr=True)
    assert agr.variant == "agr"
    reading = agr.read()
    assert reading.heading_mag == pytest.approx(0.0, abs=0.1)
    # Raw +Y is west in the accelerometer frame, so the driver negates it.
    _put_le(agr.link, 0x1E, 0x68, 0, 400, 0)
    assert agr.read().heading_mag == pytest.approx(270.0, abs=0.1)

    dlhc = _compass(agr=False)
    assert dlhc.variant == "dlhc"
    reading = dlhc.read()
    assert reading.heading_mag == pytest.approx(0.0, abs=0.1)


def test_calibration_update_rewrites_the_compass_section(tmp_path: Path):
    path = tmp_path / "gps_wwvb.toml"
    path.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    update_compass_calibration(path, (1.5, -2.0, 3.25), (1.1, 0.9, 1.0))
    loaded = load_config(path)
    assert loaded.mag_offset == pytest.approx((1.5, -2.0, 3.25))
    assert loaded.mag_scale == pytest.approx((1.1, 0.9, 1.0))
    assert loaded.compass_calibrated is False
    assert loaded.es100_irq == 11
    set_compass_calibrated(path, True)
    assert load_config(path).compass_calibrated is True


class _MemoryLink:
    def __init__(self):
        self.mem: dict[tuple[int, int], int] = {}

    def read_at(self, addr: int, reg: int, n: int) -> bytes:
        base = reg & 0x7F if reg & 0x80 else reg
        return bytes(self.mem.get((addr, (base + i) & 0xFF), 0) for i in range(n))

    def write_at(self, addr: int, reg: int, data: bytes) -> None:
        for index, value in enumerate(data):
            self.mem[(addr, (reg + index) & 0xFF)] = value

    def close(self) -> None:
        return None


def _compass(agr: bool) -> Lsm303:
    link = _MemoryLink()
    if agr:
        link.mem[(0x1E, 0x4F)] = 0x40
        _put_le(link, 0x19, 0x28, 0, 0, 1000)
        _put_le(link, 0x1E, 0x68, 400, 0, 0)
    else:
        _put_le(link, 0x19, 0x28, 0, 0, 1000)
        _put_be_xyz_dlhc(link, 0x1E, 0x03, 400, 0, 0)
    return Lsm303(link, 0x19, 0x1E)


def _put_le(link: _MemoryLink, addr: int, reg: int, x: int, y: int, z: int) -> None:
    raw = b"".join(_le(value) for value in (x, y, z))
    for index, value in enumerate(raw):
        link.mem[(addr, reg + index)] = value


def _put_be_xyz_dlhc(link: _MemoryLink, addr: int, reg: int, x: int, y: int, z: int) -> None:
    # DLHC magnetometer order is X, Z, Y, big-endian.
    raw = _be(x) + _be(z) + _be(y)
    for index, value in enumerate(raw):
        link.mem[(addr, reg + index)] = value


def _le(value: int) -> bytes:
    if value < 0:
        value += 65536
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _be(value: int) -> bytes:
    if value < 0:
        value += 65536
    return bytes(((value >> 8) & 0xFF, value & 0xFF))
