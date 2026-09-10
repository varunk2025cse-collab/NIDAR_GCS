"""Survivor, detection and delivery schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.enums import (
    DeliveryConfirmationSource,
    DeliveryState,
    DetectionSource,
    SurvivorState,
)
from app.schemas.common import ORMModel


class DetectionEventRequest(BaseModel):
    """A detection reported by an onboard perception system.

    Either a computed coordinate, or the pixel plus camera geometry for the
    backend to project. A detection with neither is rejected: a survivor
    record needs a position that can be defended.
    """

    drone_id: str = Field(..., min_length=1, max_length=32)
    detection_id: str = Field(..., min_length=1, max_length=64,
                              description="Unique per drone, e.g. DET-001")
    timestamp: datetime
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="Classifier confidence, not position accuracy")

    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    #: Position error in metres, if the companion computed one.
    estimated_accuracy_m: float | None = Field(default=None, ge=0, le=10000)

    pixel_x: int | None = Field(default=None, ge=0)
    pixel_y: int | None = Field(default=None, ge=0)
    image_width: int | None = Field(default=None, gt=0)
    image_height: int | None = Field(default=None, gt=0)
    horizontal_fov_deg: float | None = Field(default=None, gt=0, lt=180)
    vertical_fov_deg: float | None = Field(default=None, gt=0, lt=180)
    camera_pitch_deg: float | None = Field(default=None, ge=-90, le=90)

    source: DetectionSource = DetectionSource.ONBOARD_AI
    model_name: str | None = Field(default=None, max_length=64)
    model_version: str | None = Field(default=None, max_length=32)
    image_reference: str | None = Field(default=None, max_length=255)
    camera_metadata: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"protected_namespaces": ()}

    @model_validator(mode="after")
    def _needs_a_position(self) -> DetectionEventRequest:
        has_coordinate = self.latitude is not None and self.longitude is not None
        has_pixel_geometry = (
            self.pixel_x is not None
            and self.pixel_y is not None
            and self.image_width is not None
            and self.image_height is not None
            and self.horizontal_fov_deg is not None
        )
        if not has_coordinate and not has_pixel_geometry:
            raise ValueError(
                "supply either latitude/longitude, or pixel coordinates with "
                "image size and horizontal_fov_deg"
            )
        return self


class DetectionIngestResponse(BaseModel):
    detection_id: uuid.UUID
    external_detection_id: str
    survivor_id: uuid.UUID | None = None
    survivor_code: str | None = None
    survivor_state: str | None = None
    created_new_survivor: bool
    matched_existing: bool
    match_distance_m: float | None = None
    confirmed: bool
    warnings: list[str] = Field(default_factory=list)


class SurvivorResponse(ORMModel):
    id: uuid.UUID
    mission_id: uuid.UUID
    survivor_code: str
    state: SurvivorState
    state_changed_at: datetime | None = None
    #: Classifier confidence of the best detection.
    best_confidence: float | None = None
    #: Position error in metres. Deliberately distinct from confidence.
    location_accuracy_m: float | None = None
    observation_count: int
    priority: int
    first_detected_at: datetime
    last_observed_at: datetime | None = None
    confirmed_at: datetime | None = None
    delivered_at: datetime | None = None
    rejection_reason: str | None = None
    notes: str | None = None
    duplicate_of_id: uuid.UUID | None = None


class SurvivorDetailResponse(SurvivorResponse):
    latitude: float
    longitude: float
    detections: list[DetectionRecordResponse] = Field(default_factory=list)
    delivery_tasks: list[DeliveryTaskResponse] = Field(default_factory=list)


class DetectionRecordResponse(ORMModel):
    id: uuid.UUID
    external_detection_id: str
    source: DetectionSource
    detected_at: datetime
    received_at: datetime
    confidence: float
    estimated_accuracy_m: float | None = None
    geolocation_method: str | None = None
    accepted: bool
    rejection_reason: str | None = None
    is_duplicate_of_existing: bool
    duplicate_distance_m: float | None = None
    model_name: str | None = None
    model_version: str | None = None
    image_reference: str | None = None

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class SurvivorConfirmRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class SurvivorRejectRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


class SurvivorDuplicateRequest(BaseModel):
    primary_survivor_id: uuid.UUID


class SurvivorSummaryResponse(BaseModel):
    found: int
    delivered: int
    pending_delivery: int
    confirmed: int
    duplicates: int
    rejected: int
    by_state: dict[str, int]


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
class DeliveryCreateRequest(BaseModel):
    survivor_id: uuid.UUID
    #: Leave unset to let the dispatcher choose the safest available aircraft.
    drone_id: str | None = Field(default=None, max_length=32)
    priority: int = Field(default=0, ge=0, le=100)
    payload_type: str | None = Field(default=None, max_length=64)
    payload_mass_g: int | None = Field(default=None, ge=0, le=100000)
    dispatch_immediately: bool = True
    idempotency_key: str | None = Field(default=None, max_length=128)


class DeliveryTaskResponse(ORMModel):
    id: uuid.UUID
    mission_id: uuid.UUID
    survivor_id: uuid.UUID
    drone_uuid: uuid.UUID | None = None
    task_code: str
    state: DeliveryState
    state_changed_at: datetime | None = None
    priority: int
    planned_distance_m: float | None = None
    estimated_duration_s: float | None = None
    delivery_altitude_m: float | None = None
    payload_type: str | None = None
    payload_mass_g: int | None = None
    assigned_at: datetime | None = None
    departed_at: datetime | None = None
    arrived_at: datetime | None = None
    delivered_at: datetime | None = None
    completed_at: datetime | None = None
    confirmation_source: DeliveryConfirmationSource | None = None
    confirmation_detail: str | None = None
    failure_reason: str | None = None
    cancellation_reason: str | None = None
    safety_evaluation: dict[str, Any] = Field(default_factory=dict)


class DeliveryDetailResponse(DeliveryTaskResponse):
    drone_id: str | None = None
    survivor_code: str | None = None
    target: dict[str, float] | None = None
    events: list[DeliveryEventResponse] = Field(default_factory=list)


class DeliveryEventResponse(ORMModel):
    id: uuid.UUID
    occurred_at: datetime
    event_type: str
    from_state: str | None = None
    to_state: str | None = None
    message: str | None = None
    automatic: bool
    evidence: dict[str, Any] = Field(default_factory=dict)


class DeliveryDispatchResponse(BaseModel):
    task_code: str
    drone_id: str | None = None
    dispatched: bool
    target: dict[str, float] | None = None
    command: dict[str, Any] | None = None
    safety: dict[str, Any] | None = None
    detail: str | None = None


class DeliveryConfirmationRequest(BaseModel):
    """Physical confirmation that a payload was released.

    ``confirmed=false`` records a failed release, which is as important to
    capture as a successful one.
    """

    source: DeliveryConfirmationSource
    confirmed: bool = True
    detail: str = Field(..., min_length=1, max_length=255)
    evidence: dict[str, Any] = Field(default_factory=dict)


class DeliveryReleaseRequest(BaseModel):
    confirmation_timeout_s: float = Field(default=60.0, ge=1.0, le=600.0)


class DeliveryCancelRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


class DeliverySafetyResponse(BaseModel):
    drone_id: str
    safe: bool
    distance_m: float | None = None
    estimated_duration_s: float | None = None
    estimated_battery_cost_pct: float | None = None
    battery_after_pct: float | None = None
    checks: list[dict[str, Any]]


SurvivorDetailResponse.model_rebuild()
DeliveryDetailResponse.model_rebuild()
