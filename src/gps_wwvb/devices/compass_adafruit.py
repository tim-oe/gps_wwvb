"""LSM303AGR through the Adafruit CircuitPython drivers.

The magnetometer die is an LIS2MDL, so the driver is
``adafruit-circuitpython-lis2mdl`` rather than the DLHC magnetometer library.
``magnetic`` is microteslas at 0.15 µT per count. Readings are converted back
to counts so the hard-iron offsets already stored in the config stay valid.
Magnetometer +Y is negated, matching ``Lsm303``: that axis is opposite the
accelerometer, and without the flip a counterclockwise turn increases heading.
"""

from __future__ import annotations

from gps_wwvb.devices.compass import make_reading
from gps_wwvb.devices.types import CompassReading

# Matches adafruit_lis2mdl._MAG_SCALE (1.5 milligauss/LSB).
MAG_LSB_UT = 0.15


def mag_counts(mx_ut: float, my_ut: float, mz_ut: float) -> tuple[float, float, float]:
    """LIS2MDL microteslas to aligned counts. +Y is flipped into the accel frame."""
    return (mx_ut / MAG_LSB_UT, -(my_ut / MAG_LSB_UT), mz_ut / MAG_LSB_UT)


class AdafruitLsm303:
    """Accelerometer plus AGR magnetometer on ``board.I2C()``."""

    variant = "agr"

    def __init__(self, i2c=None):
        import adafruit_lis2mdl
        import adafruit_lsm303_accel
        import board

        self._owns_i2c = i2c is None
        self._i2c = None
        try:
            self._i2c = board.I2C() if i2c is None else i2c
            self._accel = adafruit_lsm303_accel.LSM303_Accel(self._i2c)
            self._accel.data_rate = adafruit_lsm303_accel.Rate.RATE_100_HZ
            self._accel.mode = adafruit_lsm303_accel.Mode.MODE_HIGH_RESOLUTION
            self._accel.range = adafruit_lsm303_accel.Range.RANGE_2G
            self._mag = adafruit_lis2mdl.LIS2MDL(self._i2c)
            self._mag.data_rate = adafruit_lis2mdl.DataRate.Rate_100_HZ
        except Exception:
            self.close()
            raise

    def read_counts(self) -> tuple[float, float, float, float, float, float]:
        """Accelerometer (m/s^2) and aligned magnetometer counts, before hard-iron."""
        ax, ay, az = self._accel.acceleration
        mx, my, mz = mag_counts(*self._mag.magnetic)
        return ax, ay, az, mx, my, mz

    def read(
        self,
        *,
        offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        declination_deg: float = 0.0,
    ) -> CompassReading:
        ax, ay, az, mx, my, mz = self.read_counts()
        return make_reading(
            ax,
            ay,
            az,
            mx,
            my,
            mz,
            offset=offset,
            scale=scale,
            declination_deg=declination_deg,
        )

    def close(self) -> None:
        if self._owns_i2c and self._i2c is not None:
            deinit = getattr(self._i2c, "deinit", None)
            if deinit:
                deinit()
            self._i2c = None
