"""Tilt-compensated heading and hard-iron calibration for the LSM303 compass.

Heading is tilt-compensated in the accelerometer frame: 0 degrees means +X
points magnetic north, and the angle increases clockwise when +Z is up.
Declination (east positive) converts that to true north:
true = magnetic + declination.
"""

from __future__ import annotations

import math
import time

from gps_wwvb.devices.types import CompassReading


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


def _unit(x: float, y: float, z: float) -> tuple[float, float, float]:
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        raise ValueError("zero vector")
    return x / norm, y / norm, z / norm
