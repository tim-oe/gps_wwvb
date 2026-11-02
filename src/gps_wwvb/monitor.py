"""Compare each WWVB reception with GPS time, corrected for travel delay."""

from __future__ import annotations

import csv
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gps_wwvb.devices.types import GpsFix, WwvbReading
from gps_wwvb.geo import (
    antenna_is_aimed,
    antenna_turn_deg,
    format_turn,
    is_night,
    propagation,
    seconds_until_on_air,
    true_heading_deg,
    turn_direction,
    wwvb_maintenance_gap,
)

logger = logging.getLogger(__name__)

_BREAK = object()
_CONTINUE = object()
_OPEN = object()

# A NAV-PVT older than this no longer contributes a clock offset. The GPS loop
# samples about twice a second, so a reception can use the sample it already has.
GPS_FIX_MAX_AGE_S = 5.0
_GPS_POLL_S = 0.5
# Calibration accepts 5 deg. 15 deg still splits the signal across the V and
# stays outside ordinary compass noise.
ANTENNA_AIM_TOLERANCE_DEG = 15.0

LOG_COLUMNS = (
    "event",
    "system_time",
    "wwvb_time",
    "corrected_wwvb",
    "gps_time",
    "offset_ms",
    "antenna",
    "dst",
    "leap_second",
    "tracking",
    "lat",
    "lon",
    "alt_m",
    "fix_type",
    "num_sv",
    "time_accuracy_ns",
    "fix_age_s",
    "distance_km",
    "bearing_deg",
    "propagation_ms",
    "heading_mag",
    "heading_true",
    "declination_deg",
    "antenna_turn_deg",
    "day_night",
)


@dataclass
class Observation:
    event: str
    system_time: datetime
    wwvb_time: datetime | None = None
    corrected_wwvb: datetime | None = None
    gps_time: datetime | None = None
    offset_ms: float | None = None
    antenna: int | None = None
    dst: bool | None = None
    leap_second: str | None = None
    tracking: bool = False
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    fix_type: int | None = None
    num_sv: int | None = None
    time_accuracy_ns: int | None = None
    fix_age_s: float | None = None
    distance_km: float | None = None
    bearing_deg: float | None = None
    propagation_ms: float | None = None
    heading_mag: float | None = None
    heading_true: float | None = None
    declination_deg: float | None = None
    antenna_turn_deg: float | None = None
    day_night: str | None = None

    def to_row(self) -> dict:
        row = {}
        for column in LOG_COLUMNS:
            value = getattr(self, column)
            if isinstance(value, datetime):
                value = value.isoformat()
            row[column] = value
        return row


def compute_offset_ms(
    wwvb_time: datetime,
    irq_system_time: datetime,
    travel_s: float,
    gps_minus_system_s: float,
) -> float:
    """WWVB-minus-GPS in milliseconds, after adding the radio travel time.

    ``gps_minus_system_s`` maps a system-clock timestamp onto GPS time:
    GPS time = system time + offset.
    """
    corrected = wwvb_time + timedelta(seconds=travel_s)
    gps_at_irq = irq_system_time + timedelta(seconds=gps_minus_system_s)
    return (corrected - gps_at_irq).total_seconds() * 1000.0


class _ThrottledLog:
    """Log a repeated hardware error immediately, then at most every 30 s."""

    def __init__(self, interval_s: float = 30.0):
        self.interval_s = interval_s
        self._last: float | None = None
        self._hidden = 0

    def log(self, source: str, exc: Exception) -> None:
        hidden = self._claim()
        if hidden is None:
            return
        if hidden:
            logger.error(
                "%s: %s (%d similar errors suppressed)",
                source,
                exc,
                hidden,
                exc_info=True,
            )
        else:
            logger.error("%s: %s", source, exc, exc_info=True)

    def warn(self, message: str) -> None:
        hidden = self._claim()
        if hidden is None:
            return
        if hidden:
            logger.warning("%s (%d similar warnings suppressed)", message, hidden)
        else:
            logger.warning("%s", message)

    def _claim(self) -> int | None:
        now = time.monotonic()
        if self._last is not None and now - self._last < self.interval_s:
            self._hidden += 1
            return None
        hidden = self._hidden
        self._hidden = 0
        self._last = now
        return hidden


class Monitor:
    def __init__(
        self,
        gps,
        compass,
        wwvb,
        *,
        log_path: Path | None,
        log_format: str,
        echo: bool = True,
        clock: Callable[[], datetime] | None = None,
    ):
        self.gps = gps
        self.compass = compass
        self.wwvb = wwvb
        self.echo = echo
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._fix: GpsFix | None = None
        self._offset_s: float | None = None
        self._noted_at: float | None = None
        self._stop = threading.Event()
        self._log = SampleLog(log_path, log_format)
        self._gps_errors = _ThrottledLog()
        self._gps_misses = _ThrottledLog()
        self._wwvb_errors = _ThrottledLog()
        self._gps_lock = threading.Lock()
        self._gps_poll_s = _GPS_POLL_S

    def note_fix(self, fix: GpsFix) -> None:
        """Remember a fix that has a position or a resolved time.

        An empty NAV-PVT (the receiver's 0,0 placeholder) is ignored so it
        cannot replace the last good sample. A sample with no resolved time
        clears the clock offset.
        """
        if not fix.position_ok and not fix.time_ok:
            return
        with self._lock:
            self._fix = fix
            self._noted_at = time.monotonic()
            if fix.time_ok and fix.utc is not None:
                if fix.clock_offset_s is not None:
                    self._offset_s = fix.clock_offset_s
                else:
                    self._offset_s = fix.utc.timestamp() - fix.received_at
            else:
                self._offset_s = None

    def snapshot(self) -> tuple[GpsFix | None, float | None, float | None]:
        """Return ``(fix, gps-minus-system seconds, age)``.

        The offset is None when the newest useful fix is older than
        ``GPS_FIX_MAX_AGE_S``. Age is seconds since that fix was noted.
        """
        with self._lock:
            fix = self._fix
            offset = self._offset_s
            noted = self._noted_at
        if noted is None:
            return fix, None, None
        age = time.monotonic() - noted
        if age > GPS_FIX_MAX_AGE_S:
            return fix, None, age
        return fix, offset, age

    def radio_now(self) -> datetime:
        _fix, offset, _age = self.snapshot()
        now = self.clock()
        if offset is None:
            return now
        return now + timedelta(seconds=offset)

    def handle_reading(self, reading: WwvbReading) -> Observation:
        self._align_replay_fix(reading)
        fix, offset_s, fix_age_s = self.snapshot()
        heading = self._compass_for(reading)
        distance = bearing = travel = None
        lat = lon = alt = None
        fix_type = num_sv = tacc = None
        if fix is not None:
            fix_type, num_sv, tacc = fix.fix_type, fix.num_sv, fix.time_accuracy_ns
            if fix.position_ok and fix.lat is not None and fix.lon is not None:
                lat, lon, alt = fix.lat, fix.lon, fix.alt_m
                distance, bearing, travel = propagation(fix.lat, fix.lon)
        corrected = None
        gps_time = None
        offset_ms = None
        if not reading.tracking and offset_s is not None:
            gps_time = reading.system_time + timedelta(seconds=offset_s)
            if travel is not None:
                corrected = reading.wwvb_time + timedelta(seconds=travel)
                offset_ms = compute_offset_ms(
                    reading.wwvb_time,
                    reading.system_time,
                    travel,
                    offset_s,
                )
        heading_mag = None if heading is None else heading.heading_mag
        when = self.clock() if fix is None or fix.utc is None else fix.utc
        heading_true, declination = true_heading_deg(heading_mag, lat, lon, alt, when)
        turn = None
        if heading_true is not None and bearing is not None:
            turn = antenna_turn_deg(heading_true, bearing)
        day_night = None
        if lat is not None and lon is not None:
            day_night = "night" if is_night(lat, lon, self.radio_now()) else "day"
        observation = Observation(
            event="reception",
            system_time=reading.system_time,
            wwvb_time=reading.wwvb_time,
            corrected_wwvb=corrected,
            gps_time=gps_time,
            offset_ms=offset_ms,
            antenna=reading.antenna,
            dst=reading.dst,
            leap_second=reading.leap_second,
            tracking=reading.tracking,
            lat=lat,
            lon=lon,
            alt_m=alt,
            fix_type=fix_type,
            num_sv=num_sv,
            time_accuracy_ns=tacc,
            fix_age_s=fix_age_s,
            distance_km=distance,
            bearing_deg=bearing,
            propagation_ms=None if travel is None else travel * 1000.0,
            heading_mag=heading_mag,
            heading_true=heading_true,
            declination_deg=declination,
            antenna_turn_deg=turn,
            day_night=day_night,
        )
        self._emit(observation)
        return observation

    def run(
        self,
        stop: threading.Event,
        *,
        max_readings: int | None = None,
        max_loops: int | None = None,
    ) -> None:
        """Block on WWVB receptions until ``stop`` is set or the replay is finished."""
        self._stop = stop
        replay = callable(getattr(self.gps, "fix_at", None))
        readings = 0
        loops = 0
        announced_gap = False
        gps_thread = None
        try:
            if not replay and not self._startup():
                return
            if not replay:
                gps_thread = threading.Thread(
                    target=self._keep_gps, name="gps-wwvb-gps", daemon=True
                )
                gps_thread.start()
            while not stop.is_set():
                loops += 1
                if max_loops is not None and loops > max_loops:
                    break
                step, announced_gap = self._hold_for_maintenance(
                    stop, loops, max_loops, announced_gap
                )
                if step is _OPEN:
                    step = self._next_reading(stop)
                if step is _BREAK:
                    break
                if step is _CONTINUE:
                    continue
                if not replay:
                    self._sample_gps_if_stale()
                self.handle_reading(step)
                readings += 1
                if max_readings is not None and readings >= max_readings:
                    break
        finally:
            stop.set()
            if gps_thread is not None:
                gps_thread.join(timeout=3.0)
            self._log.close()

    def close(self) -> None:
        self._log.close()

    def _hold_for_maintenance(self, stop, loops: int, max_loops: int | None, announced: bool):
        """Log a maintenance gap once, then wait it out without another row."""
        now = self.radio_now()
        if not wwvb_maintenance_gap(now):
            return _OPEN, False
        if not announced:
            self._emit(Observation(event="maintenance_gap", system_time=now))
        if max_loops is not None and loops >= max_loops:
            return _BREAK, True
        if stop.wait(min(1.0, seconds_until_on_air(now))):
            return _BREAK, True
        return _CONTINUE, True

    def _next_reading(self, stop):
        try:
            reading = self.wwvb.receive()
        except Exception as exc:
            self._wwvb_errors.log("wwvb", exc)
            return _BREAK if stop.wait(1.0) else _CONTINUE
        if reading is not None:
            return reading
        if self.wwvb.exhausted or stop.wait(0.5):
            return _BREAK
        return _CONTINUE

    def _align_replay_fix(self, reading: WwvbReading) -> None:
        fix_at = getattr(self.gps, "fix_at", None)
        if not callable(fix_at):
            return
        try:
            fix = fix_at(reading.system_time)
        except Exception as exc:
            self._gps_errors.log("replay gps", exc)
            return
        if fix is not None:
            self.note_fix(fix)

    def _compass_for(self, reading: WwvbReading):
        read_at = getattr(self.compass, "read_at", None)
        if callable(read_at):
            try:
                return read_at(reading.system_time)
            except Exception:
                return None
        return _safe_compass(self.compass)

    def _startup(self) -> bool:
        """Wait for a GPS fix, then read the compass. False when stopped first."""
        fix = self._wait_for_gps()
        if fix is None:
            return False
        utc = "n/a" if fix.utc is None else fix.utc.isoformat()
        acc = "n/a" if fix.time_accuracy_ns is None else f"{fix.time_accuracy_ns} ns"
        logger.info(
            "GPS: lat %s lon %s alt %s m time %s sv %s acc %s",
            format_number(fix.lat, ".6f"),
            format_number(fix.lon, ".6f"),
            format_number(fix.alt_m, ".1f"),
            utc,
            fix.num_sv,
            acc,
        )
        heading = _safe_compass(self.compass)
        if heading is None:
            logger.info("Compass: no sample")
            return True
        when = self.clock() if fix is None or fix.utc is None else fix.utc
        lat = None if fix is None else fix.lat
        lon = None if fix is None else fix.lon
        alt = None if fix is None else fix.alt_m
        heading_true, declination = true_heading_deg(heading.heading_mag, lat, lon, alt, when)
        bearing = None
        if lat is not None and lon is not None:
            _distance, bearing, _travel = propagation(lat, lon)
        turn = None
        if heading_true is not None and bearing is not None:
            turn = antenna_turn_deg(heading_true, bearing)
        logger.info(
            "Compass: mag %s  true %s  dec %s  brg %s  turn %s",
            format_number(heading.heading_mag, ".1f"),
            format_number(heading_true, ".1f"),
            format_number(declination, "+.1f"),
            format_number(bearing, ".1f"),
            format_turn(turn),
        )
        if (
            turn is not None
            and heading_true is not None
            and bearing is not None
            and not antenna_is_aimed(heading_true, bearing, ANTENNA_AIM_TOLERANCE_DEG)
        ):
            way = turn_direction(turn)
            logger.warning(
                "antenna is %.1f deg off WWVB; turn %s (tolerance ±%.0f)",
                abs(turn),
                way,
                ANTENNA_AIM_TOLERANCE_DEG,
            )
        return True

    def _wait_for_gps(self) -> GpsFix | None:
        """Keep sampling until the fix has a position and a resolved time."""
        told: float | None = None
        while not self._stop.is_set():
            fix = self._sample_gps()
            if fix is not None and fix.position_ok and fix.time_ok:
                return fix
            now = time.monotonic()
            if told is None or now - told >= 30.0:
                logger.info("waiting for GPS fix")
                told = now
            if self._stop.wait(1.0):
                return None
        return None

    def _keep_gps(self) -> None:
        """Keep the latest GPS fix and PPS offset fresh while WWVB receive blocks."""
        while not self._stop.is_set():
            if self._stop.wait(self._gps_poll_s):
                return
            self._sample_gps()

    def _sample_gps_if_stale(self) -> None:
        _fix, _offset, age = self.snapshot()
        if age is not None and age <= GPS_FIX_MAX_AGE_S:
            return
        self._sample_gps()

    def _sample_gps(self) -> GpsFix | None:
        """Take one GPS fix and remember it. None when the read fails."""
        with self._gps_lock:
            try:
                fix = self.gps.read_fix(timeout=2.0)
            except Exception as exc:
                self._gps_errors.log("gps", exc)
                return None
            if fix is None:
                self._gps_misses.warn("GPS read timed out")
                return None
            if not fix.position_ok and not fix.time_ok:
                self._gps_misses.warn("GPS report has no position or time")
            self.note_fix(fix)
            return fix

    def _emit(self, observation: Observation) -> None:
        if self.echo:
            logger.info("%s", format_observation(observation))
        self._log.write(observation.to_row())


def format_observation(observation: Observation) -> str:
    stamp = observation.system_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    if observation.event == "maintenance_gap":
        minute = 16 if observation.system_time.minute < 16 else 46
        return f"{stamp}Z WWVB maintenance gap (off air until minute {minute:02d})"
    offset = "n/a" if observation.offset_ms is None else f"{observation.offset_ms:+.1f} ms"
    antenna = "n/a" if observation.antenna is None else str(observation.antenna)
    distance = format_number(observation.distance_km, ".1f")
    bearing = format_number(observation.bearing_deg, ".1f")
    heading = format_number(observation.heading_true, ".1f")
    declination = format_number(observation.declination_deg, "+.1f")
    turn = format_turn(observation.antenna_turn_deg)
    day = observation.day_night or "n/a"
    tracking = " tracking" if observation.tracking else ""
    age = "n/a" if observation.fix_age_s is None else f"{observation.fix_age_s:.1f}s"
    return (
        f"{stamp}Z offset {offset}  ant {antenna}  {distance} km  "
        f"brg {bearing}  hdg {heading}  dec {declination}  turn {turn}  {day}{tracking}"
        f"  fix {age}"
    )


class SampleLog:
    def __init__(self, path: Path | None, fmt: str):
        self.path = path
        self.fmt = fmt
        self._handle = None
        self._writer = None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        self._handle = path.open("a", encoding="utf-8", newline="")
        if fmt == "csv":
            self._writer = csv.DictWriter(self._handle, fieldnames=LOG_COLUMNS)
            if new_file:
                self._writer.writeheader()
                self._handle.flush()

    def write(self, row: dict) -> None:
        if self._handle is None:
            return
        if self._writer is not None:
            self._writer.writerow({key: _csv(row.get(key)) for key in LOG_COLUMNS})
        else:
            self._handle.write(json.dumps(row) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def format_number(value: float | None, spec: str) -> str:
    if value is None:
        return "n/a"
    return format(value, spec)


def _safe_compass(compass):
    try:
        return compass.read()
    except Exception:
        return None


def _csv(value):
    if value is None:
        return ""
    return value
