"""Mission, search sector and geofence schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import GeofenceType, MissionState, SectorState
from app.schemas.common import CheckResultSchema, Coordinate, ORMModel, PolygonInput


class MissionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None
    launch_point: Coordinate | None = None
    launch_altitude_amsl_m: float | None = None
    search_area: PolygonInput | None = None
    search_altitude_m: float | None = Field(default=None, gt=0, le=500)
    delivery_altitude_m: float | None = Field(default=None, gt=0, le=500)
    max_duration_s: int | None = Field(default=None, gt=0, le=86400)
    drone_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class MissionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    launch_point: Coordinate | None = None
    launch_altitude_amsl_m: float | None = None
    search_altitude_m: float | None = Field(default=None, gt=0, le=500)
    delivery_altitude_m: float | None = Field(default=None, gt=0, le=500)
    max_duration_s: int | None = Field(default=None, gt=0, le=86400)


class MissionResponse(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    state: MissionState
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    state_changed_at: datetime | None = None
    search_altitude_m: float | None = None
    delivery_altitude_m: float | None = None
    max_duration_s: int | None = None
    abort_reason: str | None = None
    failure_reason: str | None = None


class MissionTimingSchema(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    elapsed_s: float | None = None
    remaining_s: float | None = None
    max_duration_s: int
    expired: bool


class MissionStatusResponse(BaseModel):
    mission_id: uuid.UUID
    name: str
    state: MissionState
    state_changed_at: datetime | None = None
    timing: MissionTimingSchema
    drones: list[dict[str, Any]]
    launch_point: dict[str, float] | None = None
    search_altitude_m: float | None = None
    delivery_altitude_m: float | None = None
    abort_reason: str | None = None
    failure_reason: str | None = None


class PreflightResponse(BaseModel):
    """Result of querying the real aircraft before a launch.

    ``ready`` is false whenever any check is FAIL. There is no override
    parameter -- a failed check is fixed, not bypassed.
    """

    ready: bool
    mission_id: uuid.UUID
    generated_at: datetime
    failure_count: int
    warning_count: int
    checks: list[CheckResultSchema]


class DroneActionResultSchema(BaseModel):
    drone_id: str
    requested: str
    state: str
    acknowledged: bool
    verified: bool
    detail: str | None = None


class WorkflowResponse(BaseModel):
    """Per-aircraft outcome of a fleet-wide operation.

    ``all_succeeded`` is false if even one aircraft did not comply, and the
    failing entries carry the reason.
    """

    workflow: str
    mission_id: uuid.UUID
    mission_state: MissionState
    all_succeeded: bool
    results: list[DroneActionResultSchema]
    started_at: datetime
    completed_at: datetime | None = None
    detail: str | None = None


class MissionStartResponse(BaseModel):
    workflow: WorkflowResponse
    preflight: PreflightResponse


class AbortRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=128)


class AssignDronesRequest(BaseModel):
    drone_ids: list[str] = Field(..., min_length=1, max_length=32)


# ---------------------------------------------------------------------------
# search sectors
# ---------------------------------------------------------------------------
class SectorCreateRequest(BaseModel):
    sector_code: str = Field(..., min_length=1, max_length=32)
    polygon: PolygonInput
    priority: int = Field(default=0, ge=0, le=100)
    search_altitude_m: float | None = Field(default=None, gt=0, le=500)


class SectorAutoPartitionRequest(BaseModel):
    sector_count: int = Field(..., ge=1, le=32)
    prefix: str = Field(default="S", min_length=1, max_length=8)


class WaypointGenerateRequest(BaseModel):
    #: Ground spacing between passes. Derive it from the camera footprint at
    #: the search altitude rather than guessing.
    line_spacing_m: float | None = Field(default=None, gt=0, le=500)
    horizontal_fov_deg: float | None = Field(default=None, gt=0, lt=180)
    overlap_fraction: float = Field(default=0.2, ge=0.0, lt=0.9)
    altitude_m: float | None = Field(default=None, gt=0, le=500)
    speed_mps: float | None = Field(default=None, gt=0, le=30)


class SectorAssignRequest(BaseModel):
    drone_id: str = Field(..., min_length=1, max_length=32)


class SectorResponse(ORMModel):
    """A search sector.

    ``progress`` is ``None`` unless ``progress_status`` is MEASURED. A sector
    nobody has flown reports NOT_STARTED, not 0% -- those are different facts
    and only one of them means the ground has been looked at.
    """

    id: uuid.UUID
    mission_id: uuid.UUID
    sector_code: str
    state: SectorState
    progress: float | None = None
    progress_status: str = "NOT_STARTED"
    progress_observed_at: datetime | None = None
    priority: int
    area_m2: float | None = None
    assigned_drone_uuid: uuid.UUID | None = None
    search_altitude_m: float | None = None
    start_time: datetime | None = None
    completion_time: datetime | None = None
    blocked_reason: str | None = None


class SectorCoverageResponse(BaseModel):
    """Mission coverage, computed from real PX4 mission progress only.

    Sectors that never reported progress contribute nothing to
    ``covered_area_m2``; their area appears in ``unmeasured_area_m2`` so the
    operator can see how much of the search area is unaccounted for.
    """

    sector_count: int
    by_state: dict[str, int]
    by_progress_status: dict[str, int]
    total_area_m2: float
    covered_area_m2: float
    unmeasured_area_m2: float
    coverage_fraction: float
    completed: int
    unmeasured_sectors: int


class WaypointResponse(ORMModel):
    id: uuid.UUID
    sequence: int
    relative_altitude_m: float
    speed_mps: float | None = None
    acceptance_radius_m: float | None = None
    action: str


# ---------------------------------------------------------------------------
# geofence
# ---------------------------------------------------------------------------
class GeofenceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    fence_type: GeofenceType = GeofenceType.INCLUSION
    boundary: PolygonInput
    min_altitude_m: float | None = None
    max_altitude_m: float | None = Field(default=None, gt=0, le=1000)


class GeofenceResponse(ORMModel):
    id: uuid.UUID
    name: str
    fence_type: GeofenceType
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None


class MapResponse(BaseModel):
    """Everything the map view needs, in GeoJSON.

    Drone positions come from live telemetry; a drone with no fresh position
    is absent from ``drones`` rather than drawn at a remembered location.
    """

    mission_id: uuid.UUID
    generated_at: datetime
    launch_point: dict[str, Any] | None = None
    search_area: dict[str, Any] | None = None
    geofences: dict[str, Any]
    sectors: dict[str, Any]
    survivors: dict[str, Any]
    delivery_routes: dict[str, Any]
    drones: dict[str, Any]
    trails: dict[str, Any]
