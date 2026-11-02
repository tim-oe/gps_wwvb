"""ES100 wrapper, exercised with a stand-in for the Pi-only es100 package."""

# pylint: disable=redefined-outer-name

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
