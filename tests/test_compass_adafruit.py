"""Adafruit LIS2MDL readings stay in the count frame the calibration file uses."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gps_wwvb.devices.compass_adafruit import MAG_LSB_UT, AdafruitLsm303, mag_counts


def test_mag_counts_flip_y_and_undo_the_microtesla_scale():
    mx, my, mz = mag_counts(1.5, 3.0, -0.75)
    assert mx == 1.5 / MAG_LSB_UT
    assert my == -(3.0 / MAG_LSB_UT)
    assert mz == -0.75 / MAG_LSB_UT


def test_driver_applies_saved_count_offsets_to_library_microteslas():
    accel = MagicMock()
    accel.acceleration = (0.0, 0.0, 9.8)
    mag = MagicMock()
    mag.magnetic = (15.0, -6.525, 15.15)
    lis2mdl = SimpleNamespace(
        LIS2MDL=MagicMock(return_value=mag),
        DataRate=SimpleNamespace(Rate_100_HZ=3),
    )
    lsm = SimpleNamespace(
        LSM303_Accel=MagicMock(return_value=accel),
        Rate=SimpleNamespace(RATE_100_HZ=5),
        Mode=SimpleNamespace(MODE_HIGH_RESOLUTION=1),
        Range=SimpleNamespace(RANGE_2G=0),
    )
    modules = {"adafruit_lis2mdl": lis2mdl, "adafruit_lsm303_accel": lsm}
    with patch.dict("sys.modules", modules):
        compass = AdafruitLsm303(MagicMock())
    reading = compass.read(offset=(-266.5, -43.5, 101.0), scale=(1.0, 1.0, 1.0))
    # 15 µT / 0.15 = 100 counts, then the saved offset is subtracted.
    assert reading.mx == 100.0 - (-266.5)
    # Library Y -6.525 µT is -43.5 counts; the axis flip makes it +43.5.
    assert reading.my == 43.5 - (-43.5)
    assert reading.mz == 0.0
