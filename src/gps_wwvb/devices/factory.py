"""Choose the simulator, a recording, or the real devices."""

from __future__ import annotations

import logging
import platform
from pathlib import Path

from gps_wwvb.config import Config
from gps_wwvb.devices.compass import make_reading
from gps_wwvb.devices.gps_gpsd import GpsdGps
from gps_wwvb.devices.replay import ReplayCompass, ReplayGps, ReplayWwvb
from gps_wwvb.devices.sim import SimCompass, SimGps, SimWwvb
from gps_wwvb.devices.types import CompassReading, CompassSource, Devices, GpsSource, WwvbSource
from gps_wwvb.devices.wwvb import Es100Wwvb

logger = logging.getLogger(__name__)

# adafruit_lsm303_accel and adafruit_lis2mdl hardcode these addresses.
ADAFRUIT_ACCEL_ADDRESS = 0x19
ADAFRUIT_MAG_ADDRESS = 0x1E


def is_arm_linux() -> bool:
    machine = platform.machine().lower()
    return platform.system() == "Linux" and (
        machine.startswith("arm") or machine.startswith("aarch")
    )


def backend_name(*, simulate: bool, replay: Path | None) -> str:
    if replay is not None:
        return "replay"
    if simulate or not is_arm_linux():
        return "simulate"
    return "hardware"


def open_devices(config: Config, *, simulate: bool, replay: Path | None) -> Devices:
    """Return ``(gps, compass, wwvb)`` for the selected backend."""
    if replay is not None:
        return Devices(
            ReplayGps(replay / "gps.jsonl"),
            ReplayCompass(
                replay / "compass.jsonl",
                offset=config.mag_offset,
                scale=config.mag_scale,
            ),
            ReplayWwvb(replay / "wwvb.jsonl"),
        )
    if simulate or not is_arm_linux():
        return Devices(
            SimGps(
                config.sim_lat,
                config.sim_lon,
                config.sim_alt_m,
                interval_s=config.sim_gps_interval_s,
            ),
            SimCompass(
                config.sim_lat,
                config.sim_lon,
                alt_m=config.sim_alt_m,
                offset=config.mag_offset,
                scale=config.mag_scale,
                interval_s=config.sim_compass_interval_s,
            ),
            SimWwvb(
                config.sim_lat,
                config.sim_lon,
                interval_s=config.sim_wwvb_interval_s,
            ),
        )
    compass = None
    try:
        from gps_wwvb.devices.compass_adafruit import AdafruitLsm303

        if (config.compass_accel_address, config.compass_mag_address) != (
            ADAFRUIT_ACCEL_ADDRESS,
            ADAFRUIT_MAG_ADDRESS,
        ):
            logger.warning(
                "Adafruit drivers use accel 0x%02x and mag 0x%02x; "
                "config asked for 0x%02x and 0x%02x",
                ADAFRUIT_ACCEL_ADDRESS,
                ADAFRUIT_MAG_ADDRESS,
                config.compass_accel_address,
                config.compass_mag_address,
            )
        gps = GpsdGps(config.gpsd_host, config.gpsd_port)
        compass = AdafruitLsm303()
    except Exception:
        if compass is not None:
            compass.close()
        raise
    return Devices(
        gps,
        CalibratedCompass(compass, config),
        Es100Wwvb(config.es100_irq, config.es100_en, config.i2c_bus, config.es100_address),
    )


class CalibratedCompass:
    """Apply the config file's hard-iron correction to an LSM303.

    Declination is applied later, from the GPS fix.
    """

    def __init__(self, device: CompassSource, config: Config):
        self._device = device
        self._config = config
        self.variant = device.variant

    def read(self) -> CompassReading:
        ax, ay, az, mx, my, mz = self._device.read_counts()
        return make_reading(
            ax,
            ay,
            az,
            mx,
            my,
            mz,
            offset=self._config.mag_offset,
            scale=self._config.mag_scale,
            declination_deg=0.0,
        )

    def read_counts(self) -> tuple[float, float, float, float, float, float]:
        return self._device.read_counts()

    def close(self) -> None:
        self._device.close()


def close_devices(*devices: GpsSource | CompassSource | WwvbSource | None) -> None:
    for device in devices:
        if device is None:
            continue
        try:
            device.close()
        except Exception:
            logger.warning("failed to close %s", type(device).__name__, exc_info=True)
