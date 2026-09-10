"""Drone, telemetry and command schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import DroneRole
from app.core.enums import ConnectionState
from app.schemas.common import ORMModel


class DroneIdentitySchema(BaseModel):
    system_id: int
    component_id: int
    autopilot: str | None = None
    vehicle_type: str | None = None
    hardware_uid: str | None = None
    firmware_version: str | None = None
    firmware_vendor: str | None = None
    product_name: str | None = None


class ConnectionSchema(BaseModel):
    state: ConnectionState
    since: datetime
    connected_since: datetime | None = None
    disconnected_at: datetime | None = None
    last_contact_at: datetime | None = None
    last_contact_age_s: float | None = None
    identity_verified: bool
    reconnect_attempts: int
    last_error: str | None = None


class DroneCardResponse(BaseModel):
    """Compact projection for the dashboard drone cards.

    Every value has a companion ``*_status``. A ``None`` value with a
    ``STALE``/``NO_DATA`` status means the aircraft is not reporting it -- it
    never means zero.
    """

    drone_id: str
    role: DroneRole
    connection_state: ConnectionState
    online: bool
    identity_verified: bool

    battery_percent: float | None = None
    battery_status: str
    battery_voltage_v: float | None = None

    latitude: float | None = None
    longitude: float | None = None
    altitude_relative_m: float | None = None
    position_status: str

    ground_speed_mps: float | None = None
    vertical_speed_mps: float | None = None
    speed_status: str

    heading_deg: float | None = None
    heading_status: str

    flight_mode: str | None = None
    flight_mode_status: str
    armed: bool | None = None

    gps_fix: str | None = None
    satellites: int | None = None
    gps_status: str

    geofence_status: str
    mission_id: str | None = None
    sector_code: str | None = None
    delivery_task_id: str | None = None
    last_contact_age_s: float | None = None


class DroneDetailResponse(BaseModel):
    """Full state, with per-field freshness on every telemetry value."""

    drone_id: str
    name: str | None = None
    role: DroneRole
    system_id: int
    component_id: int
    endpoint: str
    connection: ConnectionSchema
    identity: DroneIdentitySchema | None = None
    telemetry: dict[str, Any]
    last_known_position: dict[str, Any] | None = None
    geofence: dict[str, Any]
    assignment: dict[str, Any]
    status_texts: list[dict[str, Any]] = Field(default_factory=list)


class FleetSummaryResponse(BaseModel):
    total: int
    online: int
    degraded: int
    offline: int
    identity_verified: int
    armed: int
    in_air: int
    with_fresh_position: int
    by_connection_state: dict[str, int]
    by_role: dict[str, int]
    generated_at: datetime


class FleetResponse(BaseModel):
    summary: FleetSummaryResponse
    drones: list[DroneCardResponse]


class DroneRegistryResponse(ORMModel):
    """The persisted record of an airframe."""

    id: uuid.UUID
    drone_id: str
    name: str | None = None
    role: DroneRole
    system_id: int
    component_id: int
    connection_endpoint: str
    hardware_uid: str | None = None
    autopilot_type: str | None = None
    vehicle_type: str | None = None
    firmware_version: str | None = None
    product_name: str | None = None
    payload_capacity_g: int | None = None
    enabled: bool


class DroneHealthResponse(BaseModel):
    drone_id: str
    connection_state: ConnectionState
    identity_verified: bool
    health: dict[str, Any]
    gps: dict[str, Any]
    battery: dict[str, Any]
    telemetry_freshness: dict[str, str]
    link: dict[str, Any]
    active_alerts: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
class CommandRequestBase(BaseModel):
    """Common fields for any command aimed at a physical aircraft."""

    #: Supply the same key to retry safely. Without one the server generates a
    #: unique key, so an accidental double submit issues two commands.
    idempotency_key: str | None = Field(default=None, max_length=128)
    mission_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=255)


class TakeoffRequest(CommandRequestBase):
    altitude_m: float = Field(..., gt=0, le=500,
                              description="Relative takeoff altitude in metres")


class GotoRequest(CommandRequestBase):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    absolute_altitude_m: float = Field(..., description="AMSL altitude in metres")
    yaw_deg: float | None = None


class TelemetrySampleResponse(ORMModel):
    sampled_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    relative_altitude_m: float | None = None
    absolute_altitude_m: float | None = None
    ground_speed_mps: float | None = None
    vertical_speed_mps: float | None = None
    heading_deg: float | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    yaw_deg: float | None = None
    battery_percent: float | None = None
    battery_voltage_v: float | None = None
    battery_current_a: float | None = None
    gps_fix_type: int | None = None
    satellites: int | None = None
    armed: bool | None = None
    in_air: bool | None = None
    flight_mode: str | None = None
    freshness: dict[str, Any] = Field(default_factory=dict)


class TelemetryHistoryResponse(BaseModel):
    drone_id: str
    sample_count: int
    since: datetime | None = None
    until: datetime | None = None
    samples: list[TelemetrySampleResponse]


class CommandRecordResponse(ORMModel):
    id: uuid.UUID
    command_type: str
    state: str
    drone_uuid: uuid.UUID
    mission_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None
    requested_at: datetime
    sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None
    verified: bool
    failure_reason: str | None = None
    acknowledgement: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
