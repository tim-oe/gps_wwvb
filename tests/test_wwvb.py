"""ES100 wrapper, exercised with a stand-in for the Pi-only es100 package."""

# pylint: disable=redefined-outer-name

import logging
import sys
import types
from datetime import UTC, datetime

import pytest

from gps_wwvb.devices.wwvb import Es100Wwvb, WwvbError, _antenna


@pytest.fixture
def es100_module():
    previous = sys.modules.get("es100")
    module = types.ModuleType("es100")

    class ES100Error(Exception):
        """Stand-in for es100.ES100Error."""

    module.ES100Error = ES100Error
    sys.modules["es100"] = module
    yield module
    if previous is None:
        sys.modules.pop("es100", None)
    else:
        sys.modules["es100"] = previous


def _install(module, cls) -> None:
    module.ES100 = cls


def test_tracking_reception_is_the_year_one_result(es100_module):
    created = []

    class ES100:
        def __init__(self, irq, en, bus, address):
            created.append(self)
            self.args = (irq, en, bus, address)
            self.used = ""

        def time(self):
            return datetime(1, 1, 1, 0, 0, 30)

        def system_time(self):
            return datetime(2026, 11, 1, 12, 0, 30)

        def rx_antenna(self):
            return 2

        def is_presently_dst(self):
            return False

        def leap_second(self):
            return None

        def disable(self):
            self.used = "disable"

        def _disable(self):
            self.used = "_disable"

    _install(es100_module, ES100)
    receiver = Es100Wwvb(11, 7, 1, 0x32)
    reading = receiver.receive()
    assert reading is not None
    assert reading.tracking is True
    assert reading.system_time.tzinfo is not None
    assert reading.antenna == 2
    receiver.close()
    assert created[0].used == "disable"


def test_private_disable_is_the_fallback(es100_module):
    created = []

    class ES100:
        def __init__(self, irq, en, bus, address):
            del irq, en, bus, address
            created.append(self)
            self.used = ""

        def time(self):
            return datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

        def system_time(self):
            return datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

        def rx_antenna(self):
            return "Antenna2"

        def is_presently_dst(self):
            return "nope"

        def leap_second(self):
            return 1

        def _disable(self):
            self.used = "_disable"

    _install(es100_module, ES100)
    receiver = Es100Wwvb(11, 7, 1, 0x32)
    reading = receiver.receive()
    assert reading is not None
    assert reading.tracking is False
    assert reading.antenna == 2
    assert reading.dst is None
    assert reading.leap_second is None
    receiver.close()
    assert created[0].used == "_disable"


def test_antenna_names_from_es100_become_numbers():
    assert _antenna("Antenna1") == 1
    assert _antenna("Antenna2") == 2
    assert _antenna(1) == 1
    assert _antenna("nope") is None
    assert _antenna(None) is None


def test_library_errors_are_no_reception_and_bus_errors_propagate(es100_module):
    class ES100:
        def __init__(self, **_kwargs):
            return None

        def time(self):
            raise es100_module.ES100Error("no signal")

    _install(es100_module, ES100)
    assert Es100Wwvb(11, 7, 1, 0x32).receive() is None

    class Broken:
        def __init__(self, **_kwargs):
            return None

        def time(self):
            raise OSError("bus")

    _install(es100_module, Broken)
    with pytest.raises(WwvbError, match="ES100 read failed"):
        Es100Wwvb(11, 7, 1, 0x32).receive()

    class Unexpected:
        def __init__(self, **_kwargs):
            return None

        def time(self):
            raise RuntimeError("bug")

    _install(es100_module, Unexpected)
    with pytest.raises(RuntimeError, match="bug"):
        Es100Wwvb(11, 7, 1, 0x32).receive()


def test_single_cycle_receive_stops_on_cycle_complete_and_preserves_microseconds(
    es100_module, monkeypatch, caplog
):
    slept: list[float] = []
    monkeypatch.setattr("gps_wwvb.devices.wwvb.time.time", lambda: 100.010)
    monkeypatch.setattr("gps_wwvb.devices.wwvb.time.sleep", slept.append)

    class _Gpio:
        def __init__(self):
            self.waits: list[float | None] = []

        def irq_wait(self, timeout=None):
            self.waits.append(timeout)
            return True

    class ES100:
        def __init__(self, **_kwargs):
            self._gpio = _Gpio()
            self._antenna = 1
            self._rx_antenna = "Antenna1"
            self._rx_complete = False
            self._cycle_complete = False
            self._status_ok = False
            self._system_time_received = None
            self.control0_writes: list[int] = []
            self.reports = 0
            self.outcomes = ["cycle_complete", "rx_complete"]

        def _start_rx(self):
            self.control0_writes.append(1)

        def _start_tracking(self):
            self.control0_writes.append(0x11)

        def _write_control0(self, val):
            self.control0_writes.append(val)

        def _report_status0_and_irq_reg(self):
            self.reports += 1
            if self.reports % 2 == 1:
                self._rx_complete = False
                self._cycle_complete = False
                self._status_ok = False
                return
            outcome = self.outcomes.pop(0)
            if outcome == "cycle_complete":
                self._rx_complete = False
                self._cycle_complete = True
                self._status_ok = False
            else:
                self._rx_complete = True
                self._cycle_complete = False
                self._status_ok = True

        def _wait_for_interrupt(self, timeout=None):
            return self._gpio.irq_wait(timeout)

        def _es100_receive(self, tracking=False):
            del tracking

        def time(self):
            self._es100_receive(False)
            if not self._status_ok:
                return None
            return datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

        def system_time(self):
            return self._system_time_received

        def rx_antenna(self):
            return self._rx_antenna

        def is_presently_dst(self):
            return False

        def leap_second(self):
            return None

    _install(es100_module, ES100)
    receiver = Es100Wwvb(11, 7, 1, 0x32)
    with caplog.at_level(logging.INFO, logger="gps_wwvb.devices.wwvb"):
        assert receiver.receive() is None
    assert any("no reception on ant 1 (cycle complete)" in rec.message for rec in caplog.records)
    assert receiver._dev.control0_writes == [1, 0]  # pylint: disable=protected-access
    assert slept and slept[0] == pytest.approx(0.0365, abs=1e-4)

    reading = receiver.receive()
    assert reading is not None
    assert reading.antenna == 1
    assert reading.system_time.tzinfo is UTC
