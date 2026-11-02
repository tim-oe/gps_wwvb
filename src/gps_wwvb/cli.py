"""Command line for the GPS, WWVB, and compass monitor."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from gps_wwvb import __version__
from gps_wwvb.config import (
    Config,
    ConfigError,
    config_file,
    load_config,
    set_compass_calibrated,
    update_compass_calibration,
)
from gps_wwvb.devices.compass import CompassError, hard_iron_fit, make_reading
from gps_wwvb.devices.factory import Devices, backend_name, close_devices, open_devices
from gps_wwvb.geo import (
    antenna_is_aimed,
    antenna_turn_deg,
    format_turn,
    magnetic_declination_deg,
    propagation,
    true_heading_deg,
)
from gps_wwvb.monitor import Monitor, format_number
from gps_wwvb.recording import RecordingError, record_devices
from gps_wwvb.util.logging import init_logging

logger = logging.getLogger(__name__)

# systemd RestartPreventExitStatus keeps these from restart-looping the service.
EXIT_CONFIG = 2
EXIT_UNCALIBRATED = 3

FIX_NAMES = {
    0: "none",
    1: "dead-reckoning",
    2: "2D",
    3: "3D",
    4: "GNSS+DR",
    5: "time-only",
}


COMMANDS = {
    "status",
    "run",
    "compass",
    "orientate",
    "calibrate",
    "calibrate-compass",
    "record",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(normalize_argv(argv))
    try:
        init_logging()
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    if config.path is None:
        logger.info("No config file found; using built-in defaults.")
    else:
        logger.info("Loaded config %s.", config.path)
    name = backend_name(simulate=args.simulate, replay=args.replay)
    if args.cmd == "run" and name == "hardware" and not config.compass_calibrated:
        logger.error("%s", _uncalibrated_message(config))
        return EXIT_UNCALIBRATED
    if name == "simulate" and not args.simulate and args.replay is None:
        logger.info("Using the simulator (this machine is not ARM Linux).")
    elif name == "simulate":
        logger.info("Using the simulator.")
    elif name == "replay":
        logger.info("Replaying %s.", args.replay)
    try:
        devices = open_devices(config, simulate=args.simulate, replay=args.replay)
    except Exception:
        logger.exception("Failed to open devices")
        return 1
    try:
        return args.func(args, config, devices)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    finally:
        close_devices(*devices)


def normalize_argv(argv: list[str] | None) -> list[str] | None:
    """Allow ``--simulate`` and ``--config`` before or after the subcommand."""
    if argv is None:
        return None
    optional: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--simulate":
            optional.append(arg)
        elif arg in {"--config", "--replay"}:
            optional.append(arg)
            if index + 1 < len(argv):
                index += 1
                optional.append(argv[index])
        elif arg.startswith("--config=") or arg.startswith("--replay="):
            optional.append(arg)
        else:
            rest.append(arg)
        index += 1
    if not rest or rest[0] not in COMMANDS:
        return list(argv)
    return [rest[0], *optional, *rest[1:]]


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="TOML config file")
    common.add_argument(
        "--simulate",
        action="store_true",
        help="use the simulator even on the Pi",
    )
    common.add_argument("--replay", type=Path, metavar="DIR", help="replay a recording directory")

    parser = argparse.ArgumentParser(prog="gps-wwvb", description="GPS, WWVB, and compass monitor")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", parents=[common], help="read GPS, WWVB, and the compass once")
    status.set_defaults(func=cmd_status)

    run = sub.add_parser(
        "run",
        parents=[common],
        help="log the WWVB-versus-GPS offset until stopped",
    )
    run.set_defaults(func=cmd_run)

    compass = sub.add_parser(
        "compass",
        parents=[common],
        help="show heading and the turn toward WWVB",
    )
    compass.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between status refreshes",
    )
    compass.set_defaults(func=cmd_compass)

    orientate = sub.add_parser(
        "orientate",
        parents=[common],
        help="print the heading and how far +X is from WWVB",
    )
    orientate.add_argument("--interval", type=float, default=0.2, help="seconds between samples")
    orientate.set_defaults(func=cmd_orientate)

    calibrate = sub.add_parser(
        "calibrate",
        parents=[common],
        help="calibrate the compass and point the open end of the V at WWVB",
    )
    _add_sweep_args(calibrate)
    calibrate.add_argument(
        "--tolerance",
        type=float,
        default=5.0,
        help="degrees of error that still count as aimed at WWVB",
    )
    calibrate.add_argument(
        "--aim-samples",
        type=int,
        default=5,
        help="consecutive aimed samples required",
    )
    calibrate.add_argument(
        "--aim-interval",
        type=float,
        default=0.2,
        help="seconds between aim samples",
    )
    calibrate.add_argument(
        "--aim-timeout",
        type=float,
        default=180.0,
        help="seconds allowed to aim",
    )
    calibrate.add_argument(
        "--fix-timeout",
        type=float,
        default=45.0,
        help="seconds to wait for a GPS fix",
    )
    calibrate.add_argument(
        "--aim-only",
        action="store_true",
        help="skip the magnetometer sweep and check the antenna with the saved offsets",
    )
    calibrate.set_defaults(func=cmd_calibrate)

    sweep = sub.add_parser(
        "calibrate-compass",
        parents=[common],
        help="sweep hard-iron offsets only; this leaves the monitor stopped",
    )
    _add_sweep_args(sweep)
    sweep.set_defaults(func=cmd_calibrate_compass)

    record = sub.add_parser("record", parents=[common], help="save raw device data for replay")
    record.add_argument("--out", type=Path, required=True, help="directory to create")
    record.add_argument("--duration", type=float, required=True, help="seconds to record")
    record.set_defaults(func=cmd_record)
    return parser


def cmd_status(args, config, devices) -> int:
    del args, config
    gps, compass, wwvb = devices
    fix = _call("GPS", gps.read_fix, 5.0)
    if fix is None:
        print("GPS: no fix")
    else:
        kind = FIX_NAMES.get(fix.fix_type, str(fix.fix_type))
        lat = format_number(fix.lat, ".6f")
        lon = format_number(fix.lon, ".6f")
        alt = format_number(fix.alt_m, ".1f")
        utc = fix.utc.isoformat() if fix.utc else "n/a"
        acc = "n/a" if fix.time_accuracy_ns is None else f"{fix.time_accuracy_ns} ns"
        print(
            f"GPS: {kind}  lat {lat}  lon {lon}  alt {alt} m  "
            f"time {utc}  acc {acc}  sv {fix.num_sv}"
        )
    heading = _call("compass", compass.read)
    heading_true, declination = _true_heading(None if heading is None else heading.heading_mag, fix)
    if heading is None:
        print("Compass: no sample")
    else:
        print(
            f"Compass: mag {format_number(heading.heading_mag, '.1f')}  "
            f"true {format_number(heading_true, '.1f')}  dec {format_number(declination, '+.1f')}  "
            f"({compass.variant})"
        )
    if fix is not None and fix.position_ok and fix.lat is not None and fix.lon is not None:
        distance, bearing, delay = propagation(fix.lat, fix.lon)
        turn = None if heading_true is None else antenna_turn_deg(heading_true, bearing)
        print(
            f"WWVB bearing {format_number(bearing, '.1f')} deg  "
            f"distance {format_number(distance, '.1f')} km  "
            f"travel {format_number(delay * 1000, '.3f')} ms  "
            f"antenna turn {format_turn(turn)}"
        )
    else:
        print("WWVB bearing: waiting for a GPS position")
    # The ES100 blocks until a decode, which can take minutes, so it runs last.
    reading = _call("WWVB", wwvb.receive)
    if reading is None:
        print("WWVB: no reception")
    else:
        print(
            f"WWVB: {reading.wwvb_time.isoformat()}  at {reading.system_time.isoformat()}"
            f"  antenna {reading.antenna}  dst {reading.dst}  leap {reading.leap_second}"
            f"{'  tracking' if reading.tracking else ''}"
        )
    return 0 if any(item is not None for item in (fix, reading, heading)) else 1


def cmd_run(args, config, devices) -> int:
    del args
    gps, compass, wwvb = devices
    monitor = Monitor(
        gps,
        compass,
        wwvb,
        log_path=config.log_path,
        log_format=config.log_format,
    )
    stop = threading.Event()

    def _stop(signum, _frame):
        stop.set()
        if signum == signal.SIGTERM:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        monitor.run(stop)
    finally:
        monitor.close()
    return 0


def cmd_compass(args, config, devices) -> int:
    del config
    gps, compass, _wwvb = devices
    fix = _call("GPS", gps.read_fix, 2.0)
    bearing = _bearing(fix)
    if bearing is None:
        print("No GPS position yet; showing magnetic heading only.")
    else:
        _announce_bearing(fix, bearing)
    try:
        while True:
            if bearing is None:
                fix = _call("GPS", gps.read_fix, 2.0)
                bearing = _bearing(fix)
                if bearing is not None:
                    _announce_bearing(fix, bearing)
            sample = compass.read()
            if sample is None:
                _finish_status()
                return 0
            heading_true, declination = _true_heading(sample.heading_mag, fix)
            turn = None
            if bearing is not None and heading_true is not None:
                turn = antenna_turn_deg(heading_true, bearing)
            _write_status(
                f"heading mag {format_number(sample.heading_mag, '.1f')}  "
                f"true {format_number(heading_true, '.1f')}  "
                f"dec {format_number(declination, '+.1f')}  "
                f"brg {format_number(bearing, '.1f')}  "
                f"turn {format_turn(turn)}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _finish_status()
        return 130


def _bearing(fix) -> float | None:
    if fix is None or not fix.position_ok or fix.lat is None or fix.lon is None:
        return None
    _distance, bearing, _delay = propagation(fix.lat, fix.lon)
    return bearing


def _announce_bearing(fix, bearing: float) -> None:
    distance, _bearing, _delay = propagation(fix.lat, fix.lon)
    _heading, declination = _true_heading(0.0, fix)
    print(
        f"WWVB bearing {bearing:.1f} deg at {distance:.1f} km"
        f"  declination {format_number(declination, '+.1f')} deg"
        "  (open end of the V, along +X, points at WWVB)"
    )


def cmd_orientate(args, config, devices) -> int:
    gps, compass, wwvb = devices
    _release_receiver(wwvb)
    if not config.compass_calibrated:
        print("The open end is not marked on WWVB yet. Off is the turn still required.")
    fix = gps.read_fix(timeout=5.0)
    bearing = None
    if fix is not None and fix.position_ok and fix.lat is not None and fix.lon is not None:
        _distance, bearing, _delay = propagation(fix.lat, fix.lon)
        _heading, declination = _true_heading(0.0, fix)
        print(
            f"WWVB bearing {bearing:.1f} deg. Declination {format_number(declination, '+.1f')} deg."
            " Point the open end of the V (+X) there."
        )
    else:
        print("No GPS position yet; the set heading is unknown.")
    try:
        while True:
            sample = compass.read()
            if sample is None:
                return 0
            heading_true, _declination = _true_heading(sample.heading_mag, fix)
            print(format_orientation(heading_true, bearing), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


def _write_status(line: str) -> None:
    """Rewrite one status line on a terminal; otherwise print a new line."""
    if sys.stdout.isatty():
        print(f"\r{line}\033[K", end="", flush=True)
    else:
        print(line, flush=True)


def _finish_status() -> None:
    """Leave the cursor on the next line after an in-place status."""
    if sys.stdout.isatty():
        print()


def _true_heading(heading_mag: float | None, fix) -> tuple[float | None, float | None]:
    """True heading and east-positive declination from a GPS fix."""
    lat = lon = alt = None
    when = datetime.now(UTC)
    if fix is not None and fix.position_ok and fix.lat is not None and fix.lon is not None:
        lat, lon, alt = fix.lat, fix.lon, fix.alt_m
        if fix.utc is not None:
            when = fix.utc
    return true_heading_deg(heading_mag, lat, lon, alt, when)


def format_orientation(heading: float | None, bearing: float | None) -> str:
    """Pointing, the bearing to WWVB, and the degrees still to turn."""
    if heading is None or bearing is None:
        return f"pointing {format_number(heading, '.1f')} deg  set n/a  off n/a"
    off = antenna_turn_deg(heading, bearing)
    return f"pointing {heading:.1f} deg  set {bearing % 360.0:.1f} deg  off {format_turn(off)}"


def cmd_calibrate(args, config, devices) -> int:
    gps, compass, wwvb = devices
    _release_receiver(wwvb)
    if args.aim_only:
        return _verify_antenna(args, config, gps, compass, config.mag_offset, config.mag_scale)
    fitted = _sweep_compass(args, config, compass)
    if fitted is None:
        return 1
    offset, scale = fitted
    return _verify_antenna(args, config, gps, compass, offset, scale)


def cmd_calibrate_compass(args, config, devices) -> int:
    _gps, compass, wwvb = devices
    _release_receiver(wwvb)
    fitted = _sweep_compass(args, config, compass)
    if fitted is None:
        return 1
    print(
        "Magnetometer offsets are saved. The monitor stays stopped until the antenna check passes."
    )
    path = config_file(config)
    print(f"Run: gps-wwvb --config {path} calibrate --aim-only")
    return 0


def _add_sweep_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to rotate the board")
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0.02,
        help="seconds between magnetometer samples",
    )


def _sweep_compass(args, config, compass):
    print(f"Rotate the board through every orientation for {args.duration:.0f}s.")
    samples: list[tuple[float, float, float]] = []
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        _ax, _ay, _az, mx, my, mz = compass.read_counts()
        samples.append((mx, my, mz))
        time.sleep(args.sample_interval)
    try:
        offset, scale = hard_iron_fit(samples)
    except CompassError as exc:
        print(exc, file=sys.stderr)
        return None
    path = config_file(config)
    try:
        written = update_compass_calibration(path, offset, scale)
    except OSError as exc:
        print(f"Could not write {path}: {exc}", file=sys.stderr)
        print(f"offset {offset}  scale {scale}")
        return None
    print(
        f"Wrote {written}: offset ({offset[0]:.1f}, {offset[1]:.1f}, {offset[2]:.1f})"
        f"  scale ({scale[0]:.3f}, {scale[1]:.3f}, {scale[2]:.3f})"
    )
    return offset, scale


def _verify_antenna(args, config, gps, compass, offset, scale) -> int:
    print("Point the open end of the V at WWVB. +X runs out through that opening.")
    fix = _wait_for_fix(gps, args.fix_timeout)
    if fix is None:
        print("No GPS position, so the antenna aim cannot be checked.", file=sys.stderr)
        return 1
    _distance, bearing, _delay = propagation(fix.lat, fix.lon)
    when = fix.utc if fix.utc is not None else datetime.now(UTC)
    declination = magnetic_declination_deg(fix.lat, fix.lon, fix.alt_m, when)
    print(f"Bearing {bearing:.1f} deg. Point +X there.  Declination {declination:+.1f} deg.")
    path = config_file(config)
    steady = 0
    deadline = time.monotonic() + args.aim_timeout
    while time.monotonic() < deadline:
        ax, ay, az, mx, my, mz = compass.read_counts()
        reading = make_reading(
            ax,
            ay,
            az,
            mx,
            my,
            mz,
            offset=offset,
            scale=scale,
            declination_deg=declination,
        )
        if reading.heading_true is None:
            steady = 0
            print("heading n/a", flush=True)
        else:
            turn = antenna_turn_deg(reading.heading_true, bearing)
            aimed = antenna_is_aimed(reading.heading_true, bearing, args.tolerance)
            steady = steady + 1 if aimed else 0
            print(
                f"heading {reading.heading_true:.1f}  turn {format_turn(turn)}"
                f"  ({steady}/{args.aim_samples})",
                flush=True,
            )
            if steady >= args.aim_samples:
                try:
                    written = set_compass_calibrated(path, True)
                except OSError as exc:
                    print(f"Could not write {path}: {exc}", file=sys.stderr)
                    return 1
                print(f"Open end is aimed at WWVB. Marked {written} calibrated.")
                print("Start the monitor with: make svc-start")
                return 0
        time.sleep(args.aim_interval)
    print(
        "The open end did not stay on WWVB. The monitor stays stopped.",
        file=sys.stderr,
    )
    return 1


def _wait_for_fix(gps, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fix = gps.read_fix(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
        if fix is not None and fix.position_ok and fix.lat is not None and fix.lon is not None:
            return fix
        time.sleep(0.05)
    return None


def _release_receiver(wwvb) -> None:
    close = getattr(wwvb, "close", None)
    if close:
        close()


def _uncalibrated_message(config: Config) -> str:
    path = config_file(config)
    return (
        "The antenna has not been calibrated, so the monitor will not start.\n"
        f"Run: gps-wwvb --config {path} calibrate\n"
        "That records the magnetometer, then checks the open end of the V points at WWVB."
    )


def cmd_record(args, config, devices: Devices) -> int:
    del config
    gps, compass, wwvb = devices
    print(f"Recording to {args.out} for {args.duration:.1f}s.")
    try:
        record_devices(gps, compass, wwvb, args.out, args.duration)
    except RecordingError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Saved {args.out}")
    return 0


def _call(label: str, func, *args):
    try:
        return func(*args)
    except Exception as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
