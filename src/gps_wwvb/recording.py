"""Record raw device streams as JSONL for later replay."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from gps_wwvb.devices.gps_ubx import extract_ubx_frames
from gps_wwvb.devices.types import WwvbReading

logger = logging.getLogger(__name__)

GPS_FILE = "gps.jsonl"
COMPASS_FILE = "compass.jsonl"
WWVB_FILE = "wwvb.jsonl"


class RecordingError(Exception):
    """A device failed while a recording was in progress."""


def record_devices(gps, compass, wwvb, out: Path, duration: float) -> None:
    """Record until ``duration`` seconds have passed.

    GPS lines are complete UBX frames. Compass lines are aligned counts before
    hard-iron correction. WWVB lines are the decoded readings. A reception
    already in progress is allowed to finish. A device error raises
    ``RecordingError`` instead of ending the recording as if it had succeeded.
    """
    out.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    errors: list[BaseException] = []
    error_lock = threading.Lock()

    def fail(exc: BaseException) -> None:
        with error_lock:
            errors.append(exc)
        stop.set()

    def guarded(name: str, func, *args) -> None:
        try:
            func(*args)
        except Exception as exc:
            logger.error("%s recording failed: %s", name, exc, exc_info=True)
            fail(exc)

    threads = [
        threading.Thread(
            target=guarded,
            args=("gps", _record_gps, gps, out / GPS_FILE, stop),
            name="record-gps",
        ),
        threading.Thread(
            target=guarded,
            args=("compass", _record_compass, compass, out / COMPASS_FILE, stop),
            name="record-compass",
        ),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            if errors:
                break
            try:
                reading = wwvb.receive()
            except Exception as exc:
                logger.error("wwvb recording failed: %s", exc, exc_info=True)
                fail(exc)
                break
            if reading is None:
                if wwvb.exhausted:
                    break
                if stop.wait(0.1):
                    break
                continue
            _append(out / WWVB_FILE, _wwvb_row(reading))
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2.0)
    if errors:
        raise RecordingError(str(errors[0])) from errors[0]


def _record_gps(gps, path: Path, stop: threading.Event) -> None:
    pending = bytearray()
    while not stop.is_set():
        chunk = gps.read_raw(timeout=0.3)
        if not chunk:
            if stop.wait(0.05):
                return
            continue
        pending.extend(chunk)
        frames = extract_ubx_frames(pending)
        for frame in frames:
            _append(path, {"t": time.time(), "hex": frame.hex()})


def _record_compass(compass, path: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        ax, ay, az, mx, my, mz = compass.read_counts()
        _append(
            path,
            {
                "t": time.time(),
                "ax": ax,
                "ay": ay,
                "az": az,
                "mx": mx,
                "my": my,
                "mz": mz,
            },
        )
        if stop.wait(0.05):
            return


def _wwvb_row(reading: WwvbReading) -> dict:
    return {
        "t": time.time(),
        "wwvb_time": reading.wwvb_time.isoformat(),
        "system_time": reading.system_time.isoformat(),
        "antenna": reading.antenna,
        "dst": reading.dst,
        "leap_second": reading.leap_second,
        "tracking": reading.tracking,
    }


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
