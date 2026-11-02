"""Config errors, the compass-section writer, and the system state file."""

from pathlib import Path

import pytest

from gps_wwvb import config as config_module
from gps_wwvb.cli import main
from gps_wwvb.config import (
    ConfigError,
    load_config,
    set_compass_calibrated,
    update_compass_calibration,
)

ROOT = Path(__file__).resolve().parents[1]


def test_working_directory_config_is_not_implicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gps_wwvb.toml").write_text("broken = [\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "search_paths", lambda: [])
    config = load_config()
    assert config.path is None
    assert config.gpsd_host == "127.0.0.1"
    assert config.gpsd_port == 2947


def test_invalid_toml_and_values_are_config_errors(tmp_path, capsys):
    path = tmp_path / "bad.toml"
    path.write_text("[compass\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
    assert main(["--config", str(path), "status"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err

    path.write_text('[compass]\nmag_offset_x = "abc"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(path)


def test_calibration_edit_stays_inside_the_compass_table(tmp_path):
    path = tmp_path / "gps_wwvb.toml"
    path.write_text(
        "[other]\ncalibrated = false\n\n[compass]\ncalibrated = false\n\n[es100]\nirq = 11\n",
        encoding="utf-8",
    )
    update_compass_calibration(path, (1.5, -2.0, 3.25), (1.1, 0.9, 1.0))
    text = path.read_text(encoding="utf-8")
    assert "[other]\ncalibrated = false" in text
    compass = text.split("[compass]", 1)[1].split("[es100]", 1)[0]
    assert "calibrated = false" in compass
    assert "mag_offset_x = 1.500000" in compass
    assert text.index("mag_offset_x") < text.index("[es100]")
    set_compass_calibrated(path, True)
    text = path.read_text(encoding="utf-8")
    assert "[other]\ncalibrated = false" in text
    compass = text.split("[es100]", 1)[0]
    assert "calibrated = true" in compass


def test_system_config_stores_calibration_in_the_state_file(tmp_path, monkeypatch):
    etc = tmp_path / "etc.toml"
    state = tmp_path / "state" / "calibration.toml"
    etc.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", etc)
    monkeypatch.setattr(config_module, "SYSTEM_STATE", state)
    written = update_compass_calibration(etc, (4.0, 5.0, 6.0), (1.0, 1.0, 1.0))
    assert written == state
    assert "mag_offset_x = 4.000000" not in etc.read_text(encoding="utf-8")
    loaded = load_config(etc)
    assert loaded.mag_offset == pytest.approx((4.0, 5.0, 6.0))
    assert loaded.compass_calibrated is False
    set_compass_calibrated(etc, True)
    assert load_config(etc).compass_calibrated is True


def test_state_path_can_be_overridden(tmp_path, monkeypatch):
    etc = tmp_path / "etc.toml"
    custom = tmp_path / "custom.toml"
    etc.write_text((ROOT / "gps_wwvb.toml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", etc)
    monkeypatch.setenv("GPS_WWVB_STATE", str(custom))
    assert set_compass_calibrated(etc, True) == custom
    assert load_config(etc).compass_calibrated is True
