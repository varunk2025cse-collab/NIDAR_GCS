"""Ingestion pipeline for onboard-AI detection events.

Sits between the authenticated companion-computer endpoint and the survivor
domain, and does the work that must happen before a detection is allowed to
become a survivor record:

    authenticate -> resolve drone and mission -> validate -> estimate
    geolocation accuracy -> ingest -> dedupe -> (maybe) confirm

The companion computer reports what it saw. This module decides what the GCS
believes, and records both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.detection_validator import DetectionValidator
from app.ai.geolocation import (
    CameraModel,
    accuracy_for_reported_position,
    project_pixel_to_ground,
)
from app.core.config import Settings
from app.core.enums import MISSION_ACTIVE_STATES, DetectionSource
from app.core.exceptions import DetectionRejectedError
from app.core.logging import get_logger
from app.models.mission import Mission, MissionDroneAssignment
from app.services.fleet_manager import FleetManager
from app.services.survivor_manager import IngestResult, SurvivorManager

logger = get_logger(__name__)


@dataclass(slots=True)
class DetectionEvent:
    """A detection exactly as reported by a companion computer."""

    drone_id: str
    detection_id: str
    timestamp: datetime
    confidence: float

    latitude: float | None = None
    longitude: float | None = None
    source: DetectionSource = DetectionSource.ONBOARD_AI

    # Optional geolocation inputs. When latitude/longitude are absent these
    # are required so the backend can do the projection itself.
    pixel_x: int | None = None
    pixel_y: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    horizontal_fov_deg: float | None = None
    vertical_fov_deg: float | None = None
    camera_pitch_deg: float | None = None

    reported_accuracy_m: float | None = None
    model_name: str | None = None
    model_version: str | None = None
    image_reference: str | None = None
    camera_metadata: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None


class SurvivorEventIngestion:
    def __init__(
        self,
        fleet: FleetManager,
        survivors: SurvivorManager,
        validator: DetectionValidator,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._survivors = survivors
        self._validator = validator
        self._settings = settings

    async def ingest(
        self, session: AsyncSession, event: DetectionEvent
    ) -> IngestResult:
        drone_id = event.drone_id.upper()

        if not self._fleet.connection_manager.has(drone_id):
            raise DetectionRejectedError(
                f"Detection references unknown aircraft {drone_id}",
                details={"drone_id": drone_id, "code": "UNKNOWN_DRONE"},
            )
        state = self._fleet.state(drone_id)

        mission = await self._resolve_mission(session, drone_id)
        mission_id = mission.id if mission else None

        # --- work out where the survivor is, and how well we know it -------
        latitude, longitude, accuracy_m, method, geo_warnings = self._resolve_position(
            event, state
        )

        # --- validate -------------------------------------------------------
        outcome = self._validator.validate(
            drone_state=state,
            mission_id=str(mission_id) if mission_id else None,
            mission_state=mission.state if mission else None,
            latitude=latitude,
            longitude=longitude,
            confidence=event.confidence,
            detected_at=event.timestamp,
            estimated_accuracy_m=accuracy_m,
        )
        if not outcome.accepted:
            await self._survivors.record_rejected_detection(
                session,
                mission_id=mission_id,
                drone_id=drone_id,
                external_detection_id=event.detection_id,
                detected_at=event.timestamp,
                latitude=latitude,
                longitude=longitude,
                confidence=event.confidence,
                reason=outcome.reason or "rejected",
                code=outcome.code or "REJECTED",
                payload={"event": _event_payload(event), "validation": outcome.as_dict()},
            )
            raise DetectionRejectedError(
                outcome.reason or "Detection rejected",
                details={
                    "code": outcome.code,
                    "drone_id": drone_id,
                    "detection_id": event.detection_id,
                    **outcome.context,
                },
            )

        assert mission_id is not None  # validation guarantees an active mission

        # --- ingest ----------------------------------------------------------
        position = state.position.value
        attitude = state.attitude.value
        result = await self._survivors.ingest_detection(
            session,
            mission_id=mission_id,
            drone_id=drone_id,
            external_detection_id=event.detection_id,
            detected_at=event.timestamp,
            latitude=latitude,
            longitude=longitude,
            confidence=event.confidence,
            estimated_accuracy_m=accuracy_m,
            source=event.source,
            geolocation_method=method,
            drone_position=(
                (position.latitude, position.longitude) if position else None
            ),
            drone_relative_altitude_m=position.relative_altitude_m if position else None,
            drone_absolute_altitude_m=position.absolute_altitude_m if position else None,
            drone_heading_deg=state.heading.value,
            drone_attitude=(
                (attitude.roll_deg, attitude.pitch_deg, attitude.yaw_deg)
                if attitude
                else None
            ),
            pixel=(
                (event.pixel_x, event.pixel_y)
                if event.pixel_x is not None and event.pixel_y is not None
                else None
            ),
            image_size=(
                (event.image_width, event.image_height)
                if event.image_width and event.image_height
                else None
            ),
            camera_metadata=event.camera_metadata or {},
            model_name=event.model_name,
            model_version=event.model_version,
            image_reference=event.image_reference,
            payload={"event": _event_payload(event), "validation": outcome.as_dict()},
            warnings=[*outcome.warnings, *geo_warnings],
        )
        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _resolve_position(
        self, event: DetectionEvent, state: Any
    ) -> tuple[float, float, float | None, str, list[str]]:
        """Determine the survivor coordinate and its error estimate.

        Either the companion computer supplied a coordinate (and the GCS
        computes an independent error bar), or it supplied pixel coordinates
        and camera geometry (and the GCS does the projection itself). Anything
        less is rejected: a survivor record without a defensible position is
        worse than no record.
        """
        position = state.position.value
        attitude = state.attitude.value
        gps = state.gps.value
        gps_accuracy = gps.horizontal_accuracy_m if gps else None

        if event.latitude is not None and event.longitude is not None:
            accuracy, warnings = accuracy_for_reported_position(
                drone_latitude=position.latitude if position else None,
                drone_longitude=position.longitude if position else None,
                survivor_latitude=event.latitude,
                survivor_longitude=event.longitude,
                height_above_ground_m=(
                    position.relative_altitude_m if position else None
                ),
                gps_accuracy_m=gps_accuracy,
            )
            # A companion-reported accuracy is trusted only when it is more
            # pessimistic than our own estimate.
            if event.reported_accuracy_m is not None:
                if accuracy is None or event.reported_accuracy_m > accuracy:
                    accuracy = event.reported_accuracy_m
                    warnings.append(
                        "Using the accuracy reported by the companion computer; "
                        "it is more conservative than the GCS estimate"
                    )
            return (
                event.latitude,
                event.longitude,
                accuracy,
                "COMPANION_REPORTED",
                warnings,
            )

        # No coordinate: project the pixel ourselves.
        required = (
            event.pixel_x is not None
            and event.pixel_y is not None
            and event.image_width
            and event.image_height
            and event.horizontal_fov_deg
        )
        if not required:
            raise DetectionRejectedError(
                "Detection carries neither a coordinate nor the pixel and camera "
                "geometry needed to compute one",
                details={"detection_id": event.detection_id, "code": "NO_GEOLOCATION"},
            )
        if position is None or not position.relative_altitude_m:
            raise DetectionRejectedError(
                "Cannot project a detection without a current aircraft position "
                "and altitude",
                details={"detection_id": event.detection_id, "code": "NO_AIRCRAFT_POSITION"},
            )

        camera = CameraModel(
            image_width=int(event.image_width),
            image_height=int(event.image_height),
            horizontal_fov_deg=float(event.horizontal_fov_deg),
            vertical_fov_deg=event.vertical_fov_deg,
        )
        try:
            projected = project_pixel_to_ground(
                drone_latitude=position.latitude,
                drone_longitude=position.longitude,
                height_above_ground_m=position.relative_altitude_m,
                heading_deg=state.heading.value or (attitude.yaw_deg if attitude else 0.0),
                pixel_x=int(event.pixel_x),
                pixel_y=int(event.pixel_y),
                camera=camera,
                camera_pitch_deg=(
                    event.camera_pitch_deg if event.camera_pitch_deg is not None else -90.0
                ),
                roll_deg=attitude.roll_deg if attitude else 0.0,
                pitch_deg=attitude.pitch_deg if attitude else 0.0,
            )
        except ValueError as exc:
            raise DetectionRejectedError(
                f"Detection geolocation failed: {exc}",
                details={"detection_id": event.detection_id, "code": "PROJECTION_FAILED"},
            ) from exc

        return (
            projected.latitude,
            projected.longitude,
            projected.accuracy_m,
            projected.method,
            projected.warnings,
        )

    async def _resolve_mission(
        self, session: AsyncSession, drone_id: str
    ) -> Mission | None:
        """Find the active mission this aircraft is flying.

        Preference is the live assignment held in fleet state; the database is
        the fallback after a backend restart.
        """
        state = self._fleet.state(drone_id)
        if state.mission_id:
            mission = await session.get(Mission, uuid.UUID(state.mission_id))
            if mission is not None and mission.state in MISSION_ACTIVE_STATES:
                return mission

        drone_uuid = self._fleet.drone_uuid(drone_id)
        if drone_uuid is None:
            return None
        result = await session.execute(
            select(Mission)
            .join(MissionDroneAssignment, MissionDroneAssignment.mission_id == Mission.id)
            .where(
                MissionDroneAssignment.drone_uuid == drone_uuid,
                MissionDroneAssignment.released_at.is_(None),
                Mission.state.in_(list(MISSION_ACTIVE_STATES)),
            )
            .order_by(Mission.started_at.desc())
            .limit(1)
        )
        mission = result.scalar_one_or_none()
        if mission is not None:
            # Re-sync the live view so subsequent events skip the query.
            self._fleet.assign_mission(drone_id, str(mission.id))
        return mission


def _event_payload(event: DetectionEvent) -> dict[str, Any]:
    """Preserve the raw event as reported, for later re-examination."""
    return {
        "drone_id": event.drone_id,
        "detection_id": event.detection_id,
        "timestamp": event.timestamp.isoformat()
        if event.timestamp.tzinfo
        else event.timestamp.replace(tzinfo=UTC).isoformat(),
        "confidence": event.confidence,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "pixel": [event.pixel_x, event.pixel_y]
        if event.pixel_x is not None
        else None,
        "image_size": [event.image_width, event.image_height]
        if event.image_width
        else None,
        "horizontal_fov_deg": event.horizontal_fov_deg,
        "camera_pitch_deg": event.camera_pitch_deg,
        "reported_accuracy_m": event.reported_accuracy_m,
        "model": {"name": event.model_name, "version": event.model_version},
        "image_reference": event.image_reference,
        "source": str(event.source),
        "extra": event.extra or {},
        "received_at": datetime.now(UTC).isoformat(),
    }
