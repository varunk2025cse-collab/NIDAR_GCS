"""System health, alerts, events, audit and video metadata."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import desc, select

from app.api.deps import (
    AdminPrincipal,
    DbSession,
    OperatorPrincipal,
    Services,
    ViewerPrincipal,
)
from app.core.enums import MISSION_ACTIVE_STATES, AlertSeverity, ComponentStatus
from app.core.exceptions import NotFoundError
from app.models.event import Alert, AuditLog
from app.schemas.common import MessageResponse
from app.schemas.system import (
    ActiveAlertResponse,
    AlertResponse,
    AuditLogResponse,
    CameraResponse,
    DashboardResponse,
    MissionEventResponse,
    SystemHealthResponse,
    VideoSummaryResponse,
)

router = APIRouter(tags=["system"])


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@router.get("/system/health", response_model=SystemHealthResponse)
async def system_health(
    _: ViewerPrincipal, services: Services, response: Response
) -> SystemHealthResponse:
    """Measured status of every subsystem.

    Returns 503 when any component has FAILED, so a monitoring probe sees the
    degradation without parsing the body.
    """
    report = await services.health.check_all()
    if report["status"] == str(ComponentStatus.FAILED):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return SystemHealthResponse.model_validate(report)


@router.get("/system/status")
async def system_status(_: ViewerPrincipal, services: Services) -> dict:
    """Internal diagnostics: bus, sockets, links, telemetry pipeline."""
    return services.status()


@router.get("/system/calibration")
async def calibration_status(
    _: ViewerPrincipal, services: Services, response: Response
) -> dict:
    """Delivery energy model calibration state.

    Returns 409 while UNCALIBRATED, because in that state the backend will
    refuse every delivery dispatch. The dashboard should surface this before a
    survivor is found rather than at the moment one needs aid.
    """
    described = services.energy_model.describe()
    if not described["calibrated"]:
        response.status_code = status.HTTP_409_CONFLICT
    return {
        **described,
        "delivery_dispatch_enabled": described["calibrated"]
        or services.settings.safety.allow_uncalibrated_delivery,
        "allow_uncalibrated_delivery": (
            services.settings.safety.allow_uncalibrated_delivery
        ),
        "calibrate_with": "python -m scripts.calibrate_energy",
    }


@router.get("/system/liveness")
async def liveness() -> dict:
    """Unauthenticated process liveness. Says nothing about the fleet."""
    return {"status": "alive", "time": datetime.now(UTC).isoformat()}


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    events: Annotated[int, Query(ge=1, le=100)] = 10,
) -> DashboardResponse:
    """One call for the dashboard header and side panels.

    Everything is computed at request time from live state and the database.
    """
    mission = await services.missions.active_mission(session)
    summary = services.fleet.summary()

    survivor_counts = (
        await services.survivors.summary(session, mission.id)
        if mission
        else {"found": 0, "delivered": 0}
    )
    timing = services.missions.timing(mission) if mission else None

    recent = await services.events.timeline(
        session, mission_id=mission.id if mission else None, limit=events
    )
    health = await services.health.check_all()

    return DashboardResponse(
        mission_active=mission is not None and mission.state in MISSION_ACTIVE_STATES,
        mission=(
            {
                "id": str(mission.id),
                "name": mission.name,
                "state": str(mission.state),
                "started_at": mission.started_at.isoformat() if mission.started_at else None,
            }
            if mission
            else None
        ),
        drones_online=summary["online"],
        drones_total=summary["total"],
        survivors_found=survivor_counts["found"],
        survivors_delivered=survivor_counts["delivered"],
        mission_elapsed_s=timing["elapsed_s"] if timing else None,
        mission_remaining_s=timing["remaining_s"] if timing else None,
        fleet=services.fleet.cards(),
        active_alerts=[
            ActiveAlertResponse.model_validate(a) for a in services.safety.active_alerts()
        ],
        recent_events=[MissionEventResponse.model_validate(e) for e in recent],
        system_health=SystemHealthResponse.model_validate(health),
        generated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------
@router.get("/alerts/active", response_model=list[ActiveAlertResponse])
async def active_alerts(
    _: ViewerPrincipal, services: Services
) -> list[ActiveAlertResponse]:
    """Standing safety conditions, most severe first."""
    return [ActiveAlertResponse.model_validate(a) for a in services.safety.active_alerts()]


@router.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    _: ViewerPrincipal,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    severity: AlertSeverity | None = None,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AlertResponse]:
    stmt = select(Alert).order_by(desc(Alert.raised_at))
    if mission_id is not None:
        stmt = stmt.where(Alert.mission_id == mission_id)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if active_only:
        stmt = stmt.where(Alert.active.is_(True))
    result = await session.execute(stmt.limit(limit).offset(offset))
    return [AlertResponse.model_validate(a) for a in result.scalars().all()]


@router.post("/alerts/{alert_id}/acknowledge", response_model=MessageResponse)
async def acknowledge_alert(
    alert_id: uuid.UUID, principal: OperatorPrincipal, services: Services
) -> MessageResponse:
    """Acknowledge an alert.

    Records that an operator has seen it. It does not clear it -- an alert
    clears when the underlying condition goes away.
    """
    ok = await services.safety.acknowledge(alert_id, principal.operator_id)
    if not ok:
        raise NotFoundError(
            f"Alert {alert_id} does not exist", details={"alert_id": str(alert_id)}
        )
    return MessageResponse(
        message="Alert acknowledged",
        detail={"note": "The alert stays active until the condition resolves"},
    )


# ---------------------------------------------------------------------------
# events and audit
# ---------------------------------------------------------------------------
@router.get("/events", response_model=list[MissionEventResponse])
async def list_events(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    event_type: Annotated[list[str] | None, Query()] = None,
    min_severity: AlertSeverity | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MissionEventResponse]:
    events = await services.events.timeline(
        session,
        mission_id=mission_id,
        limit=limit,
        offset=offset,
        event_types=event_type,
        min_severity=min_severity,
    )
    return [MissionEventResponse.model_validate(e) for e in events]


@router.get("/audit", response_model=list[AuditLogResponse])
async def audit_trail(
    _: AdminPrincipal,
    services: Services,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogResponse]:
    """Every control action, including the refused ones. Admin only."""
    logs = await services.events.audit_trail(
        session, limit=limit, offset=offset, mission_id=mission_id, action=action
    )
    return [AuditLogResponse.model_validate(entry) for entry in logs]


@router.get("/audit/commands", response_model=list[AuditLogResponse])
async def command_audit(
    _: AdminPrincipal,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditLogResponse]:
    """Audit entries for physical-drone commands only."""
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.action.like("%_COMMAND"))
        .order_by(desc(AuditLog.occurred_at))
        .limit(limit)
    )
    return [AuditLogResponse.model_validate(entry) for entry in result.scalars().all()]


# ---------------------------------------------------------------------------
# video metadata
# ---------------------------------------------------------------------------
@router.get("/video/cameras", response_model=list[CameraResponse])
async def list_cameras(
    _: ViewerPrincipal, services: Services, drone_id: str | None = None
) -> list[CameraResponse]:
    """Camera inventory and live stream reachability.

    The player connects directly to ``stream_url`` on the local network;
    video does not pass through this backend.
    """
    return [CameraResponse.model_validate(c) for c in services.video.feed_list(drone_id)]


@router.get("/video/cameras/{camera_id}", response_model=CameraResponse)
async def get_camera(
    camera_id: str, _: ViewerPrincipal, services: Services
) -> CameraResponse:
    camera = services.video.get(camera_id)
    return CameraResponse.model_validate(services.video.describe(camera))


@router.get("/video/summary", response_model=VideoSummaryResponse)
async def video_summary(
    _: ViewerPrincipal, services: Services
) -> VideoSummaryResponse:
    return VideoSummaryResponse.model_validate(services.video.summary())


@router.post("/video/refresh", response_model=VideoSummaryResponse)
async def refresh_streams(
    _: OperatorPrincipal, services: Services
) -> VideoSummaryResponse:
    """Re-probe every configured stream endpoint now."""
    await services.video.check_all_streams()
    return VideoSummaryResponse.model_validate(services.video.summary())


# ---------------------------------------------------------------------------
# recent mission events for the sidebar
# ---------------------------------------------------------------------------
@router.get("/events/recent", response_model=list[MissionEventResponse])
async def recent_events(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[MissionEventResponse]:
    mission = await services.missions.active_mission(session)
    events = await services.events.timeline(
        session, mission_id=mission.id if mission else None, limit=limit
    )
    return [MissionEventResponse.model_validate(e) for e in events]


@router.get("/system/health/history")
async def health_history(
    _: ViewerPrincipal,
    session: DbSession,
    component: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    """Recorded subsystem status, for reviewing when the GCS was degraded."""
    from app.models.event import SystemHealthSample

    stmt = select(SystemHealthSample).order_by(desc(SystemHealthSample.sampled_at))
    if component:
        stmt = stmt.where(SystemHealthSample.component == component)
    result = await session.execute(stmt.limit(limit))
    rows = result.scalars().all()
    return {
        "count": len(rows),
        "samples": [
            {
                "sampled_at": r.sampled_at.isoformat(),
                "component": r.component,
                "status": str(r.status),
                "latency_ms": r.latency_ms,
                "detail": r.detail,
            }
            for r in rows
        ],
    }
