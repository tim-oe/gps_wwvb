"""Readings shared by the hardware, simulator, and replay backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Protocol


@dataclass(frozen=True)
class GpsFix:
    lat: float | None
    lon: float | None
    alt_m: float | None
    utc: datetime | None
    fix_type: int
    valid_date: bool
    valid_time: bool
    fully_resolved: bool
    gnss_fix_ok: bool
    time_accuracy_ns: int | None
    num_sv: int | None
    received_at: float

    @property
    def position_ok(self) -> bool:
        return bool(
            self.gnss_fix_ok
            and self.fix_type in (2, 3, 4)
            and self.lat is not None
            and self.lon is not None
        )

    @property
    def time_ok(self) -> bool:
        return bool(
            self.utc is not None
            and self.valid_date
            and self.valid_time
            and self.fully_resolved
            and self.utc.year >= 2000
        )


@dataclass(frozen=True)
class CompassReading:
    heading_mag: float | None
    heading_true: float | None
    ax: float
    ay: float
    az: float
    mx: float
    my: float
    mz: float
    received_at: float


@dataclass(frozen=True)
class WwvbReading:
    wwvb_time: datetime
    system_time: datetime
    antenna: int | None
    dst: bool | None
    leap_second: str | None
    tracking: bool


class GpsSource(Protocol):
    """A u-blox receiver, the simulator, or a GPS recording."""

    exhausted: bool

    def read_fix(self, timeout: float = 2.0) -> GpsFix | None: ...

    def read_raw(self, timeout: float = 2.0) -> bytes: ...

    def close(self) -> None: ...


class CompassSource(Protocol):
    """A compass that yields a heading and the raw counts behind it."""

    variant: str

    def read(self) -> CompassReading | None: ...

    def read_counts(self) -> tuple[float, float, float, float, float, float]: ...

    def close(self) -> None: ...


class WwvbSource(Protocol):
    """An ES100 receiver, the simulator, or a WWVB recording."""

    exhausted: bool

    def receive(self) -> WwvbReading | None: ...

    def close(self) -> None: ...


class Devices(NamedTuple):
    gps: GpsSource
    compass: CompassSource
    wwvb: WwvbSource
