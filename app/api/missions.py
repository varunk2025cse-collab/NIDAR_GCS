"""Mission lifecycle endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from geoalchemy2.shape import from_shape
from shapely.geometry import Point

from app.api.deps import (
    ClientIp,
    DbSession,
    OperatorPrincipal,
    RequestId,
    Services,
    ViewerPrincipal,
)
from app.core.enums import MissionState
from app.core.exceptions import PreflightFailedError
from app.core.logging import get_logger
from app.schemas.common import MessageResponse
from app.schemas.mission import (
    AbortRequest,
    AssignDronesRequest,
    MissionCreateRequest,
    MissionResponse,
    MissionStartResponse,
    MissionStatusResponse,
    MissionUpdateRequest,
    PreflightResponse,
    WorkflowResponse,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/missions", tags=["missions"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get("", response_model=list[MissionResponse])
async def list_missions(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MissionResponse]:
    missions = await services.missions.list_missions(session, limit=limit, offset=offset)
    return [MissionResponse.model_validate(m) for m in missions]


@router.post("", response_model=MissionResponse, status_code=status.HTTP_201_CREATED)
async def create_mission(
    payload: MissionCreateRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MissionResponse:
    mission = await services.missions.create(
        session,
        name=payload.name,
        principal=principal,
        description=payload.description,
        launch_point=payload.launch_point.as_tuple() if payload.launch_point else None,
        launch_altitude_amsl_m=payload.launch_altitude_amsl_m,
        search_area=payload.search_area.as_tuples() if payload.search_area else None,
        search_altitude_m=payload.search_altitude_m,
        delivery_altitude_m=payload.delivery_altitude_m,
        max_duration_s=payload.max_duration_s,
        parameters=payload.parameters,
    )
    if payload.drone_ids:
        await services.missions.assign_drones(session, mission, payload.drone_ids)
    return MissionResponse.model_validate(mission)


@router.get("/active", response_model=MissionResponse | None)
async def active_mission(
    _: ViewerPrincipal, services: Services, session: DbSession
) -> MissionResponse | None:
    mission = await services.missions.active_mission(session)
    return MissionResponse.model_validate(mission) if mission else None


@router.get("/{mission_id}", response_model=MissionResponse)
async def get_mission(
    mission_id: uuid.UUID, _: ViewerPrincipal, services: Services, session: DbSession
) -> MissionResponse:
    mission = await services.missions.get(session, mission_id)
    return MissionResponse.model_validate(mission)


@router.patch("/{mission_id}", response_model=MissionResponse)
async def update_mission(
    mission_id: uuid.UUID,
    payload: MissionUpdateRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MissionResponse:
    """Edit a mission plan.

    Only permitted before the mission goes live: changing the plan under a
    flying aircraft would put the GCS view and the uploaded plan out of step.
    """
    mission = await services.missions.get(session, mission_id)
    if mission.state not in (MissionState.DRAFT, MissionState.READY):
        raise PreflightFailedError(
            f"Mission is {mission.state}; only DRAFT or READY missions can be edited",
            details={"state": str(mission.state)},
        )

    if payload.name is not None:
        mission.name = payload.name
    if payload.description is not None:
        mission.description = payload.description
    if payload.launch_point is not None:
        mission.launch_point = from_shape(
            Point(payload.launch_point.longitude, payload.launch_point.latitude), srid=4326
        )
    if payload.launch_altitude_amsl_m is not None:
        mission.launch_altitude_amsl_m = payload.launch_altitude_amsl_m
    if payload.search_altitude_m is not None:
        mission.search_altitude_m = payload.search_altitude_m
    if payload.delivery_altitude_m is not None:
        mission.delivery_altitude_m = payload.delivery_altitude_m
    if payload.max_duration_s is not None:
        mission.max_duration_s = payload.max_duration_s

    await session.flush()
    logger.info("mission_updated", mission_id=str(mission_id),
                operator=principal.username)
    return MissionResponse.model_validate(mission)


@router.get("/{mission_id}/state", response_model=MissionStatusResponse)
async def mission_state(
    mission_id: uuid.UUID, _: ViewerPrincipal, services: Services, session: DbSession
) -> MissionStatusResponse:
    mission = await services.missions.get(session, mission_id)
    return MissionStatusResponse.model_validate(
        await services.missions.status(session, mission)
    )


@router.post("/{mission_id}/drones", response_model=MessageResponse)
async def assign_drones(
    mission_id: uuid.UUID,
    payload: AssignDronesRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MessageResponse:
    mission = await services.missions.get(session, mission_id)
    assignments = await services.missions.assign_drones(
        session, mission, payload.drone_ids
    )
    return MessageResponse(
        message=f"{len(assignments)} aircraft assigned to {mission.name}",
        detail={"drone_ids": payload.drone_ids},
    )


# ---------------------------------------------------------------------------
# preflight and lifecycle
# ---------------------------------------------------------------------------
@router.post("/{mission_id}/preflight", response_model=PreflightResponse)
async def run_preflight(
    mission_id: uuid.UUID,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> PreflightResponse:
    """Query the real aircraft and report readiness.

    Safe to run at any time -- it sends no commands, it only reads.
    """
    mission = await services.missions.get(session, mission_id)
    report = await services.missions.run_preflight(session, mission)
    return PreflightResponse.model_validate(report.as_dict())


@router.post("/{mission_id}/start", response_model=MissionStartResponse)
async def start_mission(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> MissionStartResponse:
    """Run preflight, then arm and take off the assigned aircraft.

    Blocked if any preflight check fails. There is no override.
    """
    mission = await services.missions.get(session, mission_id)
    workflow, report = await services.missions.start(
        session, mission, principal, request_id=request_id
    )
    return MissionStartResponse(
        workflow=WorkflowResponse.model_validate(workflow.as_dict()),
        preflight=PreflightResponse.model_validate(report.as_dict()),
    )


@router.post("/{mission_id}/pause", response_model=WorkflowResponse)
async def pause_mission(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> WorkflowResponse:
    mission = await services.missions.get(session, mission_id)
    result = await services.missions.pause(
        session, mission, principal, request_id=request_id
    )
    return WorkflowResponse.model_validate(result.as_dict())


@router.post("/{mission_id}/resume", response_model=WorkflowResponse)
async def resume_mission(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> WorkflowResponse:
    mission = await services.missions.get(session, mission_id)
    result = await services.missions.resume(
        session, mission, principal, request_id=request_id
    )
    return WorkflowResponse.model_validate(result.as_dict())


@router.post("/{mission_id}/abort", response_model=WorkflowResponse)
async def abort_mission(
    mission_id: uuid.UUID,
    payload: AbortRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
    source_ip: ClientIp,
) -> WorkflowResponse:
    """Emergency abort.

    Airborne aircraft are sent home; aircraft on the ground are disarmed. The
    response lists what each aircraft actually did. If any aircraft did not
    comply the mission ends in PARTIAL_ABORT_FAILURE, not ABORTED.
    """
    mission = await services.missions.get(session, mission_id)
    result = await services.missions.abort(
        session, mission, principal, payload.reason, request_id=request_id
    )
    return WorkflowResponse.model_validate(result.as_dict())


@router.post("/{mission_id}/rtl-all", response_model=WorkflowResponse)
async def rtl_all(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> WorkflowResponse:
    """Send every mission aircraft home, and report which ones confirmed."""
    mission = await services.missions.get(session, mission_id)
    result = await services.missions.rtl_all(
        session, mission, principal, request_id=request_id
    )
    return WorkflowResponse.model_validate(result.as_dict())


@router.post("/{mission_id}/complete", response_model=MissionResponse)
async def complete_mission(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MissionResponse:
    """Close out a mission. Refused while any aircraft is still airborne."""
    mission = await services.missions.get(session, mission_id)
    mission = await services.missions.complete(session, mission, principal)
    return MissionResponse.model_validate(mission)


@router.get("/{mission_id}/timeline")
async def mission_timeline(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """The mission narrative, newest first, every entry timestamped."""
    events = await services.events.timeline(
        session, mission_id=mission_id, limit=limit, offset=offset
    )
    return {
        "mission_id": str(mission_id),
        "count": len(events),
        "events": [
            {
                "occurred_at": e.occurred_at.isoformat(),
                "event_type": e.event_type,
                "severity": str(e.severity),
                "message": e.message,
                "drone_uuid": str(e.drone_uuid) if e.drone_uuid else None,
                "data": e.data,
            }
            for e in events
        ],
    }
