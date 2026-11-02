"""The packaged logging.yml follows the WeatherWatch console and rotating-file setup."""

import logging
import logging.handlers

from gps_wwvb.util.logging import find_logging_config, init_logging


def _is_console(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.StreamHandler) and not isinstance(
        handler, logging.handlers.RotatingFileHandler
    )


def test_logging_config_writes_console_and_a_rotating_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GPS_WWVB_LOG_LEVEL", "INFO")
    monkeypatch.setenv("GPS_WWVB_LOG_PATH", str(tmp_path))
    monkeypatch.setenv("GPS_WWVB_LOG_NAME", "gps-wwvb")
    monkeypatch.delenv("GPS_WWVB_NO_CONSOLE", raising=False)
    init_logging(find_logging_config())
    logging.getLogger("gps_wwvb.test").info("reception line")
    text = (tmp_path / "gps-wwvb.log").read_text(encoding="utf-8")
    handlers = logging.root.handlers
    assert "|INFO- reception line" in text
    assert "reception line" in capsys.readouterr().out
    assert any(isinstance(handler, logging.handlers.RotatingFileHandler) for handler in handlers)
    assert any(_is_console(handler) for handler in handlers)


def test_no_console_keeps_only_the_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GPS_WWVB_LOG_LEVEL", "INFO")
    monkeypatch.setenv("GPS_WWVB_LOG_PATH", str(tmp_path))
    monkeypatch.setenv("GPS_WWVB_LOG_NAME", "gps-wwvb")
    monkeypatch.setenv("GPS_WWVB_NO_CONSOLE", "1")
    init_logging(find_logging_config())
    handlers = logging.root.handlers
    assert handlers
    assert all(isinstance(handler, logging.handlers.RotatingFileHandler) for handler in handlers)
    monkeypatch.delenv("GPS_WWVB_NO_CONSOLE")
    init_logging(find_logging_config())


def test_gps_debug_follows_the_log_level_and_es100_info_is_dropped(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GPS_WWVB_LOG_LEVEL", "INFO")
    monkeypatch.setenv("GPS_WWVB_LOG_PATH", str(tmp_path))
    monkeypatch.setenv("GPS_WWVB_LOG_NAME", "gps-wwvb")
    monkeypatch.delenv("GPS_WWVB_NO_CONSOLE", raising=False)
    init_logging(find_logging_config())
    logging.getLogger("gps_wwvb.devices.gps_gpsd").info("GPS fix lat 1")
    logging.getLogger("gps_wwvb.devices.gps_gpsd").debug("GPS (gpsd) fix deadbeef")
    logging.getLogger("ES100").info("DST info: DST  ENDS-TODAY")
    logging.getLogger("ES100").warning("ES100 failed")
    text = (tmp_path / "gps-wwvb.log").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "GPS fix lat 1" in text
    assert "GPS (gpsd) fix deadbeef" not in text
    assert "DST info" not in text
    assert "ES100 failed" in text
    assert "GPS (gpsd) fix deadbeef" not in out
    assert "DST info" not in out

    monkeypatch.setenv("GPS_WWVB_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GPS_WWVB_LOG_NAME", "gps-wwvb-debug")
    init_logging(find_logging_config())
    logging.getLogger("gps_wwvb.devices.gps_gpsd").debug("GPS (gpsd) fix cafebabe")
    debug_text = (tmp_path / "gps-wwvb-debug.log").read_text(encoding="utf-8")
    assert "GPS (gpsd) fix cafebabe" in debug_text


def test_logging_config_does_not_come_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("GPS_WWVB_LOG_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    path = find_logging_config()
    assert path.name == "logging.yml"
    assert path.is_file()
    assert tmp_path not in path.parents
