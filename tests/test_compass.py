"""Tilt-compensated heading and hard-iron calibration."""

import math
from pathlib import Path

import pytest

from gps_wwvb.config import load_config, set_compass_calibrated, update_compass_calibration
from gps_wwvb.devices.compass import hard_iron_fit, heading_deg, make_reading

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
