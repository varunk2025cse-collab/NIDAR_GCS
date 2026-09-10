"""Survivor geolocation and its error budget.

The property these protect is the separation of two numbers that are easy to
conflate: how sure the model is that it saw a person (confidence), and how
well we know where that person is (accuracy). A 0.98-confidence detection from
120 m is not a 0.98-accurate position, and a delivery aircraft is dispatched
against the second number, not the first.
"""

from __future__ import annotations

import math

import pytest

from app.ai.geolocation import (
    CameraModel,
    accuracy_for_reported_position,
    estimate_accuracy_m,
    fuse_positions,
    project_pixel_to_ground,
)
from app.core.geo import haversine_m


@pytest.fixture
def camera() -> CameraModel:
    return CameraModel(image_width=1280, image_height=720, horizontal_fov_deg=62.2)


def test_vertical_fov_is_derived_from_the_aspect_ratio(camera: CameraModel) -> None:
    vertical = camera.vertical_fov()
    assert 0 < vertical < camera.horizontal_fov_deg
    assert vertical == pytest.approx(37.5, abs=2.0)


def test_centre_pixel_projects_to_the_nadir_point(camera: CameraModel) -> None:
    """Straight down, the middle of the frame is directly beneath the aircraft."""
    result = project_pixel_to_ground(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        height_above_ground_m=100.0,
        heading_deg=0.0,
        pixel_x=camera.image_width // 2,
        pixel_y=camera.image_height // 2,
        camera=camera,
        camera_pitch_deg=-90.0,
    )
    offset = haversine_m(11.2345, 77.1234, result.latitude, result.longitude)
    assert offset < 1.0
    assert result.method == "PIXEL_PROJECTION"


def test_off_centre_pixel_projects_away_from_the_aircraft(camera: CameraModel) -> None:
    result = project_pixel_to_ground(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        height_above_ground_m=100.0,
        heading_deg=0.0,
        pixel_x=camera.image_width // 2,
        pixel_y=int(camera.image_height * 0.9),
        camera=camera,
        camera_pitch_deg=-90.0,
    )
    offset = haversine_m(11.2345, 77.1234, result.latitude, result.longitude)
    assert offset > 10.0


def test_ray_above_the_horizon_is_rejected(camera: CameraModel) -> None:
    """A forward-looking camera can produce a pixel whose ray never meets the
    ground. Returning a coordinate for it would be inventing a position."""
    with pytest.raises(ValueError, match="ground plane"):
        project_pixel_to_ground(
            drone_latitude=11.2345,
            drone_longitude=77.1234,
            height_above_ground_m=100.0,
            heading_deg=0.0,
            pixel_x=camera.image_width // 2,
            pixel_y=0,
            camera=camera,
            camera_pitch_deg=-5.0,
        )


def test_accuracy_degrades_with_altitude() -> None:
    """Attitude error dominates at range: the higher you are, the worse a
    given pointing error puts you on the ground."""
    low = estimate_accuracy_m(height_above_ground_m=30.0, slant_distance_m=30.0)
    high = estimate_accuracy_m(height_above_ground_m=120.0, slant_distance_m=120.0)
    assert high > low
    # 120 m with a 3-degree attitude error is roughly 6 m of ground error,
    # combined in quadrature with GPS and altitude terms.
    assert 5.0 < high < 20.0


def test_accuracy_degrades_with_oblique_geometry() -> None:
    nadir = estimate_accuracy_m(height_above_ground_m=100.0, slant_distance_m=100.0)
    oblique = estimate_accuracy_m(height_above_ground_m=100.0, slant_distance_m=400.0)
    assert oblique > nadir * 2


def test_better_gps_improves_accuracy() -> None:
    poor = estimate_accuracy_m(
        height_above_ground_m=100.0, slant_distance_m=100.0, gps_accuracy_m=8.0
    )
    good = estimate_accuracy_m(
        height_above_ground_m=100.0, slant_distance_m=100.0, gps_accuracy_m=0.5
    )
    assert poor > good


def test_accuracy_is_never_claimed_without_the_inputs() -> None:
    """No altitude means no defensible error bar, so none is produced."""
    accuracy, warnings = accuracy_for_reported_position(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        survivor_latitude=11.2350,
        survivor_longitude=77.1240,
        height_above_ground_m=None,
    )
    assert accuracy is None
    assert warnings and "altitude" in warnings[0].lower()

    accuracy, warnings = accuracy_for_reported_position(
        drone_latitude=None,
        drone_longitude=None,
        survivor_latitude=11.2350,
        survivor_longitude=77.1240,
        height_above_ground_m=100.0,
    )
    assert accuracy is None


def test_reported_position_gets_an_independent_error_bar() -> None:
    accuracy, warnings = accuracy_for_reported_position(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        survivor_latitude=11.2350,
        survivor_longitude=77.1234,
        height_above_ground_m=100.0,
        gps_accuracy_m=2.0,
    )
    assert accuracy is not None
    assert accuracy > 0
    assert not warnings


def test_very_oblique_reported_position_is_flagged() -> None:
    """A survivor reported 900 m away from an aircraft at 100 m is geometry
    worth warning about, even if the companion is confident."""
    accuracy, warnings = accuracy_for_reported_position(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        survivor_latitude=11.2345,
        survivor_longitude=77.1316,
        height_above_ground_m=100.0,
    )
    assert accuracy is not None
    assert warnings
    assert "oblique" in " ".join(warnings).lower()


def test_invalid_survivor_coordinate_yields_no_accuracy() -> None:
    accuracy, warnings = accuracy_for_reported_position(
        drone_latitude=11.2345,
        drone_longitude=77.1234,
        survivor_latitude=0.0,
        survivor_longitude=0.0,
        height_above_ground_m=100.0,
    )
    assert accuracy is None


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------
def test_fusing_two_sightings_improves_the_estimate() -> None:
    latitude, longitude, accuracy = fuse_positions(
        [(11.2345, 77.1234, 10.0), (11.2346, 77.1235, 10.0)]
    )
    assert 11.2345 <= latitude <= 11.2346
    assert accuracy is not None
    # Two independent 10 m fixes give about 7 m.
    assert accuracy == pytest.approx(10.0 / math.sqrt(2), abs=0.5)


def test_fusion_weights_the_more_accurate_sighting() -> None:
    latitude, _, _ = fuse_positions(
        [(11.2340, 77.1234, 30.0), (11.2350, 77.1234, 3.0)]
    )
    # The 3 m fix should dominate.
    assert latitude == pytest.approx(11.2350, abs=0.0002)


def test_unknown_accuracy_is_weighted_as_the_worst_known() -> None:
    """An unknown-accuracy sighting is neither discarded nor trusted."""
    _, _, accuracy = fuse_positions([(11.2345, 77.1234, 5.0), (11.2346, 77.1235, None)])
    assert accuracy is not None
    assert accuracy < 5.0


def test_single_observation_passes_through_unchanged() -> None:
    assert fuse_positions([(11.2345, 77.1234, 7.0)]) == (11.2345, 77.1234, 7.0)


def test_fusing_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        fuse_positions([])
