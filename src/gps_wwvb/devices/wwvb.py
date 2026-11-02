"""ES100 WWVB receiver.

``es100`` is imported only when a real device is opened. The package depends
on RPi.GPIO and smbus, which are not installed on the development machine.
The shutdown call is pinned to es100-wwvb 0.4.4, whose only way to drop the
EN pin is the private ``_disable`` method.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from gps_wwvb.devices.types import WwvbReading

logger = logging.getLogger(__name__)


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
            if not _is_es100_error(exc):
                raise
            return None
        if wwvb_time is None:
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
        return self._dev


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
