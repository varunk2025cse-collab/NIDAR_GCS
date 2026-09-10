"""Survivor geolocation and, more importantly, its error budget.

Two numbers are routinely conflated in detection pipelines and must not be
here:

* **Detection confidence** -- how sure the model is that it is looking at a
  person. A property of the classifier.
* **Geolocation accuracy** -- how far the reported ground position might be
  from the real one. A property of GPS, altitude, attitude and camera
  geometry.

A 0.98-confidence detection from 120 m with a 3 degree attitude error is still
only located to within roughly 10 m. Reporting that as a precise coordinate
would send a delivery aircraft to the wrong place with unearned certainty, so
this module always produces an explicit accuracy estimate, and says so when it
cannot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.core.geo import LatLon, destination_point, is_valid_coordinate

#: Assumed attitude error when the companion computer does not report one.
#: Deliberately pessimistic: a good gimbal does better, a fixed mount in wind
#: does worse, and over-estimating accuracy is the dangerous direction.
DEFAULT_ATTITUDE_ERROR_DEG = 3.0
#: Assumed horizontal GPS error when the aircraft does not report one.
DEFAULT_GPS_ACCURACY_M = 2.5
#: Assumed error in the height above the survivor (terrain, barometer drift).
DEFAULT_ALTITUDE_ERROR_M = 3.0


@dataclass(slots=True)
class CameraModel:
    """Minimal pinhole description of a real camera."""

    image_width: int
    image_height: int
    horizontal_fov_deg: float
    vertical_fov_deg: float | None = None

    def vertical_fov(self) -> float:
        if self.vertical_fov_deg is not None:
            return self.vertical_fov_deg
        # Derive from the horizontal FOV and the image aspect ratio.
        half_h = math.tan(math.radians(self.horizontal_fov_deg / 2))
        aspect = self.image_height / self.image_width
        return math.degrees(2 * math.atan(half_h * aspect))

    def angular_resolution_deg(self) -> float:
        return self.horizontal_fov_deg / max(1, self.image_width)


@dataclass(slots=True)
class GeolocationResult:
    latitude: float
    longitude: float
    #: 1-sigma-ish horizontal error estimate, in metres.
    accuracy_m: float
    method: str
    inputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy_m": round(self.accuracy_m, 2),
            "method": self.method,
            "inputs": self.inputs,
            "warnings": self.warnings,
        }


def project_pixel_to_ground(
    *,
    drone_latitude: float,
    drone_longitude: float,
    height_above_ground_m: float,
    heading_deg: float,
    pixel_x: int,
    pixel_y: int,
    camera: CameraModel,
    camera_pitch_deg: float = -90.0,
    roll_deg: float = 0.0,
    pitch_deg: float = 0.0,
) -> GeolocationResult:
    """Project an image pixel onto flat ground beneath the aircraft.

    Assumes locally flat terrain at a known height below the aircraft. Where
    that assumption is weak (sloping ground, unknown terrain) the error shows
    up in the accuracy estimate rather than being hidden.

    ``camera_pitch_deg`` is the camera depression angle: -90 is straight down.
    """
    warnings: list[str] = []
    if height_above_ground_m <= 0:
        raise ValueError("height_above_ground_m must be positive")

    # Angular offset of the pixel from the optical axis.
    half_w = camera.image_width / 2
    half_h = camera.image_height / 2
    x_norm = (pixel_x - half_w) / half_w
    y_norm = (pixel_y - half_h) / half_h

    ax = math.radians(camera.horizontal_fov_deg / 2) * x_norm
    ay = math.radians(camera.vertical_fov() / 2) * y_norm

    # Depression angle of the ray, including airframe pitch.
    depression = math.radians(camera_pitch_deg + pitch_deg) - ay
    if depression >= -0.05:
        # The ray is at or above the horizon: no ground intersection.
        raise ValueError(
            "Pixel ray does not intersect the ground plane "
            "(camera is looking at or above the horizon)"
        )

    # Distance along the ground from the nadir point.
    forward_m = height_above_ground_m / math.tan(abs(depression))
    lateral_m = height_above_ground_m * math.tan(ax) / max(
        math.sin(abs(depression)), 1e-6
    )

    ground_distance = math.hypot(forward_m, lateral_m)
    bearing_offset = math.degrees(math.atan2(lateral_m, forward_m))
    bearing = (heading_deg + roll_deg * 0.0 + bearing_offset) % 360.0

    point: LatLon = destination_point(
        drone_latitude, drone_longitude, bearing, ground_distance
    )

    if abs(roll_deg) > 10:
        warnings.append(
            f"Aircraft roll of {roll_deg:.1f} deg was not compensated; "
            "accuracy is degraded"
        )
    if ground_distance > height_above_ground_m * 3:
        warnings.append(
            "Detection is far off-nadir; small attitude errors dominate the position"
        )

    accuracy = estimate_accuracy_m(
        height_above_ground_m=height_above_ground_m,
        slant_distance_m=math.hypot(ground_distance, height_above_ground_m),
        camera=camera,
    )
    return GeolocationResult(
        latitude=point.latitude,
        longitude=point.longitude,
        accuracy_m=accuracy,
        method="PIXEL_PROJECTION",
        inputs={
            "height_above_ground_m": height_above_ground_m,
            "ground_distance_m": round(ground_distance, 2),
            "bearing_deg": round(bearing, 2),
            "pixel": [pixel_x, pixel_y],
            "camera_pitch_deg": camera_pitch_deg,
        },
        warnings=warnings,
    )


def estimate_accuracy_m(
    *,
    height_above_ground_m: float,
    slant_distance_m: float,
    camera: CameraModel | None = None,
    gps_accuracy_m: float | None = None,
    attitude_error_deg: float | None = None,
    altitude_error_m: float | None = None,
) -> float:
    """Combine the independent error sources into one horizontal estimate.

    Terms:

    * GPS error of the aircraft, which translates one-for-one to the ground.
    * Attitude error, which grows with slant range -- the dominant term at
      altitude.
    * Altitude error, which biases the projection distance.
    * Pixel quantisation, usually negligible but included for completeness.

    Summed in quadrature, treating them as independent.
    """
    gps = gps_accuracy_m if gps_accuracy_m is not None else DEFAULT_GPS_ACCURACY_M
    attitude = (
        attitude_error_deg if attitude_error_deg is not None else DEFAULT_ATTITUDE_ERROR_DEG
    )
    altitude_err = (
        altitude_error_m if altitude_error_m is not None else DEFAULT_ALTITUDE_ERROR_M
    )

    attitude_term = slant_distance_m * math.tan(math.radians(attitude))
    altitude_term = (
        altitude_err * (slant_distance_m / height_above_ground_m)
        if height_above_ground_m > 0
        else altitude_err
    )
    pixel_term = (
        slant_distance_m * math.tan(math.radians(camera.angular_resolution_deg()))
        if camera is not None
        else 0.0
    )

    return math.sqrt(
        gps**2 + attitude_term**2 + altitude_term**2 + pixel_term**2
    )


def accuracy_for_reported_position(
    *,
    drone_latitude: float | None,
    drone_longitude: float | None,
    survivor_latitude: float,
    survivor_longitude: float,
    height_above_ground_m: float | None,
    gps_accuracy_m: float | None = None,
    attitude_error_deg: float | None = None,
) -> tuple[float | None, list[str]]:
    """Estimate the accuracy of a position the companion computer computed.

    The companion did the projection; the GCS still needs a defensible error
    bar. When there is not enough information to produce one, this returns
    ``None`` -- the record then carries an explicitly unknown accuracy rather
    than a fabricated one.
    """
    warnings: list[str] = []
    if height_above_ground_m is None or height_above_ground_m <= 0:
        return None, ["No aircraft altitude was reported; accuracy cannot be estimated"]
    if drone_latitude is None or drone_longitude is None:
        return None, ["No aircraft position was reported; accuracy cannot be estimated"]
    if not is_valid_coordinate(survivor_latitude, survivor_longitude):
        return None, ["Reported survivor coordinate is not valid"]

    from app.core.geo import haversine_m

    ground_distance = haversine_m(
        drone_latitude, drone_longitude, survivor_latitude, survivor_longitude
    )
    slant = math.hypot(ground_distance, height_above_ground_m)

    if ground_distance > height_above_ground_m * 5:
        warnings.append(
            f"Reported survivor position is {ground_distance:.0f} m from the aircraft at "
            f"{height_above_ground_m:.0f} m altitude; the geometry is very oblique"
        )

    accuracy = estimate_accuracy_m(
        height_above_ground_m=height_above_ground_m,
        slant_distance_m=slant,
        gps_accuracy_m=gps_accuracy_m,
        attitude_error_deg=attitude_error_deg,
    )
    return accuracy, warnings


def fuse_positions(
    observations: list[tuple[float, float, float | None]]
) -> tuple[float, float, float | None]:
    """Inverse-variance weighted mean of several sightings of one survivor.

    Observations are ``(latitude, longitude, accuracy_m)``. A sighting with an
    unknown accuracy is given the weight of the worst known one rather than
    being dropped or trusted.
    """
    if not observations:
        raise ValueError("No observations to fuse")
    if len(observations) == 1:
        return observations[0]

    known = [a for _, _, a in observations if a is not None and a > 0]
    fallback = max(known) if known else 1.0

    total_weight = 0.0
    lat_sum = 0.0
    lon_sum = 0.0
    for latitude, longitude, accuracy in observations:
        sigma = accuracy if accuracy is not None and accuracy > 0 else fallback
        weight = 1.0 / (sigma**2)
        lat_sum += latitude * weight
        lon_sum += longitude * weight
        total_weight += weight

    fused_accuracy = math.sqrt(1.0 / total_weight) if total_weight > 0 else None
    return lat_sum / total_weight, lon_sum / total_weight, fused_accuracy
