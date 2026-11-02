"""smbus2 I2C transactions, plus an in-memory stand-in for tests and replay."""

from __future__ import annotations

from typing import Any

from smbus2 import SMBus, i2c_msg


class Smbus2Link:
    """Register reads and writes via ``smbus2.i2c_rdwr``.

    ``bus`` is either an I2C bus number or an object that already provides
    ``i2c_rdwr`` (used by tests).
    """

    def __init__(self, bus: int | object):
        self._owned = False
        self._bus: Any
        if isinstance(bus, int):  # pragma: no cover (real I2C bus; Pi only)
            self._bus = SMBus(bus)
            self._owned = True
        else:
            self._bus = bus

    def read_at(self, addr: int, reg: int, n: int) -> bytes:
        write = i2c_msg.write(addr, [reg & 0xFF])
        read = i2c_msg.read(addr, n)
        self._bus.i2c_rdwr(write, read)
        return bytes(read)

    def write_at(self, addr: int, reg: int, data: bytes) -> None:
        payload = bytes([reg & 0xFF]) + bytes(data)
        self._bus.i2c_rdwr(i2c_msg.write(addr, payload))

    def close(self) -> None:
        if self._owned:  # pragma: no cover (real I2C bus; Pi only)
            self._bus.close()
            self._owned = False


class BufferLink:
    """Fake DDC port. Register 0xFD is the byte count; 0xFF is the stream."""

    def __init__(self, data: bytes = b""):
        self.buf = bytearray(data)
        self.writes: list[bytes] = []

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def read_at(self, addr: int, reg: int, n: int) -> bytes:
        del addr
        if reg == 0xFD:
            count = len(self.buf)
            raw = bytes([(count >> 8) & 0xFF, count & 0xFF])
            return raw[:n]
        if reg == 0xFF:
            take = min(n, len(self.buf))
            out = bytes(self.buf[:take])
            del self.buf[:take]
            if len(out) < n:
                out += bytes(n - len(out))
            return out
        return bytes(n)

    def write_at(self, addr: int, reg: int, data: bytes) -> None:
        del addr, reg
        self.writes.append(bytes(data))

    def close(self) -> None:
        return None
