"""Validation of incoming detection events.

An endpoint that creates survivors is an endpoint that redirects aircraft.
Every detection is checked before it is allowed to become evidence:

* the source is an authenticated companion computer, bound to one drone_id;
* the drone is one we know and is actually flying this mission;
* the mission is active;
* the coordinate is real, and inside the mission geofence;
* the timestamp is recent and not from the future;
* the confidence is in range and above the configured floor;
* the detection id has not been seen before.

Anything that fails is recorded as a rejected detection with the reason --
kept, not discarded, so a misconfigured perception stack is visible rather
than silently dropping survivors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.enums import MISSION_ACTIVE_STATES, GeofenceStatus, MissionState
from app.core.geo import is_valid_coordinate
from app.core.logging import get_logger
from app.drone.state import DroneState
from app.services.geofence_service import GeofenceService

logger = get_logger(__name__)


@dataclass(slots=True)
class ValidationOutcome:
    accepted: bool
    reason: str | None = None
    code: str | None = None
    warnings: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "warnings": self.warnings,
            "context": self.context,
        }


class DetectionValidator:
    def __init__(self, settings: Settings, geofence: GeofenceService) -> None:
        self._settings = settings
        self._geofence = geofence

    def validate(
        self,
        *,
        drone_state: DroneState | None,
        mission_id: str | None,
        mission_state: MissionState | None,
        latitude: float,
        longitude: float,
        confidence: float,
        detected_at: datetime,
        estimated_accuracy_m: float | None = None,
    ) -> ValidationOutcome:
        warnings: list[str] = []
        now = datetime.now(UTC)
        config = self._settings.survivor

        # -- known aircraft -------------------------------------------------
        if drone_state is None:
            return ValidationOutcome(
                False, "Detection came from an unknown aircraft", "UNKNOWN_DRONE"
            )

        # -- mission active --------------------------------------------------
        if mission_state is None or mission_state not in MISSION_ACTIVE_STATES:
            return ValidationOutcome(
                False,
                f"No active mission for {drone_state.drone_id} "
                f"(mission state: {mission_state})",
                "MISSION_NOT_ACTIVE",
                context={"mission_state": str(mission_state) if mission_state else None},
            )

        # -- coordinate validity ---------------------------------------------
        if not is_valid_coordinate(latitude, longitude):
            return ValidationOutcome(
                False,
                f"Coordinate ({latitude}, {longitude}) is not a valid position",
                "INVALID_COORDINATE",
                context={"latitude": latitude, "longitude": longitude},
            )

        # -- confidence range -------------------------------------------------
        if not (0.0 <= confidence <= 1.0):
            return ValidationOutcome(
                False,
                f"Confidence {confidence} is outside the range 0.0-1.0",
                "INVALID_CONFIDENCE",
                context={"confidence": confidence},
            )
        if confidence < config.min_confidence:
            return ValidationOutcome(
                False,
                f"Confidence {confidence:.2f} is below the {config.min_confidence:.2f} floor",
                "CONFIDENCE_TOO_LOW",
                context={"confidence": confidence, "minimum": config.min_confidence},
            )

        # -- timestamp sanity --------------------------------------------------
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=UTC)
        age = (now - detected_at).total_seconds()
        if age < -30:
            return ValidationOutcome(
                False,
                f"Detection timestamp is {abs(age):.0f}s in the future; "
                "check the companion computer clock",
                "TIMESTAMP_IN_FUTURE",
                context={"detected_at": detected_at.isoformat(), "age_s": age},
            )
        if age > config.max_detection_age_s:
            return ValidationOutcome(
                False,
                f"Detection is {age:.0f}s old, beyond the "
                f"{config.max_detection_age_s:.0f}s limit",
                "DETECTION_TOO_OLD",
                context={"age_s": round(age, 1)},
            )
        if age > config.max_detection_age_s / 2:
            warnings.append(f"Detection arrived {age:.0f}s after it was made")

        # -- geofence relationship ----------------------------------------------
        evaluation = self._geofence.evaluate(mission_id, latitude, longitude)
        if evaluation.status is GeofenceStatus.BREACHED:
            return ValidationOutcome(
                False,
                "Detection lies outside the mission geofence",
                "OUTSIDE_GEOFENCE",
                context={
                    "breached_fences": evaluation.breached_fences,
                    "latitude": latitude,
                    "longitude": longitude,
                },
            )
        if evaluation.status is GeofenceStatus.UNKNOWN:
            warnings.append("No geofence configured; detection location was not bounded")

        # -- plausibility against the aircraft position ---------------------------
        position = drone_state.position.value
        if position is not None:
            from app.core.geo import haversine_m

            distance = haversine_m(
                position.latitude, position.longitude, latitude, longitude
            )
            altitude = position.relative_altitude_m or 0.0
            # A downward-looking camera cannot see much beyond a few times its
            # own altitude. Well past that, the reported position is suspect.
            limit = max(200.0, altitude * 6)
            if distance > limit:
                return ValidationOutcome(
                    False,
                    f"Detection is {distance:.0f} m from {drone_state.drone_id}, which was "
                    f"at {altitude:.0f} m; the geometry is not plausible",
                    "IMPLAUSIBLE_GEOMETRY",
                    context={"distance_m": round(distance, 1), "altitude_m": altitude},
                )
            if distance > limit / 2:
                warnings.append(
                    f"Detection is {distance:.0f} m from the aircraft; accuracy is degraded"
                )

        # -- accuracy sanity -------------------------------------------------------
        if estimated_accuracy_m is not None:
            if estimated_accuracy_m < 0:
                return ValidationOutcome(
                    False, "Estimated accuracy cannot be negative", "INVALID_ACCURACY"
                )
            if estimated_accuracy_m > config.max_auto_confirm_accuracy_m:
                warnings.append(
                    f"Position accuracy {estimated_accuracy_m:.0f} m exceeds the "
                    f"{config.max_auto_confirm_accuracy_m:.0f} m auto-confirm limit; "
                    "operator confirmation will be required"
                )

        # -- telemetry quality at the moment of detection ---------------------------
        gps = drone_state.gps.value
        if gps is not None and gps.satellites < self._settings.safety.min_satellites:
            warnings.append(
                f"{drone_state.drone_id} had only {gps.satellites} satellites; "
                "the detection position is less reliable"
            )

        return ValidationOutcome(
            True,
            warnings=warnings,
            context={
                "geofence_status": str(evaluation.status),
                "detection_age_s": round(age, 1),
            },
        )


def is_stale_for_dispatch(detected_at: datetime, max_age_s: float) -> bool:
    """Whether a detection is too old to send an aircraft to."""
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - detected_at > timedelta(seconds=max_age_s)
