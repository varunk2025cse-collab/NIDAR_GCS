"""System, alert, event and video schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import AlertCategory, AlertSeverity, ComponentStatus
from app.schemas.common import ORMModel


class ComponentHealthResponse(BaseModel):
    component: str
    status: ComponentStatus
    detail: str | None = None
    latency_ms: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class SystemHealthResponse(BaseModel):
    """Measured subsystem status.

    Every entry is the result of an actual check. A component that cannot be
    measured reports UNKNOWN rather than OK.
    """

    status: ComponentStatus
    checked_at: datetime
    uptime_s: float
    environment: str
    components: list[ComponentHealthResponse]


class AlertResponse(ORMModel):
    id: uuid.UUID
    mission_id: uuid.UUID | None = None
    drone_uuid: uuid.UUID | None = None
    category: AlertCategory
    severity: AlertSeverity
    code: str
    message: str
    raised_at: datetime
    cleared_at: datetime | None = None
    active: bool
    acknowledged_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class ActiveAlertResponse(BaseModel):
    alert_id: uuid.UUID | None = None
    code: str
    category: str
    severity: str
    message: str
    drone_id: str | None = None
    mission_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    raised_at: datetime
    last_seen_at: datetime


class MissionEventResponse(ORMModel):
    id: uuid.UUID
    mission_id: uuid.UUID | None = None
    drone_uuid: uuid.UUID | None = None
    survivor_id: uuid.UUID | None = None
    delivery_task_id: uuid.UUID | None = None
    occurred_at: datetime
    event_type: str
    severity: AlertSeverity
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class AuditLogResponse(ORMModel):
    id: uuid.UUID
    occurred_at: datetime
    operator_username: str | None = None
    operator_role: str | None = None
    mission_id: uuid.UUID | None = None
    drone_uuid: uuid.UUID | None = None
    command_id: uuid.UUID | None = None
    action: str
    request_id: str | None = None
    result: str
    acknowledgement: str | None = None
    failure_reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class DashboardResponse(BaseModel):
    """One call backing the dashboard header and side panels.

    Everything here is derived from live state and the database at request
    time; nothing is cached from an earlier mission.
    """

    mission_active: bool
    mission: dict[str, Any] | None = None
    drones_online: int
    drones_total: int
    survivors_found: int
    survivors_delivered: int
    mission_elapsed_s: float | None = None
    mission_remaining_s: float | None = None
    fleet: list[dict[str, Any]]
    active_alerts: list[ActiveAlertResponse]
    recent_events: list[MissionEventResponse]
    system_health: SystemHealthResponse
    generated_at: datetime


class StreamStatusResponse(BaseModel):
    camera_id: str
    reachable: bool
    status: str
    checked_at: datetime | None = None
    latency_ms: float | None = None
    detail: str | None = None


class CameraResponse(BaseModel):
    """Camera metadata. Video itself never passes through this backend."""

    camera_id: str
    drone_id: str
    label: str
    kind: str
    protocol: str
    stream_url: str
    resolution: str | None = None
    framerate: int | None = None
    ai_overlay: bool
    enabled: bool
    attributes: dict[str, Any] = Field(default_factory=dict)
    stream_status: dict[str, Any]
    transport_note: str


class VideoSummaryResponse(BaseModel):
    total: int
    enabled: int
    reachable: int
    unreachable: list[str]
    checked: int
