"""ES100 WWVB receiver.

``es100`` is imported only when a real device is opened. The package depends
on RPi.GPIO and smbus, which are not installed on the development machine.
The shutdown call is pinned to es100-wwvb 0.4.4, whose only way to drop the
EN pin is the private ``_disable`` method.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from gps_wwvb.devices.types import WwvbReading

logger = logging.getLogger(__name__)

# An ES100 1-minute frame decode takes 133.9035 s (2678 * 50 ms + 3.5 ms) from
# the write to CONTROL0. Starting on the 46.5 ms phase of each 50 ms tick keeps
# consecutive receptions aligned to the top of the UTC second instead of
# drifting across a 50 ms sawtooth.
_FRAME_TICK_S = 0.050
_START_PHASE_S = 0.0465
_FRAME_TIMEOUT_S = 144.0
_TRACKING_TIMEOUT_S = 34.5
_CONTINUE_STALE_S = 5.0


class WwvbError(Exception):
    """The ES100 could not be opened or read."""


class Es100Wwvb:
    def __init__(self, irq: int, en: int, bus: int, address: int):
        self.irq = irq
        self.en = en
        self.bus = bus
        self.address = address
        self._dev = None
        self.exhausted = False

    def receive(self) -> WwvbReading | None:
        device = self._device()
        try:
            wwvb_time = device.time()
        except OSError as exc:
            raise WwvbError(f"ES100 read failed: {exc}") from exc
        except Exception as exc:
            # ES100Error is raised by the library. Importing it here would pull
            # in RPi.GPIO on the development machine, so match it by name.
            if not _is_es100_error(exc) and not isinstance(exc, ValueError):
                raise
            _log_no_reception(device, str(exc))
            return None
        if wwvb_time is None:
            _log_no_reception(device)
            return None
        system_time = _aware(device.system_time())
        antenna = _call(device, "rx_antenna")
        dst = _call(device, "is_presently_dst")
        leap = _call(device, "leap_second")
        # es100-wwvb returns datetime(year=1) for a tracking reception: only the
        # second is valid, and the year/month/day registers are documented as
        # unused. A decoded frame is year + 2000.
        tracking = wwvb_time.year < 2000
        return WwvbReading(
            wwvb_time=_aware(wwvb_time),
            system_time=system_time,
            antenna=_antenna(antenna),
            dst=dst if isinstance(dst, bool) else None,
            leap_second=leap if isinstance(leap, str) else None,
            tracking=tracking,
        )

    def close(self) -> None:
        device = self._dev
        self._dev = None
        if device is None:
            return
        # 0.4.4 has no public shutdown. ``disable`` / ``close`` win if a later
        # release adds one; ``_disable`` drives the EN pin low.
        release = _shutdown(device)
        if not callable(release):
            return
        try:
            release()  # pylint: disable=not-callable
        except Exception:
            logger.warning("failed to disable ES100", exc_info=True)

    def _device(self):
        if self._dev is None:
            try:
                from es100 import ES100
            except ImportError as exc:
                raise WwvbError(
                    "es100 is not installed. On the Pi, install es100-wwvb==0.4.4 "
                    "with --no-deps inside the system-site venv."
                ) from exc
            try:
                self._dev = ES100(irq=self.irq, en=self.en, bus=self.bus, address=self.address)
            except Exception as exc:
                raise WwvbError(f"failed to open ES100: {exc}") from exc
            _configure_device(self._dev)
        return self._dev


def _configure_device(device) -> None:
    """Make es100-wwvb 0.4.4 run one cycle per ``time()`` call with microsecond IRQ timestamps."""
    # pylint: disable=protected-access
    gpio = getattr(device, "_gpio", None)
    irq_wait = getattr(gpio, "irq_wait", None)
    if callable(irq_wait):

        def _wait_for_interrupt(timeout: float | None = None) -> bool:
            device._system_time_received = None
            irq_happened = bool(irq_wait(timeout))
            device._system_time_received = datetime.now(UTC)
            return irq_happened

        device._wait_for_interrupt = _wait_for_interrupt

    required = (
        "_start_rx",
        "_start_tracking",
        "_report_status0_and_irq_reg",
        "_wait_for_interrupt",
    )
    if all(callable(getattr(device, name, None)) for name in required):
        continuing_at: float | None = None

        def _es100_receive(tracking: bool = False) -> None:
            nonlocal continuing_at
            _clear_cycle_flags(device)
            if tracking or continuing_at is None:
                if not tracking:
                    _align_start()
                    device._start_rx()
                else:
                    device._start_tracking()
                device._report_status0_and_irq_reg()
                if getattr(device, "_rx_complete", False):
                    continuing_at = None
                    return
            elif time.monotonic() - continuing_at >= _CONTINUE_STALE_S:
                _drain_stale_cycle(device)
                if getattr(device, "_rx_complete", False):
                    continuing_at = None
                    return
            timeout = _TRACKING_TIMEOUT_S if tracking else _FRAME_TIMEOUT_S
            device._wait_for_interrupt(timeout)
            device._report_status0_and_irq_reg()
            if getattr(device, "_rx_complete", False):
                continuing_at = None
                return
            device._status_ok = False
            if getattr(device, "_cycle_complete", False) and not tracking:
                continuing_at = time.monotonic()
            else:
                continuing_at = None

        device._es100_receive = _es100_receive


def _clear_cycle_flags(device) -> None:
    # pylint: disable=protected-access
    device._rx_complete = False
    device._cycle_complete = False
    device._status_ok = False
    device._tracking_operation = False
    device._rx_antenna = None


def _drain_stale_cycle(device) -> None:
    """Acknowledge a cycle that finished while the monitor waited out a maintenance gap."""
    # pylint: disable=protected-access
    device._report_status0_and_irq_reg()
    if getattr(device, "_rx_complete", False):
        return
    if getattr(device, "_cycle_complete", False):
        device._antenna = 2 if getattr(device, "_antenna", 1) == 1 else 1
    _clear_cycle_flags(device)


def _align_start() -> None:
    """Sleep at most 50 ms so ``CONTROL0 = START`` lands on a consistent 50 ms phase."""
    delay = (_START_PHASE_S - (time.time() % _FRAME_TICK_S)) % _FRAME_TICK_S
    if delay > 0.0:
        time.sleep(delay)


def _log_no_reception(device, detail: str | None = None) -> None:
    ant = _antenna(getattr(device, "_rx_antenna", None) or getattr(device, "_antenna", None))
    if detail:
        reason = detail
    elif getattr(device, "_cycle_complete", False):
        reason = "cycle complete"
    else:
        reason = "no decode"
    if ant is not None:
        logger.warning("WWVB: no reception on ant %d (%s)", ant, reason)
    else:
        logger.warning("WWVB: no reception (%s)", reason)


def _shutdown(device) -> Callable[[], None] | None:
    """Public ``disable`` or ``close`` if the library grows one, else ``_disable``."""
    for name in ("disable", "close", "_disable"):
        method = getattr(device, name, None)
        if callable(method):
            return method
    return None


def _is_es100_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "ES100Error"


def _antenna(value) -> int | None:
    """es100-wwvb 0.4.4 returns ``Antenna1`` or ``Antenna2``, not ``1`` or ``2``."""
    if isinstance(value, int) and value in (1, 2):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "antenna1"):
            return 1
        if text in ("2", "antenna2"):
            return 2
    return None


def _call(device, name: str):
    method = getattr(device, name, None)
    if method is None:
        return None
    try:
        return method()
    except Exception as exc:
        if _is_es100_error(exc):
            return None
        raise


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
