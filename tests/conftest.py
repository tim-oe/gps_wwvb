"""Keep test runs from writing gps-wwvb.log into the working directory."""

import pytest


@pytest.fixture(autouse=True)
def _log_files_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("GPS_WWVB_LOG_PATH", str(tmp_path))
    monkeypatch.setenv("GPS_WWVB_LOG_NAME", "gps-wwvb-test")
