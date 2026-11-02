"""GPS, WWVB, and compass monitor."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gps-wwvb")
except PackageNotFoundError:  # source tree that has not been installed
    __version__ = "0.1.0"
