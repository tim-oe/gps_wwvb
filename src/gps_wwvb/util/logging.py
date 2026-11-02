"""Python logging configured from the packaged ``logging.yml``.

The file matches WeatherWatch: a console handler and a 2 MB rotating file,
with the level and path taken from the environment. The packaged file is the
only default. ``dictConfig`` imports the handler classes named in the file, so
a ``logging.yml`` in the working directory is never read implicitly.
"""

from __future__ import annotations

import logging
import logging.config
import logging.handlers
import os
from pathlib import Path

from pyaml_env import parse_config

ENVAR_NO_CONSOLE = "GPS_WWVB_NO_CONSOLE"
ENVAR_LOG_CONFIG = "GPS_WWVB_LOG_CONFIG"

logger = logging.getLogger(__name__)


def packaged_logging_config() -> Path:
    """``logging.yml`` shipped next to the ``gps_wwvb`` package."""
    return Path(__file__).resolve().parent.parent / "logging.yml"


def init_logging(path: Path | None = None) -> Path:
    """Load the YAML logging config. ``GPS_WWVB_NO_CONSOLE=1`` keeps the file only."""
    config_path = path or find_logging_config()
    loaded = parse_config(str(config_path))
    logging.config.dictConfig(loaded)
    if os.getenv(ENVAR_NO_CONSOLE, "0") == "1":
        logging.root.handlers = [
            handler
            for handler in logging.root.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
    logging.info("loaded logging config file %s", config_path)
    return config_path


def find_logging_config() -> Path:
    """``GPS_WWVB_LOG_CONFIG`` when it is set, otherwise the packaged file."""
    override = os.getenv(ENVAR_LOG_CONFIG)
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"logging config not found: {path}")
        return path
    path = packaged_logging_config()
    if not path.is_file():
        raise FileNotFoundError(f"logging config not found: {path}")
    return path
