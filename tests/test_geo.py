"""Known distances, bearings, antenna aim, and WWVB's off-air windows."""

from datetime import UTC, datetime

import pytest

from gps_wwvb.geo import (
    WWVB_FRAME_SECONDS,
    WWVB_LAT,
    WWVB_LON,
    antenna_is_aimed,
    antenna_target_deg,
    antenna_turn_deg,
    bearing_deg,
    format_turn,
    is_night,
    magnetic_declination_deg,
    optimized_antenna,
    propagation,
    seconds_until_on_air,
    solar_elevation_deg,
    true_heading_deg,
    wwvb_maintenance_gap,
)

SJC = (37.363056, -121.928611)
DENVER = (39.739236, -104.990251)


def test_sjc_matches_the_published_wwvb_path():
    distance_km, bearing, delay_s = propagation(*SJC)
    assert distance_km == pytest.approx(1501.66, abs=0.01)
    assert bearing == pytest.approx(70.58, abs=0.01)
    assert delay_s * 1000 == pytest.approx(5.018, abs=0.001)


def test_denver_is_north_of_the_transmitter():
    distance_km, bearing, delay_s = propagation(*DENVER)
    assert distance_km == pytest.approx(104.50, abs=0.01)
    assert bearing == pytest.approx(357.39, abs=0.01)
    assert delay_s * 1000 == pytest.approx(0.349, abs=0.001)


def test_zero_distance_at_the_transmitter():
    assert propagation(WWVB_LAT, WWVB_LON) == (0.0, 0.0, 0.0)


def test_bearing_is_clockwise_from_north():
    assert bearing_deg(0.0, 0.0, 1.0, 0.0) == pytest.approx(0.0, abs=1e-6)
    assert bearing_deg(0.0, 0.0, 0.0, 1.0) == pytest.approx(90.0, abs=1e-6)
    assert bearing_deg(0.0, 0.0, -1.0, 0.0) == pytest.approx(180.0, abs=1e-6)


def test_declination_matches_the_wmm2025_seattle_sample():
    # pygeomag's documented sample: 2025.25 at the Space Needle, east positive.
    when = datetime(2025, 4, 2, 6, 0, tzinfo=UTC)
    assert magnetic_declination_deg(47.6205, -122.3493, 0.0, when) == pytest.approx(
        15.065629638512593,
        abs=1e-6,
    )


def test_true_heading_adds_declination_and_keeps_magnetic_without_a_fix():
    when = datetime(2026, 11, 1, tzinfo=UTC)
    true, declination = true_heading_deg(10.0, 47.6205, -122.3493, 0.0, when)
    assert declination is not None
    assert true == pytest.approx((10.0 + declination) % 360.0)
    assert true_heading_deg(10.0, None, None, None, when) == (10.0, None)
    assert true_heading_deg(None, 47.6205, -122.3493, 0.0, when) == (None, None)


def test_turn_direction_follows_the_sign():
    assert format_turn(22.3) == "22.3 deg clockwise"
    assert format_turn(-22.3) == "22.3 deg counterclockwise"
    assert format_turn(0.0) == "0.0 deg"
    assert format_turn(None) == "n/a"


def test_antenna_turn_points_the_open_end_at_wwvb():
    assert antenna_turn_deg(283.6, 283.6) == pytest.approx(0.0, abs=1e-9)
    assert antenna_turn_deg(343.0, 283.6) == pytest.approx(-59.4, abs=0.05)
    assert antenna_is_aimed(283.6, 283.6, 5.0)
    assert not antenna_is_aimed(343.0, 283.6, 5.0)


def test_optimized_antenna_identifies_broadside_rod():
    # Bearing 278.1 deg: +X at 231.8 deg (turn +46.3 deg clockwise) puts antenna 2 broadside.
    assert optimized_antenna(231.8, 278.1, 15.0) == 2
    # +X at 323.1 deg (turn -45.0 deg counterclockwise) puts antenna 1 broadside.
    assert optimized_antenna(323.1, 278.1, 15.0) == 1
    # Standard V-split and far off-aim return None.
    assert optimized_antenna(278.1, 278.1, 15.0) is None
    assert optimized_antenna(180.0, 278.1, 15.0) is None


def test_antenna_target_and_turn_for_selected_antenna():
    assert antenna_target_deg(278.1, None) == pytest.approx(278.1)
    assert antenna_target_deg(278.1, 1) == pytest.approx(323.1)
    assert antenna_target_deg(278.1, 2) == pytest.approx(233.1)
    assert antenna_turn_deg(323.1, 278.1, 1) == pytest.approx(0.0, abs=1e-9)
    assert antenna_turn_deg(233.1, 278.1, 2) == pytest.approx(0.0, abs=1e-9)
    assert antenna_turn_deg(278.1, 278.1, 1) == pytest.approx(45.0)
    assert antenna_turn_deg(278.1, 278.1, 2) == pytest.approx(-45.0)
    with pytest.raises(ValueError, match="antenna must be None, 1, or 2"):
        antenna_target_deg(278.1, 3)


def test_maintenance_windows_are_the_scheduled_breaks():
    def at(minute, second=0):
        return datetime(2026, 11, 1, 12, minute, second, tzinfo=UTC)

    assert not wwvb_maintenance_gap(at(9, 59))
    assert wwvb_maintenance_gap(at(10, 0))
    assert wwvb_maintenance_gap(at(15, 59))
    assert not wwvb_maintenance_gap(at(16, 0))
    assert not wwvb_maintenance_gap(at(39, 59))
    assert wwvb_maintenance_gap(at(40, 0))
    assert wwvb_maintenance_gap(at(45, 59))
    assert not wwvb_maintenance_gap(at(46, 0))
    assert seconds_until_on_air(at(12, 30)) == pytest.approx(210.0)
    assert seconds_until_on_air(at(20, 0)) == 0.0
    # A 134 s frame starting after :07:46 or :37:46 would run into the gap.
    assert not wwvb_maintenance_gap(at(7, 45), WWVB_FRAME_SECONDS)
    assert wwvb_maintenance_gap(at(7, 46), WWVB_FRAME_SECONDS)
    assert wwvb_maintenance_gap(at(8, 30), WWVB_FRAME_SECONDS)
    assert seconds_until_on_air(at(8, 30), WWVB_FRAME_SECONDS) == pytest.approx(450.0)
    assert not wwvb_maintenance_gap(at(37, 45), WWVB_FRAME_SECONDS)
    assert wwvb_maintenance_gap(at(38, 20), WWVB_FRAME_SECONDS)
    assert seconds_until_on_air(at(38, 20), WWVB_FRAME_SECONDS) == pytest.approx(460.0)


def test_day_and_night_follow_the_sun():
    noon = datetime(2026, 6, 21, 19, 0, tzinfo=UTC)
    night = datetime(2026, 6, 21, 7, 0, tzinfo=UTC)
    assert solar_elevation_deg(40.0, -105.0, noon) > 40
    assert solar_elevation_deg(40.0, -105.0, night) < -6
    assert not is_night(40.0, -105.0, noon)
    assert is_night(40.0, -105.0, night)
    equator_noon = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    assert solar_elevation_deg(0.0, 0.0, equator_noon) > 50
