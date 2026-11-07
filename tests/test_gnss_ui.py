"""Tests for the ncurses GNSS configuration UI and ubxtool parser."""

from __future__ import annotations

import curses
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gps_wwvb.cli import build_parser, cmd_gnss_ui, main, normalize_argv
from gps_wwvb.gnss_ui import (
    SimGnssBackend,
    UbxtoolBackend,
    _curses_loop,
    _format_nmea_coord,
    _parse_gga_summary,
    apply_key_action,
    build_menu_rows,
    parse_ubxtool_output,
    render_screen_lines,
    reset_row_default,
    run_gnss_ui,
)

SAMPLE_UBXTOOL_OUTPUT = """
$GNGGA,131325.00,4209.00518,N,07121.44273,W,1,12,0.64,65.0,M,-33.4,M,,*4C
UBX-MON-VER:
  swVersion ROM CORE 3.01 (107888)
  hwVersion 00080000
  extension FWVER=SPG 3.01
  extension PROTVER=18.00
  extension GPS;GLO;GAL;BDS
  extension SBAS;IMES;QZSS

UBX-CFG-GNSS:
 msgVer 0  numTrkChHw 32 numTrkChUse 32 numConfigBlocks 7
  gnssId 0 TrkCh  8 maxTrCh 16 reserved 0 Flags x01010001
   GPS L1C/A enabled
  gnssId 1 TrkCh  1 maxTrCh  3 reserved 0 Flags x01010001
   SBAS L1C/A enabled
  gnssId 2 TrkCh  4 maxTrCh  8 reserved 0 Flags x01010001
   Galileo E1 enabled
  gnssId 3 TrkCh  8 maxTrCh 16 reserved 0 Flags x01010000
   BeiDou B1I
  gnssId 4 TrkCh  0 maxTrCh  8 reserved 0 Flags x03010000
   IMES L1
  gnssId 5 TrkCh  0 maxTrCh  3 reserved 0 Flags x05010001
   QZSS L1C/A enabled
  gnssId 6 TrkCh  8 maxTrCh 14 reserved 0 Flags x01010001
   GLONASS L1 enabled

UBX-CFG-NAV5:
 mask 0xffff dynModel 2 fixmode 3 fixedAlt 0 FixedAltVar 10000

UBX-CFG-TP5:
  tpIdx 0 version 1 reserved1 0
  antCableDelay 50 rfGroupDelay 0 freqPeriod 1000000 freqPeriodLock 1000000
  pulseLenRatio 0 pulseLenRatioLock 50 userConfigDelay 0
  flags x77
"""


def test_parse_ubxtool_output_extracts_all_sections():
    state = parse_ubxtool_output(SAMPLE_UBXTOOL_OUTPUT)
    assert state.sw_version == "ROM CORE 3.01 (107888)"
    assert state.hw_version == "00080000"
    assert state.fw_version == "SPG 3.01"
    assert state.prot_ver == "18.00"
    assert state.supported_gnss == "GPS;GLO;GAL;BDS;SBAS;IMES;QZSS"
    assert "Fix Q=1" in state.fix_summary
    assert "42.15009" in state.fix_summary
    assert "-71.35738" in state.fix_summary
    assert "SVs 12" in state.fix_summary
    assert state.constellations["GPS"].enabled is True
    assert state.constellations["Galileo"].enabled is True
    assert state.constellations["BeiDou"].enabled is False
    assert state.constellations["GLONASS"].enabled is True
    assert state.dyn_model == 2
    assert state.dyn_model_label == "2 (Stationary)"
    assert state.ant_cable_delay_ns == 50
    assert state.pulse_len_lock_us == 50
    assert state.pps_mode == "dim"
    assert "Dimmed" in state.pps_mode_label


def test_parse_gga_edge_cases():
    assert _parse_gga_summary("$GNGGA,120000,invalid*00") is None
    assert _parse_gga_summary("$GNGGA,120000,,,,,0,00,,,M,,M,,*00") == "No Fix  SVs 00"
    assert _format_nmea_coord("", "N", 2) is None
    assert _format_nmea_coord("bad", "N", 2) is None
    assert _format_nmea_coord("3721.78", "S", 2) == "-37.36300"


def test_sim_backend_and_menu_actions():
    backend = SimGnssBackend()
    assert "Simulated" in backend.open() or "simulated" in backend.open()
    assert "simulated" in backend.refresh()

    rows = build_menu_rows(backend.state)
    keys = [row.key for row in rows]
    assert "gnss:Galileo" in keys
    assert "dyn_model" in keys
    assert "pps_mode" in keys
    assert "ant_delay" in keys
    assert "pi_act_led" in keys

    # Toggle Galileo
    assert backend.state.constellations["Galileo"].enabled is True
    apply_key_action(backend, "gnss:Galileo", 1)
    assert backend.state.constellations["Galileo"].enabled is False
    assert backend.state.dirty is True

    # Cycle dynamic model forward to Stationary (2)
    apply_key_action(backend, "dyn_model", 1)
    assert backend.state.dyn_model == 2

    # Handle unknown dyn_model fallback
    backend.state.dyn_model = 99
    assert "Custom" in backend.state.dyn_model_label
    apply_key_action(backend, "dyn_model", 1)
    assert backend.state.dyn_model == 2

    # Cycle PPS mode: normal -> dim -> off -> normal
    assert backend.state.pps_mode == "normal"
    apply_key_action(backend, "pps_mode", 1)
    assert backend.state.pps_mode == "dim"
    apply_key_action(backend, "pps_mode", 1)
    assert backend.state.pps_mode == "off"
    assert "Off" in backend.state.pps_mode_label
    apply_key_action(backend, "pps_mode", 1)
    assert backend.state.pps_mode == "normal"

    # Adjust antenna cable delay
    apply_key_action(backend, "ant_delay", 1)
    assert backend.state.ant_cable_delay_ns == 55
    apply_key_action(backend, "ant_delay", -2)
    assert backend.state.ant_cable_delay_ns == 45

    # Toggle Pi ACT LED
    assert backend.state.pi_act_led_on is True
    apply_key_action(backend, "pi_act_led", 1)
    assert backend.state.pi_act_led_on is False

    # Unknown row key
    assert apply_key_action(backend, "unknown", 1) == "No action for selected row."

    # Save and reset all
    backend.save()
    assert backend.state.dirty is False
    backend.reset_defaults()
    assert backend.state.dirty is True
    assert backend.state.dyn_model == 0
    backend.close()


def test_reset_row_default_resets_only_the_selected_field():
    backend = SimGnssBackend()
    backend.state.constellations["Galileo"].enabled = True
    reset_row_default(backend, "gnss:Galileo")
    assert backend.state.constellations["Galileo"].enabled is False
    backend.state.dyn_model = 2
    reset_row_default(backend, "dyn_model")
    assert backend.state.dyn_model == 0
    backend.set_pps_mode("dim")
    reset_row_default(backend, "pps_mode")
    assert backend.state.pps_mode == "normal"
    backend.set_ant_cable_delay(90)
    reset_row_default(backend, "ant_delay")
    assert backend.state.ant_cable_delay_ns == 50
    backend.set_pi_act_led(False)
    reset_row_default(backend, "pi_act_led")
    assert backend.state.pi_act_led_on is True
    assert reset_row_default(backend, "unknown") == "No default for selected row."


def test_render_screen_lines_includes_status_and_dirty_flag():
    backend = SimGnssBackend()
    backend.state.dirty = True
    backend.state.pi_act_led_on = None
    lines = render_screen_lines(backend.state, 0, "Ready", width=80)
    joined = "\n".join(lines)
    assert "[UNSAVED CHANGES]" in joined
    assert "> [Constellation] GPS" in joined
    assert "n/a (not on Raspberry Pi)" in joined
    assert "Status : Ready" in joined


class _FakeScreen:
    def __init__(self, keys: list[int]):
        self._keys = list(keys)
        self.drawn: list[tuple[int, int, str]] = []

    def keypad(self, _flag: bool) -> None:
        pass

    def erase(self) -> None:
        self.drawn.clear()

    def getmaxyx(self) -> tuple[int, int]:
        return (30, 100)

    def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
        self.drawn.append((y, x, text))

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return self._keys.pop(0) if self._keys else ord("q")


def test_curses_loop_processes_navigation_and_commands():
    backend = SimGnssBackend()
    keys = [
        curses.KEY_DOWN,  # move to Galileo
        ord("d"),  # reset selected field (Galileo -> default disabled)
        curses.KEY_UP,  # move back to GPS
        curses.KEY_LEFT,  # toggle GPS via left
        ord("r"),  # refresh
        ord("s"),  # save
        ord("D"),  # reset all defaults
        ord("q"),  # quit
    ]
    screen = _FakeScreen(keys)
    _curses_loop(screen, backend, "Connected")
    assert any("Reset receiver configuration" in item[2] for item in screen.drawn)


def test_curses_loop_handles_action_exception():
    backend = SimGnssBackend()

    def boom():
        raise RuntimeError("serial timeout")

    backend.refresh = boom  # type: ignore[method-assign]
    screen = _FakeScreen([ord("r"), ord("q")])
    _curses_loop(screen, backend, "Connected")
    assert any("Error: serial timeout" in item[2] for item in screen.drawn)


def test_run_gnss_ui_closes_backend_when_open_fails():
    backend = SimGnssBackend()
    closed = {"called": False}

    def fail_open():
        raise RuntimeError("no device")

    def mark_close():
        closed["called"] = True

    backend.open = fail_open  # type: ignore[method-assign]
    backend.close = mark_close  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="no device"):
        run_gnss_ui(backend)
    assert closed["called"] is True


def test_cli_gnss_ui_dispatches_in_simulate_mode(monkeypatch, caplog):
    monkeypatch.setattr(
        "gps_wwvb.gnss_ui.curses.wrapper",
        lambda func, backend, status: func(_FakeScreen([ord("q")]), backend, status),
    )
    assert normalize_argv(["--simulate", "gnss-ui"]) == ["gnss-ui", "--simulate"]
    parser = build_parser()
    args = parser.parse_args(
        ["gnss-ui", "--simulate", "--device", "/dev/ttyAMA0", "--speed", "9600"]
    )
    assert args.device == "/dev/ttyAMA0"
    assert args.speed == 9600
    with caplog.at_level("WARNING"):
        assert main(["--simulate", "gnss-ui"]) == 0
    assert "failed to close" not in caplog.text


def test_cli_gnss_ui_handles_failure_and_hardware_branch(monkeypatch, capsys):
    monkeypatch.setattr("gps_wwvb.cli.backend_name", lambda **_kwargs: "hardware")
    monkeypatch.setattr(
        "gps_wwvb.cli.run_gnss_ui",
        lambda _backend: (_ for _ in ()).throw(RuntimeError("ubxtool missing")),
    )
    args = SimpleNamespace(simulate=False, replay=None, device="/dev/ttyS0", speed=9600)
    assert cmd_gnss_ui(args, None, (None, None, None)) == 1
    assert "GNSS UI: ubxtool missing" in capsys.readouterr().err


def test_ubxtool_backend_commands_and_cleanup(tmp_path: Path, monkeypatch):
    act_led = tmp_path / "brightness"
    act_led.write_text("1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "sh -c" in joined:
            val = "0" if "echo 0" in joined else "1"
            act_led.write_text(f"{val}\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, SAMPLE_UBXTOOL_OUTPUT, "")

    monkeypatch.setattr("gps_wwvb.gnss_ui.shutil.which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr("gps_wwvb.gnss_ui.subprocess.run", fake_run)
    monkeypatch.setattr("gps_wwvb.gnss_ui.os.geteuid", lambda: 1000)

    backend = UbxtoolBackend(device="/dev/ttyS0", speed=9600, act_led_path=act_led)
    assert "Polled /dev/ttyS0" in backend.open()
    assert backend.state.pi_act_led_on is True

    backend.toggle_constellation("BeiDou")
    backend.set_dyn_model(2)
    backend.set_pps_mode("dim")
    backend.set_pps_mode("off")
    backend.set_ant_cable_delay(65)
    backend.toggle_pi_act_led()
    assert backend.state.pi_act_led_on is False
    backend.save()
    assert backend.state.dirty is False
    backend.reset_defaults()
    assert backend.state.dirty is True
    backend.close()

    assert any("stop" in cmd and "gpsd.service" in cmd for cmd in calls)
    assert any("start" in cmd and "gpsd.service" in cmd for cmd in calls)
    assert any("SAVE" in cmd for cmd in calls)
    assert any("RESET" in cmd for cmd in calls)


def test_ubxtool_backend_error_paths(tmp_path: Path, monkeypatch):
    missing_led = tmp_path / "missing"
    backend = UbxtoolBackend(act_led_path=missing_led)
    assert "not available" in backend.toggle_pi_act_led()

    monkeypatch.setattr("gps_wwvb.gnss_ui.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="ubxtool not found"):
        backend.open()

    monkeypatch.setattr("gps_wwvb.gnss_ui.shutil.which", lambda _name: "/usr/bin/ubxtool")
    monkeypatch.setattr("gps_wwvb.gnss_ui.os.geteuid", lambda: 0)

    def nak_run(cmd, **_kwargs):
        if "ubxtool" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "UBX-ACK-NAK", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("gps_wwvb.gnss_ui.subprocess.run", nak_run)
    assert "rejected" in backend.toggle_constellation("Galileo")
    assert "rejected" in backend.save()

    def fail_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "serial busy")

    monkeypatch.setattr("gps_wwvb.gnss_ui.subprocess.run", fail_run)
    with pytest.raises(RuntimeError, match="serial busy"):
        backend.refresh()
