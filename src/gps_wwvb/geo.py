"""Great-circle geometry and WWVB propagation.

Distance and bearing use the same spherical model as es100-wwvb: a 6371 km
Earth and ground-wave speed of 299,250 km/s. WWVB is the NIST site at
40.678062 N, 105.046688 W.
"""

from __future__ import annotations

import functools
import math
from datetime import UTC, datetime

from pygeomag import GeoMag

EARTH_RADIUS_KM = 6371.0
# Ground-wave speed used by es100-wwvb, from Smith-Rose (about 100 kHz).
RADIO_SPEED_KM_S = 299_250.0
WWVB_LAT = 40.678062
WWVB_LON = -105.046688
WWVB_FRAME_SECONDS = 134.0
CIVIL_TWILIGHT_DEG = -6.0
# The two ferrite rods sit at ±45 deg from +X. Turning the board 45 deg puts
# one rod broadside (90 deg) to WWVB and the other end-on in the null.
ANTENNA_ROD_OFFSET_DEG = 45.0


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    cosine = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(l1 - l2)
    cosine = max(-1.0, min(1.0, cosine))
    return EARTH_RADIUS_KM * math.acos(cosine)


def decimal_year(when: datetime) -> float:
    """Year plus the fraction of the year elapsed, the time argument used by WMM."""
    when = _as_utc(when)
    start = datetime(when.year, 1, 1, tzinfo=UTC)
    end = datetime(when.year + 1, 1, 1, tzinfo=UTC)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


def magnetic_declination_deg(
    lat: float,
    lon: float,
    alt_m: float | None,
    when: datetime,
) -> float:
    """East-positive declination in degrees from the World Magnetic Model.

    ``pygeomag`` ships the WMM-2025 coefficients, so this is a local calculation.
    Altitude is metres above sea level; the model takes kilometres.
    """
    alt_km = 0.0 if alt_m is None else alt_m / 1000.0
    result = _geomag().calculate(
        glat=lat,
        glon=lon,
        alt=alt_km,
        time=decimal_year(when),
    )
    return float(result.d)


def true_heading_deg(
    heading_mag: float | None,
    lat: float | None,
    lon: float | None,
    alt_m: float | None,
    when: datetime,
) -> tuple[float | None, float | None]:
    """Return ``(true heading, declination)``.

    Without a position the magnetic heading is returned and declination is None.
    """
    if heading_mag is None:
        return None, None
    if lat is None or lon is None:
        return heading_mag % 360.0, None
    declination = magnetic_declination_deg(lat, lon, alt_m, when)
    return (heading_mag + declination) % 360.0, declination


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees clockwise from true north."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    east = math.cos(p2) * math.sin(l2 - l1)
    north = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(l2 - l1)
    bearing = math.degrees(math.atan2(east, north))
    if bearing < 0.0:
        bearing += 360.0
    return bearing


def propagation(lat: float, lon: float) -> tuple[float, float, float]:
    """Distance (km), bearing (deg), and WWVB travel time (seconds)."""
    distance_km = great_circle_km(lat, lon, WWVB_LAT, WWVB_LON)
    if distance_km < 1e-9:
        return 0.0, 0.0, 0.0
    bearing = bearing_deg(lat, lon, WWVB_LAT, WWVB_LON)
    return distance_km, bearing, distance_km / RADIO_SPEED_KM_S


def signed_delta_deg(from_deg: float, to_deg: float) -> float:
    """Smallest signed turn from ``from_deg`` to ``to_deg``. Positive is clockwise."""
    return (to_deg - from_deg + 180.0) % 360.0 - 180.0


def antenna_is_aimed(heading_deg: float, bearing: float, tolerance_deg: float) -> bool:
    """True when +X is within ``tolerance_deg`` of the bearing to WWVB."""
    return abs(antenna_turn_deg(heading_deg, bearing)) <= tolerance_deg


def optimized_antenna(
    heading_deg: float,
    bearing: float,
    tolerance_deg: float,
) -> int | None:
    """Return 1 or 2 when one rod of the V is aimed broadside at WWVB.

    The ferrite rods sit at ±45 deg from +X. When +X is 45 deg counterclockwise
    of the bearing to WWVB (``turn`` near +45 deg clockwise), antenna 2 is
    broadside to the transmitter and antenna 1 points into the null. When +X is
    45 deg clockwise of the bearing (``turn`` near -45 deg counterclockwise),
    antenna 1 is broadside and antenna 2 points into the null. Returns None
    when neither single-rod orientation is within ``tolerance_deg``.
    """
    turn = antenna_turn_deg(heading_deg, bearing)
    if abs(turn - ANTENNA_ROD_OFFSET_DEG) <= tolerance_deg:
        return 2
    if abs(turn + ANTENNA_ROD_OFFSET_DEG) <= tolerance_deg:
        return 1
    return None


def antenna_target_deg(bearing: float, antenna: int | None = None) -> float:
    """Target +X true heading for V-split (None) or broadside on antenna 1 or 2."""
    if antenna is None:
        return bearing % 360.0
    if antenna == 1:
        return (bearing + ANTENNA_ROD_OFFSET_DEG) % 360.0
    if antenna == 2:
        return (bearing - ANTENNA_ROD_OFFSET_DEG) % 360.0
    raise ValueError(f"antenna must be None, 1, or 2, not {antenna!r}")


def antenna_turn_deg(
    heading_deg: float,
    bearing: float,
    antenna: int | None = None,
) -> float:
    """Signed degrees to turn +X onto the target heading for ``antenna``.

    When ``antenna`` is None, the target is the bearing to WWVB so the open end
    of the V (+X) splits the incoming signal across both rods. When ``antenna``
    is 1 or 2, the target is offset by ±45 deg so that rod is broadside to WWVB.
    Positive means turn the board clockwise.
    """
    return signed_delta_deg(heading_deg, antenna_target_deg(bearing, antenna))


def turn_direction(turn_deg: float) -> str:
    """``clockwise`` when the signed turn is positive."""
    return "clockwise" if turn_deg > 0 else "counterclockwise"


def format_turn(turn_deg: float | None) -> str:
    """``22.3 deg counterclockwise``, ``0.0 deg``, or ``n/a``."""
    if turn_deg is None:
        return "n/a"
    if abs(turn_deg) < 0.05:
        return "0.0 deg"
    return f"{abs(turn_deg):.1f} deg {turn_direction(turn_deg)}"


def wwvb_maintenance_gap(when: datetime, duration_s: float = 0.0) -> bool:
    """True while WWVB is off for its scheduled break, or within ``duration_s`` of it.

    The transmitter is off from minute 10 until minute 16, and from minute 40
    until minute 46, of each hour. Passing ``WWVB_FRAME_SECONDS`` also covers a
    reception that would run into that break before finishing.
    """
    pos = _half_hour_seconds(when)
    start = 600.0 - max(0.0, duration_s)
    return start <= pos < 960.0


def seconds_until_on_air(when: datetime, duration_s: float = 0.0) -> float:
    """Seconds until the current or impending maintenance gap ends. Zero when clear."""
    if not wwvb_maintenance_gap(when, duration_s=duration_s):
        return 0.0
    return max(0.0, 960.0 - _half_hour_seconds(when))


def _half_hour_seconds(when: datetime) -> float:
    when = _as_utc(when)
    return (when.minute % 30) * 60.0 + when.second + when.microsecond / 1_000_000.0


def solar_elevation_deg(lat_deg: float, lon_deg: float, when: datetime) -> float:
    """Approximate solar elevation in degrees (NOAA solar equations)."""
    when = _as_utc(when)
    day = when.timetuple().tm_yday
    frac_hour = when.hour + when.minute / 60 + when.second / 3600 + when.microsecond / 3_600_000_000
    gamma = 2 * math.pi / 365 * (day - 1 + (frac_hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    # Longitude is east-positive. timezone offset is 0 because ``when`` is UTC.
    true_solar_minutes = frac_hour * 60 + eqtime + 4 * lon_deg
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(lat_deg)
    sine = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    sine = max(-1.0, min(1.0, sine))
    return math.degrees(math.asin(sine))


def is_night(lat: float, lon: float, when: datetime) -> bool:
    """True when both the receiver and WWVB are in civil night (sun below -6 deg)."""
    when = _as_utc(when)
    here = solar_elevation_deg(lat, lon, when) < CIVIL_TWILIGHT_DEG
    there = solar_elevation_deg(WWVB_LAT, WWVB_LON, when) < CIVIL_TWILIGHT_DEG
    return here and there


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


@functools.cache
def _geomag() -> GeoMag:
    return GeoMag()
