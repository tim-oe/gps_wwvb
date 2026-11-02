"""Simulated GPS, compass, and ES100 with the same methods as the hardware."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

from pyubx2 import UBXReader

from gps_wwvb.devices.compass import make_reading
from gps_wwvb.devices.gps_ubx import build_nav_pvt, parse_nav_pvt
from gps_wwvb.devices.types import CompassReading, GpsFix, WwvbReading
from gps_wwvb.geo import WWVB_LAT, WWVB_LON, bearing_deg, magnetic_declination_deg, propagation


def _next_tick(last: float | None, interval_s: float, realtime: bool) -> float:
    """Sleep until ``interval_s`` has passed since ``last``, then return now."""
    now = time.monotonic()
    if realtime and last is not None:
        remaining = interval_s - (now - last)
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
    return now


class SimGps:
    def __init__(
        self,
        lat: float,
        lon: float,
        alt_m: float,
        *,
        interval_s: float = 0.2,
        realtime: bool = True,
    ):
        self.lat = lat
        self.lon = lon
        self.alt_m = alt_m
        self.interval_s = interval_s
        self.realtime = realtime
        self.exhausted = False
        self._last: float | None = None

    def read_raw(self, timeout: float = 2.0) -> bytes:
        del timeout
        self._pace()
        now = datetime.now(UTC)
        return build_nav_pvt(
            lat=self.lat,
            lon=self.lon,
            alt_m=self.alt_m,
            when=now.replace(microsecond=0),
            nano_ns=now.microsecond * 1000,
        )

    def read_fix(self, timeout: float = 2.0) -> GpsFix | None:
        raw = self.read_raw(timeout)
        if not raw:
            return None
        return parse_nav_pvt(UBXReader.parse(raw), time.time())

    def close(self) -> None:
        return None

    def _pace(self) -> None:
        self._last = _next_tick(self._last, self.interval_s, self.realtime)


class SimCompass:
    """Flat board whose +X axis points at WWVB, unless samples are queued."""

    def __init__(
        self,
        lat: float,
        lon: float,
        *,
        alt_m: float = 0.0,
        offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        samples: list[tuple[float, float, float, float, float, float]] | None = None,
        interval_s: float = 0.05,
    ):
        self.lat = lat
        self.lon = lon
        self.alt_m = alt_m
        self.offset = offset
        self.scale = scale
        self.samples = samples
        self.interval_s = interval_s
        self._index = 0
        self.variant = "sim"

    def read_counts(self) -> tuple[float, float, float, float, float, float]:
        if self.samples:
            sample = self.samples[min(self._index, len(self.samples) - 1)]
            self._index += 1
            return sample
        # Magnetic heading is the true bearing minus declination, so +X faces WWVB
        # once the World Magnetic Model correction is applied.
        bearing = bearing_deg(self.lat, self.lon, WWVB_LAT, WWVB_LON)
        declination = magnetic_declination_deg(self.lat, self.lon, self.alt_m, datetime.now(UTC))
        target = math.radians((bearing - declination) % 360.0)
        return (0.0, 0.0, 1000.0, math.cos(target) * 400.0, math.sin(target) * 400.0, 0.0)

    def read(self) -> CompassReading:
        ax, ay, az, mx, my, mz = self.read_counts()
        return make_reading(
            ax,
            ay,
            az,
            mx,
            my,
            mz,
            offset=self.offset,
            scale=self.scale,
            declination_deg=0.0,
        )

    def close(self) -> None:
        return None


class SimWwvb:
    def __init__(
        self,
        lat: float,
        lon: float,
        *,
        interval_s: float = 0.2,
        realtime: bool = True,
        antenna: int = 1,
        bias_s: float = 0.0,
    ):
        self.lat = lat
        self.lon = lon
        self.interval_s = interval_s
        self.realtime = realtime
        self.antenna = antenna
        self.bias_s = bias_s
        self.exhausted = False
        self._last: float | None = None

    def receive(self) -> WwvbReading:
        self._last = _next_tick(self._last, self.interval_s, self.realtime)
        _distance, _bearing, delay = propagation(self.lat, self.lon)
        system_time = datetime.now(UTC)
        # The chip reports the time at the transmitter, before travel delay.
        wwvb_time = system_time - timedelta(seconds=delay) + timedelta(seconds=self.bias_s)
        return WwvbReading(
            wwvb_time=wwvb_time,
            system_time=system_time,
            antenna=self.antenna,
            dst=False,
            leap_second=None,
            tracking=False,
        )

    def close(self) -> None:
        return None
