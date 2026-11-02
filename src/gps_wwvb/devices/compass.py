"""LSM303 accelerometer and magnetometer driver.

Detects the AGR variant from magnetometer WHO_AM_I (register 0x4F == 0x40)
and otherwise speaks the DLHC register map. Heading is tilt-compensated in
the accelerometer frame: 0 degrees means +X points magnetic north, and the
angle increases clockwise when +Z is up. Declination (east positive) converts
that to true north: true = magnetic + declination.
"""

from __future__ import annotations

import math
import time
from typing import Literal

from gps_wwvb.devices.types import CompassReading

ACCEL_DEFAULT = 0x19
MAG_DEFAULT = 0x1E

# DLHC / shared accelerometer registers.
_CTRL_REG1_A = 0x20
_CTRL_REG4_A = 0x23
_OUT_X_L_A = 0x28

# DLHC magnetometer.
_CRA_REG_M = 0x00
_CRB_REG_M = 0x01
_MR_REG_M = 0x02
_OUT_X_H_M = 0x03

# AGR magnetometer.
_WHO_AM_I_M = 0x4F
_WHO_AM_I_AGR = 0x40
_CFG_REG_A_M = 0x60
_CFG_REG_C_M = 0x62
_OUTX_L_REG_M = 0x68


class CompassError(Exception):
    """The compass could not be read or calibrated."""


def heading_deg(ax: float, ay: float, az: float, mx: float, my: float, mz: float) -> float:
    """Tilt-compensated heading of the +X axis, degrees clockwise from magnetic north.

    This is the cross-product method used by the Pololu LSM303 library:
    east = mag cross accel, north = accel cross east, heading = atan2(east dot +X, north dot +X).
    Accelerometer axes are the at-rest reading (+Z up on a board sitting flat).
    """
    ax, ay, az = _unit(ax, ay, az)
    mx, my, mz = _unit(mx, my, mz)
    ex = my * az - mz * ay
    ey = mz * ax - mx * az
    ez = mx * ay - my * ax
    ex, ey, ez = _unit(ex, ey, ez)
    nx = ay * ez - az * ey
    heading = math.degrees(math.atan2(ex, nx))
    if heading < 0.0:
        heading += 360.0
    return heading


def hard_iron_fit(
    samples: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Offsets and diagonal scales from a min/max sweep of magnetometer counts."""
    if len(samples) < 8:
        raise CompassError("need at least 8 compass samples; rotate the board for longer")
    offsets: list[float] = []
    spans: list[float] = []
    for axis in range(3):
        values = [sample[axis] for sample in samples]
        lo = min(values)
        hi = max(values)
        offsets.append((lo + hi) / 2.0)
        spans.append(max((hi - lo) / 2.0, 1.0))
    average = sum(spans) / 3.0
    scales = tuple(average / span for span in spans)
    return (offsets[0], offsets[1], offsets[2]), scales  # type: ignore[return-value]


def apply_calibration(
    mx: float,
    my: float,
    mz: float,
    offset: tuple[float, float, float],
    scale: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        (mx - offset[0]) * scale[0],
        (my - offset[1]) * scale[1],
        (mz - offset[2]) * scale[2],
    )


def make_reading(
    ax: float,
    ay: float,
    az: float,
    mx: float,
    my: float,
    mz: float,
    *,
    offset: tuple[float, float, float],
    scale: tuple[float, float, float],
    declination_deg: float,
    received_at: float | None = None,
) -> CompassReading:
    """``mx, my, mz`` are aligned counts before hard-iron correction."""
    cx, cy, cz = apply_calibration(mx, my, mz, offset, scale)
    try:
        magnetic = heading_deg(ax, ay, az, cx, cy, cz)
        true_heading = (magnetic + declination_deg) % 360.0
    except ValueError:
        magnetic = None
        true_heading = None
    return CompassReading(
        heading_mag=magnetic,
        heading_true=true_heading,
        ax=ax,
        ay=ay,
        az=az,
        mx=cx,
        my=cy,
        mz=cz,
        received_at=time.time() if received_at is None else received_at,
    )


class Lsm303:
    def __init__(self, link, accel_address: int = ACCEL_DEFAULT, mag_address: int = MAG_DEFAULT):
        self.link = link
        self.accel_address = accel_address
        self.mag_address = mag_address
        self.variant = self._detect()
        self._configure()

    def read_counts(self) -> tuple[float, float, float, float, float, float]:
        """Accelerometer and aligned magnetometer counts, before hard-iron correction."""
        ax, ay, az = self._read_accel()
        mx, my, mz = self._read_mag()
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
        close = getattr(self.link, "close", None)
        if close:
            close()

    def _detect(self) -> str:
        try:
            who = self.link.read_at(self.mag_address, _WHO_AM_I_M, 1)
        except OSError:
            return "dlhc"
        if who and who[0] == _WHO_AM_I_AGR:
            return "agr"
        return "dlhc"

    def _configure(self) -> None:
        # 100 Hz, all axes, high-resolution ±2 g.
        self.link.write_at(self.accel_address, _CTRL_REG1_A, bytes([0x57]))
        self.link.write_at(self.accel_address, _CTRL_REG4_A, bytes([0x08]))
        if self.variant == "agr":
            # Temperature compensation, 100 Hz, continuous conversion, block update.
            self.link.write_at(self.mag_address, _CFG_REG_A_M, bytes([0x8C]))
            self.link.write_at(self.mag_address, _CFG_REG_C_M, bytes([0x10]))
        else:
            # 30 Hz, ±1.3 gauss, continuous conversion.
            self.link.write_at(self.mag_address, _CRA_REG_M, bytes([0x14]))
            self.link.write_at(self.mag_address, _CRB_REG_M, bytes([0x20]))
            self.link.write_at(self.mag_address, _MR_REG_M, bytes([0x00]))

    def _read_accel(self) -> tuple[int, int, int]:
        # The high bit turns on auto-increment for the accelerometer.
        raw = self.link.read_at(self.accel_address, _OUT_X_L_A | 0x80, 6)
        return _s16_le(raw, 0), _s16_le(raw, 2), _s16_le(raw, 4)

    def _read_mag(self) -> tuple[int, int, int]:
        if self.variant == "agr":
            # Magnetometer +Y is opposite accelerometer +Y. Without this flip the
            # heading is mirrored: a counterclockwise turn increases it.
            raw = self.link.read_at(self.mag_address, _OUTX_L_REG_M, 6)
            return _s16_le(raw, 0), -_s16_le(raw, 2), _s16_le(raw, 4)
        # DLHC magnetometer registers are ordered X, Z, Y and are big-endian.
        raw = self.link.read_at(self.mag_address, _OUT_X_H_M, 6)
        mx = _s16_be(raw, 0)
        mz = _s16_be(raw, 2)
        my = _s16_be(raw, 4)
        return mx, my, mz


def _unit(x: float, y: float, z: float) -> tuple[float, float, float]:
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        raise ValueError("zero vector")
    return x / norm, y / norm, z / norm


def _s16(raw: bytes, offset: int, byteorder: Literal["little", "big"]) -> int:
    chunk = raw[offset : offset + 2]
    if len(chunk) != 2:
        raise CompassError(f"short compass read ({len(raw)} bytes)")
    return int.from_bytes(chunk, byteorder, signed=True)


def _s16_le(raw: bytes, offset: int) -> int:
    return _s16(raw, offset, "little")


def _s16_be(raw: bytes, offset: int) -> int:
    return _s16(raw, offset, "big")
