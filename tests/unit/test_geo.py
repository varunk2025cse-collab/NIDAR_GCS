"""Geodesy helpers used on the per-telemetry-sample hot path."""

from __future__ import annotations

import pytest

from app.core.geo import (
    bearing_deg,
    destination_point,
    distance_to_polygon_boundary_m,
    haversine_m,
    is_valid_coordinate,
    point_in_polygon,
    polygon_area_m2,
    polygon_bounds,
    polygon_centroid,
)

#: A 1 km square around the example mission site.
SQUARE = [
    (11.2300, 77.1200),
    (11.2300, 77.1292),
    (11.2390, 77.1292),
    (11.2390, 77.1200),
]


def test_valid_coordinates() -> None:
    assert is_valid_coordinate(11.2345, 77.1234) is True
    assert is_valid_coordinate(-89.9, 179.9) is True


def test_null_island_is_not_a_valid_position() -> None:
    """Exactly (0, 0) is an uninitialised GPS reading far more often than it
    is a real location, and treating it as real puts a marker in the Gulf of
    Guinea."""
    assert is_valid_coordinate(0.0, 0.0) is False


def test_out_of_range_and_non_finite_coordinates_are_invalid() -> None:
    assert is_valid_coordinate(91.0, 0.0) is False
    assert is_valid_coordinate(0.0, 181.0) is False
    assert is_valid_coordinate(float("nan"), 77.0) is False
    assert is_valid_coordinate(float("inf"), 77.0) is False


def test_haversine_known_distance() -> None:
    # One degree of latitude is about 111.2 km.
    assert haversine_m(11.0, 77.0, 12.0, 77.0) == pytest.approx(111_195, rel=0.01)
    assert haversine_m(11.0, 77.0, 11.0, 77.0) == 0.0


def test_haversine_is_symmetric() -> None:
    forward = haversine_m(11.2345, 77.1234, 11.2400, 77.1300)
    backward = haversine_m(11.2400, 77.1300, 11.2345, 77.1234)
    assert forward == pytest.approx(backward)


def test_bearing_cardinal_directions() -> None:
    assert bearing_deg(11.0, 77.0, 12.0, 77.0) == pytest.approx(0.0, abs=0.1)
    assert bearing_deg(11.0, 77.0, 11.0, 78.0) == pytest.approx(90.0, abs=0.2)
    assert bearing_deg(12.0, 77.0, 11.0, 77.0) == pytest.approx(180.0, abs=0.1)


def test_destination_point_round_trips() -> None:
    start = (11.2345, 77.1234)
    result = destination_point(*start, 45.0, 500.0)
    assert haversine_m(*start, result.latitude, result.longitude) == pytest.approx(
        500.0, rel=0.001
    )
    assert bearing_deg(*start, result.latitude, result.longitude) == pytest.approx(
        45.0, abs=0.1
    )


def test_point_in_polygon() -> None:
    assert point_in_polygon(11.2345, 77.1245, SQUARE) is True
    assert point_in_polygon(11.2500, 77.1245, SQUARE) is False
    assert point_in_polygon(11.2345, 77.1400, SQUARE) is False


def test_degenerate_polygon_contains_nothing() -> None:
    assert point_in_polygon(11.2345, 77.1245, [(11.0, 77.0), (11.1, 77.1)]) is False
    assert point_in_polygon(11.2345, 77.1245, []) is False


def test_distance_to_boundary_is_non_negative_inside_and_out() -> None:
    inside = distance_to_polygon_boundary_m(11.2345, 77.1245, SQUARE)
    outside = distance_to_polygon_boundary_m(11.2500, 77.1245, SQUARE)
    assert inside > 0
    assert outside > 0
    # A point near the edge is closer to it than one in the middle.
    near_edge = distance_to_polygon_boundary_m(11.2302, 77.1245, SQUARE)
    assert near_edge < inside


def test_boundary_distance_supports_geofence_margin_logic() -> None:
    """Containment plus distance is what produces NEAR_BOUNDARY."""
    latitude, longitude = 11.2303, 77.1245
    assert point_in_polygon(latitude, longitude, SQUARE) is True
    assert distance_to_polygon_boundary_m(latitude, longitude, SQUARE) < 60


def test_polygon_area_of_a_known_square() -> None:
    area = polygon_area_m2(SQUARE)
    # ~1 km x ~1 km.
    assert area == pytest.approx(1_000_000, rel=0.05)


def test_empty_polygon_has_no_area() -> None:
    assert polygon_area_m2([]) == 0.0
    assert polygon_area_m2([(11.0, 77.0), (11.1, 77.1)]) == 0.0


def test_centroid_and_bounds() -> None:
    centroid = polygon_centroid(SQUARE)
    assert centroid is not None
    assert point_in_polygon(centroid.latitude, centroid.longitude, SQUARE) is True

    bounds = polygon_bounds(SQUARE)
    assert bounds == (11.2300, 77.1200, 11.2390, 77.1292)
    assert polygon_bounds([]) is None
    assert polygon_centroid([]) is None
