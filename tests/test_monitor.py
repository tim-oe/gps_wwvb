"""Offset math, the maintenance gap, and record/replay."""

import io
import json
import logging
import math
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gps_wwvb.cli import (
    EXIT_UNCALIBRATED,
    _sweep_compass,
    _verify_antenna,
    build_parser,
    cmd_calibrate,
    cmd_compass,
    cmd_orientate,
    format_orientation,
    main,
)
from gps_wwvb.config import load_config
from gps_wwvb.devices.factory import backend_name, close_devices, is_arm_linux, open_devices
from gps_wwvb.devices.gps_ubx import GpsError, build_nav_pvt
from gps_wwvb.devices.replay import ReplayCompass
from gps_wwvb.devices.types import CompassReading, GpsFix, WwvbReading
from gps_wwvb.geo import WWVB_LAT, WWVB_LON, bearing_deg, magnetic_declination_deg, propagation
from gps_wwvb.monitor import (
    GPS_FIX_MAX_AGE_S,
    LOG_COLUMNS,
    Monitor,
    Observation,
    SampleLog,
    compute_offset_ms,
    format_observation,
)
from gps_wwvb.recording import RecordingError, record_devices

ROOT = Path(__file__).resolve().parents[1]
SJC = (37.363056, -121.928611)


def test_config_uses_the_scanned_addresses():
    config = load_config(ROOT / "gps_wwvb.toml")
    assert config.i2c_bus == 1
    assert config.gpsd_host == "127.0.0.1"
    assert config.gpsd_port == 2947
    assert config.compass_accel_address == 0x19
    assert config.compass_mag_address == 0x1E
    assert config.es100_address == 0x32
    assert (config.es100_irq, config.es100_en) == (11, 7)
    assert config.compass_calibrated is False


def test_offset_adds_travel_time_and_the_gps_clock_offset():
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    irq = base + timedelta(seconds=0.1)
    offset_ms = compute_offset_ms(base, irq, travel_s=0.005, gps_minus_system_s=0.050)
    assert offset_ms == pytest.approx(-145.0)


def test_monitor_uses_the_gps_position_for_the_correction():
    irq = datetime(2026, 11, 1, 12, 0, 0, 100000, tzinfo=UTC)
    fix = GpsFix(
        lat=SJC[0],
        lon=SJC[1],
        alt_m=18.9,
        utc=irq + timedelta(seconds=0.050),
        fix_type=3,
        valid_date=True,
        valid_time=True,
        fully_resolved=True,
        gnss_fix_ok=True,
        time_accuracy_ns=50,
        num_sv=8,
        received_at=irq.timestamp(),
    )
    reading = WwvbReading(
        wwvb_time=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        system_time=irq,
        antenna=2,
        dst=False,
        leap_second=None,
        tracking=False,
    )
    _distance, bearing, travel = propagation(*SJC)
    declination = magnetic_declination_deg(SJC[0], SJC[1], 18.9, fix.utc)
    magnetic = (bearing - declination) % 360.0
    monitor = Monitor(
        _Gps(fix),
        _Compass(magnetic),
        _Wwvb(),
        log_path=None,
        log_format="jsonl",
        echo=False,
    )
    monitor.note_fix(fix)
    observation = monitor.handle_reading(reading)
    assert observation.offset_ms == pytest.approx((travel - 0.150) * 1000, abs=0.01)
    assert observation.distance_km == pytest.approx(1501.66, abs=0.01)
    assert observation.bearing_deg == pytest.approx(bearing)
    assert observation.antenna == 2
    assert observation.declination_deg == pytest.approx(declination)
    assert observation.heading_true == pytest.approx(bearing, abs=0.05)
    assert observation.antenna_turn_deg == pytest.approx(0.0, abs=0.1)


def test_maintenance_gap_does_not_touch_the_receiver(tmp_path: Path):
    monitor = Monitor(
        _Gps(_sjc_fix()),
        _Compass(),
        _ExplodingWwvb(),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 12, tzinfo=UTC)
    monitor.run(threading.Event(), max_loops=1)
    text = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "maintenance_gap" in text


def test_maintenance_gap_holds_when_a_frame_would_run_into_the_break(tmp_path: Path):
    monitor = Monitor(
        _Gps(_sjc_fix()),
        _Compass(),
        _ExplodingWwvb(),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 8, 30, tzinfo=UTC)
    monitor.run(threading.Event(), max_loops=1)
    text = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "maintenance_gap" in text


def test_maintenance_gap_is_logged_once(tmp_path: Path):
    monitor = Monitor(
        _Gps(_sjc_fix()),
        _Compass(),
        _ExplodingWwvb(),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 12, tzinfo=UTC)
    stop = threading.Event()

    def _wait(timeout=None):
        del timeout
        return stop.is_set()

    stop.wait = _wait  # type: ignore[method-assign]
    monitor.run(stop, max_loops=5)
    rows = (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["event"] == "maintenance_gap"


def test_run_logs_a_reception(tmp_path: Path):
    reading = _reading()
    monitor = Monitor(
        _Gps(_sjc_fix()),
        _Compass(),
        _ScriptedWwvb([reading]),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    monitor.run(threading.Event(), max_readings=1)
    row = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))
    assert row["event"] == "reception"
    assert row["antenna"] == 2
    assert row["distance_km"] == pytest.approx(1501.66, abs=0.01)


def test_empty_fix_does_not_replace_a_position_or_keep_a_stale_offset():
    monitor = Monitor(
        _Gps(None), _Compass(), _Wwvb(), log_path=None, log_format="jsonl", echo=False
    )
    good = _sjc_fix()
    monitor.note_fix(good)
    empty = GpsFix(
        lat=0.0,
        lon=0.0,
        alt_m=None,
        utc=None,
        fix_type=0,
        valid_date=False,
        valid_time=False,
        fully_resolved=False,
        gnss_fix_ok=False,
        time_accuracy_ns=None,
        num_sv=0,
        received_at=0.0,
    )
    monitor.note_fix(empty)
    fresh = monitor.handle_reading(_reading())
    assert fresh.lat == pytest.approx(SJC[0])
    assert fresh.offset_ms is not None
    aged = time.monotonic() - (GPS_FIX_MAX_AGE_S + 1)
    monitor._noted_at = aged  # pylint: disable=protected-access
    stale = monitor.handle_reading(_reading())
    assert stale.lat == pytest.approx(SJC[0])
    assert stale.offset_ms is None
    assert stale.fix_age_s is not None
    assert stale.fix_age_s > GPS_FIX_MAX_AGE_S
    assert stale.day_night is not None


def test_reception_is_logged_at_info(caplog):
    monitor = Monitor(_Gps(None), _Compass(), _Wwvb(), log_path=None, log_format="jsonl", echo=True)
    with caplog.at_level(logging.DEBUG, logger="gps_wwvb.monitor"):
        monitor.handle_reading(_reading())
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    infos = [record.message for record in caplog.records if record.levelno == logging.INFO]
    assert any("offset" in message for message in infos)
    assert not any("offset" in message for message in warnings)


def test_format_observation_matches_the_reception_line():
    when = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    observation = _observation(when, offset_ms=1.2, fix_age_s=0.2, day_night="day")
    text = format_observation(observation)
    assert text == (
        "2026-11-01 12:00:00Z offset +1.2 ms  ant 1  1501.7 km  brg 70.6  "
        "hdg 70.6  dec +12.7  turn 0.0 deg  day  fix 0.2s"
    )
    early_irq = datetime(2026, 11, 2, 18, 22, 49, 993000, tzinfo=UTC)
    rounded = format_observation(
        _observation(early_irq, offset_ms=19.2, fix_age_s=0.2, day_night="day")
    )
    assert rounded.startswith("2026-11-02 18:22:50Z offset +19.2 ms")


def test_csv_log_writes_a_header_and_blanks_for_missing_values(tmp_path: Path):
    path = tmp_path / "log.csv"
    log = SampleLog(path, "csv")
    log.write({"event": "reception", "offset_ms": None})
    log.close()
    header, row = path.read_text(encoding="utf-8").splitlines()
    assert header.split(",") == list(LOG_COLUMNS)
    fields = row.split(",")
    assert fields[0] == "reception"
    assert fields[LOG_COLUMNS.index("offset_ms")] == ""


def test_a_fresh_fix_is_reused_for_the_reception(tmp_path: Path, caplog):
    gps = _Gps(_sjc_fix())
    calls: list[float] = []

    def read_fix(timeout: float = 2.0):
        calls.append(timeout)
        return gps.fix

    gps.read_fix = read_fix  # type: ignore[method-assign]
    monitor = Monitor(
        gps,
        _Compass(),
        _ScriptedWwvb([_reading()]),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    monitor._gps_poll_s = 30.0  # pylint: disable=protected-access

    with caplog.at_level(logging.INFO):
        monitor.run(threading.Event(), max_readings=1)
    assert calls == [2.0]
    info = [record.message for record in caplog.records if record.levelno == logging.INFO]
    assert any(message.startswith("GPS:") for message in info)
    assert any(message.startswith("Compass: mag 70.6") and "turn " in message for message in info)
    assert not any("off WWVB" in record.message for record in caplog.records)
    row = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))
    assert row["event"] == "reception"
    assert row["lat"] == pytest.approx(SJC[0])

    stop = threading.Event()

    def fail(timeout: float = 2.0):
        del timeout
        stop.set()
        raise GpsError("bus down")

    failed = Monitor(
        _Gps(None),
        _Compass(),
        _ScriptedWwvb([_reading()]),
        log_path=None,
        log_format="jsonl",
        echo=False,
    )
    failed.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    failed.gps.read_fix = fail  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        failed.run(stop, max_readings=1)
    assert "bus down" in caplog.text
    assert failed.wwvb.receive_calls == 0


def test_gps_keeps_sampling_while_wwvb_receive_is_blocked(tmp_path: Path):
    gps = _Gps(_sjc_fix())
    calls: list[float] = []

    def read_fix(timeout: float = 2.0):
        calls.append(timeout)
        return _sjc_fix()

    gps.read_fix = read_fix  # type: ignore[method-assign]
    monitor = Monitor(
        gps,
        _Compass(),
        _SlowWwvb(0.35),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    monitor._gps_poll_s = 0.05  # pylint: disable=protected-access
    monitor.run(threading.Event(), max_readings=1)
    assert len(calls) >= 3
    row = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))
    assert row["offset_ms"] is not None
    assert row["fix_age_s"] < GPS_FIX_MAX_AGE_S


def test_startup_warns_when_the_antenna_is_more_than_15_degrees_off(tmp_path: Path, caplog):
    monitor = Monitor(
        _Gps(_sjc_fix()),
        _Compass(heading_mag=180.0),
        _ScriptedWwvb([_reading()]),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO):
        monitor.run(threading.Event(), max_readings=1)
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert any("counterclockwise" in message and "tolerance ±15" in message for message in warnings)


def test_startup_selects_single_antenna_when_rotated_broadside(tmp_path: Path, caplog):
    fix = _sjc_fix()
    _distance, bearing, _travel = propagation(*SJC)
    declination = magnetic_declination_deg(SJC[0], SJC[1], 18.9, fix.utc)
    # 45 deg counterclockwise of bearing (turn +45 deg clockwise) puts antenna 2 broadside.
    mag_ant2 = (bearing - 45.0 - declination) % 360.0
    wwvb = _ScriptedWwvb([_reading()])
    monitor = Monitor(
        _Gps(fix),
        _Compass(heading_mag=mag_ant2),
        wwvb,
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO):
        monitor.run(threading.Event(), max_readings=1)
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    infos = [record.message for record in caplog.records if record.levelno == logging.INFO]
    assert not warnings
    assert any(
        "Antenna: optimized for antenna 2 (skipping null antenna 1)" in message for message in infos
    )
    assert wwvb.selected_antenna == 2


def test_a_missed_gps_read_is_warned_once(caplog):
    monitor = Monitor(
        _Gps(None), _Compass(), _Wwvb(), log_path=None, log_format="jsonl", echo=False
    )
    with caplog.at_level(logging.WARNING):
        assert monitor._sample_gps() is None  # pylint: disable=protected-access
        assert monitor._sample_gps() is None  # pylint: disable=protected-access
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["GPS read timed out"]


def test_an_empty_nav_pvt_is_warned(caplog):
    monitor = Monitor(
        _Gps(_empty_fix()), _Compass(), _Wwvb(), log_path=None, log_format="jsonl", echo=False
    )
    with caplog.at_level(logging.WARNING):
        monitor._sample_gps()  # pylint: disable=protected-access
    assert "GPS report has no position or time" in caplog.text


def test_startup_retries_until_the_gps_fix_is_usable(tmp_path: Path, caplog):
    gps = _Gps(None)
    samples = [None, _empty_fix(), _sjc_fix()]

    def read_fix(timeout: float = 2.0):
        del timeout
        if samples:
            return samples.pop(0)
        return _sjc_fix()

    gps.read_fix = read_fix  # type: ignore[method-assign]
    monitor = Monitor(
        gps,
        _Compass(),
        _ScriptedWwvb([_reading()]),
        log_path=tmp_path / "log.jsonl",
        log_format="jsonl",
        echo=False,
    )
    monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    stop = threading.Event()
    stop.wait = lambda timeout=None: False  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        monitor.run(stop, max_readings=1)
    assert not samples
    assert "waiting for GPS fix" in caplog.text
    row = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))
    assert row["lat"] == pytest.approx(SJC[0])


def test_replay_uses_the_sample_from_the_reception_time(tmp_path: Path):
    early = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    late = early + timedelta(seconds=10)
    rec = tmp_path / "rec"
    rec.mkdir()
    early_frame = build_nav_pvt(lat=10.0, lon=20.0, alt_m=1.0, when=early)
    late_frame = build_nav_pvt(lat=30.0, lon=40.0, alt_m=2.0, when=late)
    (rec / "gps.jsonl").write_text(
        json.dumps({"t": early.timestamp(), "hex": early_frame.hex()})
        + "\n"
        + json.dumps({"t": late.timestamp(), "hex": late_frame.hex()})
        + "\n",
        encoding="utf-8",
    )
    (rec / "compass.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"t": early.timestamp(), "ax": 0, "ay": 0, "az": 1000, "mx": 400, "my": 0, "mz": 0},
                {"t": late.timestamp(), "ax": 0, "ay": 0, "az": 1000, "mx": 0, "my": 400, "mz": 0},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (rec / "wwvb.jsonl").write_text(
        json.dumps(
            {
                "t": early.timestamp() + 0.2,
                "wwvb_time": early.isoformat(),
                "system_time": (early + timedelta(seconds=0.2)).isoformat(),
                "antenna": 1,
                "dst": False,
                "leap_second": None,
                "tracking": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_config(ROOT / "gps_wwvb.toml")
    devices = open_devices(config, simulate=False, replay=rec)
    try:
        monitor = Monitor(
            *devices,
            log_path=tmp_path / "log.jsonl",
            log_format="jsonl",
            echo=False,
        )
        monitor.clock = lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        monitor.run(threading.Event(), max_readings=1)
    finally:
        close_devices(*devices)
    row = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8"))
    assert row["lat"] == pytest.approx(10.0)
    assert row["heading_mag"] == pytest.approx(0.0, abs=0.1)


def test_replay_compass_returns_none_when_the_file_is_finished(tmp_path: Path):
    path = tmp_path / "compass.jsonl"
    path.write_text(
        json.dumps({"t": 1.0, "ax": 0, "ay": 0, "az": 1, "mx": 1, "my": 0, "mz": 0}) + "\n",
        encoding="utf-8",
    )
    compass = ReplayCompass(path, offset=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0))
    assert compass.read() is not None
    assert compass.read() is None


def test_record_reports_a_device_failure(tmp_path: Path):
    class _Boom:
        def read_counts(self):
            raise OSError("i2c")

    class _QuietGps:
        def read_raw(self, timeout: float = 0.3) -> bytes:
            del timeout
            return b""

    with pytest.raises(RecordingError, match="i2c"):
        record_devices(_QuietGps(), _Boom(), _ScriptedWwvb([]), tmp_path / "rec", 5.0)


def test_simulate_status_reports_the_sjc_path(capsys):
    assert main(["--config", str(ROOT / "gps_wwvb.toml"), "--simulate", "status"]) == 0
    out = capsys.readouterr().out
    assert "1501.7 km" in out
    assert "70.6" in out


def test_record_then_replay(tmp_path: Path, capsys):
    out = tmp_path / "rec"
    code = main(
        [
            "--config",
            str(ROOT / "gps_wwvb.toml"),
            "--simulate",
            "record",
            "--out",
            str(out),
            "--duration",
            "0.05",
        ]
    )
    assert code == 0
    assert (out / "gps.jsonl").stat().st_size > 0
    assert (out / "compass.jsonl").stat().st_size > 0
    assert (out / "wwvb.jsonl").stat().st_size > 0
    assert main(["--config", str(ROOT / "gps_wwvb.toml"), "--replay", str(out), "status"]) == 0
    replayed = capsys.readouterr().out
    assert "1501.7 km" in replayed


@pytest.mark.skipif(is_arm_linux(), reason="this machine would open the real I2C bus")
def test_off_pi_default_does_not_import_es100():
    sys.modules.pop("es100", None)
    config = load_config(ROOT / "gps_wwvb.toml")
    assert backend_name(simulate=False, replay=None) == "simulate"
    devices = open_devices(config, simulate=False, replay=None)
    try:
        assert "es100" not in sys.modules
        fix = devices[0].read_fix(timeout=1.0)
        assert fix is not None and fix.position_ok
    finally:
        close_devices(*devices)


def test_run_does_not_start_until_the_antenna_is_calibrated(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("gps_wwvb.cli.backend_name", lambda **_kwargs: "hardware")

    def opened(*_args, **_kwargs):
        raise AssertionError("run opened the devices")

    monkeypatch.setattr("gps_wwvb.cli.open_devices", opened)
    path = tmp_path / "gps_wwvb.toml"
    path.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["--config", str(path), "run"]) == EXIT_UNCALIBRATED
    message = capsys.readouterr().out
    assert "will not start" in message
    assert "calibrate" in message


def test_compass_refresh_defaults_to_one_second():
    args = build_parser().parse_args(["compass"])
    assert args.interval == 1.0
    assert args.ant is None
    assert build_parser().parse_args(["compass", "--ant", "1"]).ant == 1
    assert build_parser().parse_args(["compass", "--ant", "2"]).ant == 2


def test_compass_ant_flag_orientates_for_the_selected_antenna(monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    out = _Tty()
    monkeypatch.setattr("gps_wwvb.cli.sys.stdout", out)
    monkeypatch.setattr("gps_wwvb.cli.time.sleep", lambda _seconds: None)
    fix = _sjc_fix()
    _distance, bearing, _travel = propagation(*SJC)
    declination = magnetic_declination_deg(SJC[0], SJC[1], 18.9, fix.utc)
    mag_ant2 = (bearing - 45.0 - declination) % 360.0
    readings = [CompassReading(mag_ant2, (bearing - 45.0) % 360.0, 0, 0, 1, 1, 0, 0, 0.0)]

    class _Script:
        def read(self):
            return readings.pop(0) if readings else None

    args = SimpleNamespace(interval=1.0, ant=2)
    assert cmd_compass(args, None, (_Gps(fix), _Script(), None)) == 0
    text = out.getvalue()
    assert f"(antenna 2 broadside: point +X at {(bearing - 45.0) % 360.0:.1f} deg)" in text
    assert f"ant 2 {(bearing - 45.0) % 360.0:.1f}" in text
    assert "turn 0.0 deg" in text


def test_compass_waits_for_a_gps_fix_and_rewrites_one_line(monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    out = _Tty()
    slept = []
    monkeypatch.setattr("gps_wwvb.cli.sys.stdout", out)
    monkeypatch.setattr("gps_wwvb.cli.time.sleep", slept.append)
    fixes = [None, _empty_fix(), _sjc_fix()]
    readings = [
        CompassReading(10.0, 10.0, 0, 0, 1, 1, 0, 0, 0.0),
        CompassReading(20.0, 20.0, 0, 0, 1, 1, 0, 0, 0.0),
    ]

    class _ScriptGps:
        exhausted = False

        def read_fix(self, timeout: float = 2.0):
            del timeout
            return fixes.pop(0) if fixes else _sjc_fix()

    class _ScriptCompass:
        def read(self):
            assert not fixes, "compass was read before GPS had a fix"
            return readings.pop(0) if readings else None

    args = SimpleNamespace(interval=1.0)
    assert cmd_compass(args, None, (_ScriptGps(), _ScriptCompass(), None)) == 0
    text = out.getvalue()
    assert text.startswith("Waiting for GPS fix...\nWWVB bearing 70.6 deg at 1501.7 km")
    assert text.count("Waiting for GPS fix...") == 1
    assert text.count("\rheading mag") == 2
    assert "heading mag 20.0" in text
    assert "turn n/a" not in text
    assert text.endswith("\033[K\n")
    assert slept == [1.0, 1.0, 1.0, 1.0]


def test_compass_status_names_the_turn_direction(monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    out = _Tty()
    monkeypatch.setattr("gps_wwvb.cli.sys.stdout", out)
    monkeypatch.setattr("gps_wwvb.cli.time.sleep", lambda _seconds: None)
    readings = [CompassReading(180.0, 180.0, 0, 0, 1, 1, 0, 0, 0.0)]

    class _Script:
        def read(self):
            return readings.pop(0) if readings else None

    args = SimpleNamespace(interval=1.0)
    assert cmd_compass(args, None, (_Gps(_sjc_fix()), _Script(), None)) == 0
    text = out.getvalue()
    assert "turn " in text
    assert "dec " in text
    assert "brg " in text
    assert "counterclockwise" in text


def test_orientate_waits_for_a_gps_fix_before_reading_the_compass(monkeypatch, capsys):
    monkeypatch.setattr("gps_wwvb.cli.time.sleep", lambda _seconds: None)
    fixes = [None, _sjc_fix()]
    readings = [CompassReading(180.0, 180.0, 0, 0, 1, 1, 0, 0, 0.0)]

    class _ScriptGps:
        exhausted = False

        def read_fix(self, timeout: float = 2.0):
            del timeout
            return fixes.pop(0) if fixes else _sjc_fix()

    class _ScriptCompass:
        def read(self):
            assert not fixes, "compass was read before GPS had a fix"
            return readings.pop(0) if readings else None

    args = SimpleNamespace(interval=0.2)
    config = SimpleNamespace(compass_calibrated=True)
    assert cmd_orientate(args, config, (_ScriptGps(), _ScriptCompass(), _Closable())) == 0
    out = capsys.readouterr().out
    assert "Waiting for GPS fix..." in out
    assert "WWVB bearing 70.6 deg." in out
    assert "set 70.6 deg" in out
    assert "off n/a" not in out


def test_orientation_line_shows_the_set_heading_and_the_error():
    assert format_orientation(283.6, 283.6) == "pointing 283.6 deg  set 283.6 deg  off 0.0 deg"
    assert format_orientation(343.0, 283.6) == (
        "pointing 343.0 deg  set 283.6 deg  off 59.4 deg counterclockwise"
    )
    assert format_orientation(233.1, 278.1, 2) == "pointing 233.1 deg  set 233.1 deg  off 0.0 deg"
    assert format_orientation(323.1, 278.1, 1) == "pointing 323.1 deg  set 323.1 deg  off 0.0 deg"
    assert format_orientation(None, 10.0) == "pointing n/a deg  set n/a  off n/a"


def test_aim_only_uses_the_saved_offsets(tmp_path, monkeypatch):
    path = tmp_path / "gps_wwvb.toml"
    path.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(path)
    called = {}

    def sweep(*_args):
        called["sweep"] = True

    def verify(_args, _config, _gps, _compass, offset, scale):
        called["offset"] = offset
        called["scale"] = scale
        return 0

    monkeypatch.setattr("gps_wwvb.cli._sweep_compass", sweep)
    monkeypatch.setattr("gps_wwvb.cli._verify_antenna", verify)
    args = SimpleNamespace(aim_only=True)
    assert cmd_calibrate(args, config, (_Gps(None), object(), _Closable())) == 0
    assert "sweep" not in called
    assert called["offset"] == config.mag_offset
    assert called["scale"] == config.mag_scale


class _Closable:
    def close(self):
        """Nothing to release."""


def test_sweep_clears_calibration_and_aim_sets_it(tmp_path, monkeypatch):
    path = tmp_path / "gps_wwvb.toml"
    path.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(path)
    clock = {"n": 0}

    def monotonic():
        clock["n"] += 1
        return 0.0 if clock["n"] < 40 else 100.0

    monkeypatch.setattr("gps_wwvb.cli.time.monotonic", monotonic)
    monkeypatch.setattr("gps_wwvb.cli.time.sleep", lambda _seconds: None)
    args = SimpleNamespace(duration=30.0, sample_interval=0.0)
    fitted = _sweep_compass(args, config, _CubeCompass())
    assert fitted is not None
    assert load_config(path).compass_calibrated is False

    monkeypatch.undo()
    aim = SimpleNamespace(
        fix_timeout=1.0,
        tolerance=5.0,
        aim_samples=2,
        aim_interval=0.0,
        aim_timeout=2.0,
    )
    code = _verify_antenna(
        aim,
        config,
        _Gps(_sjc_fix()),
        _BroadsideCompass(),
        (0.0, 0.0, 0.0),
        (1.0, 1.0, 1.0),
    )
    assert code == 0
    assert load_config(path).compass_calibrated is True


def _empty_fix() -> GpsFix:
    return GpsFix(
        lat=0.0,
        lon=0.0,
        alt_m=None,
        utc=None,
        fix_type=0,
        valid_date=False,
        valid_time=False,
        fully_resolved=False,
        gnss_fix_ok=False,
        time_accuracy_ns=None,
        num_sv=0,
        received_at=0.0,
    )


def _sjc_fix() -> GpsFix:
    return GpsFix(
        lat=SJC[0],
        lon=SJC[1],
        alt_m=18.9,
        utc=datetime(2026, 11, 1, tzinfo=UTC),
        fix_type=3,
        valid_date=True,
        valid_time=True,
        fully_resolved=True,
        gnss_fix_ok=True,
        time_accuracy_ns=50,
        num_sv=8,
        received_at=0.0,
    )


class _CubeCompass:
    def __init__(self):
        self._n = 0

    def read_counts(self):
        corners = ((400, 0, 0), (-400, 0, 0), (0, 400, 0), (0, -400, 0), (0, 0, 400), (0, 0, -400))
        mx, my, mz = corners[self._n % len(corners)]
        self._n += 1
        return (0.0, 0.0, 1000.0, float(mx), float(my), float(mz))


class _BroadsideCompass:
    def read_counts(self):
        when = datetime(2026, 11, 1, tzinfo=UTC)
        declination = magnetic_declination_deg(SJC[0], SJC[1], 18.9, when)
        bearing = bearing_deg(SJC[0], SJC[1], WWVB_LAT, WWVB_LON)
        target = math.radians((bearing - declination) % 360.0)
        return (0.0, 0.0, 1000.0, math.cos(target) * 400.0, math.sin(target) * 400.0, 0.0)


class _Gps:
    def __init__(self, fix):
        self.fix = fix
        self.exhausted = False

    def read_fix(self, timeout: float = 2.0):
        del timeout
        return self.fix


class _Compass:
    def __init__(self, heading_mag: float = 70.6):
        self.heading_mag = heading_mag

    def read(self):
        return CompassReading(
            heading_mag=self.heading_mag,
            heading_true=self.heading_mag,
            ax=0,
            ay=0,
            az=1,
            mx=1,
            my=0,
            mz=0,
            received_at=0.0,
        )


class _Wwvb:
    exhausted = False


class _ExplodingWwvb:
    exhausted = False

    def receive(self):
        raise AssertionError("WWVB receive during a maintenance gap")


class _SlowWwvb:
    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self.exhausted = False

    def receive(self):
        time.sleep(self.delay_s)
        self.exhausted = True
        return _reading()


class _ScriptedWwvb:
    def __init__(self, readings):
        self._readings = list(readings)
        self.exhausted = not self._readings
        self.receive_calls = 0
        self.selected_antenna: int | None = None

    def set_antenna(self, antenna: int | None) -> None:
        self.selected_antenna = antenna

    def receive(self):
        self.receive_calls += 1
        if not self._readings:
            self.exhausted = True
            return None
        reading = self._readings.pop(0)
        if not self._readings:
            self.exhausted = True
        return reading


def _reading() -> WwvbReading:
    return WwvbReading(
        wwvb_time=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        system_time=datetime(2026, 11, 1, 12, 0, 0, 100000, tzinfo=UTC),
        antenna=2,
        dst=False,
        leap_second=None,
        tracking=False,
    )


def _observation(when, offset_ms, fix_age_s, day_night) -> Observation:
    return Observation(
        event="reception",
        system_time=when,
        offset_ms=offset_ms,
        antenna=1,
        distance_km=1501.66,
        bearing_deg=70.58,
        heading_true=70.58,
        declination_deg=12.7,
        antenna_turn_deg=0.0,
        fix_age_s=fix_age_s,
        day_night=day_night,
    )
