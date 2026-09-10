"""Search sector planning, assignment and progress."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    DbSession,
    OperatorPrincipal,
    RequestId,
    Services,
    ViewerPrincipal,
)
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.schemas.common import MessageResponse
from app.schemas.mission import (
    SectorAssignRequest,
    SectorAutoPartitionRequest,
    SectorCoverageResponse,
    SectorCreateRequest,
    SectorResponse,
    WaypointGenerateRequest,
    WaypointResponse,
)
from app.services.search_manager import (
    estimate_line_spacing_m,
    progress_status,
    reported_progress,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/missions/{mission_id}/sectors", tags=["search"])


@router.get("", response_model=list[SectorResponse])
async def list_sectors(
    mission_id: uuid.UUID, _: ViewerPrincipal, services: Services, session: DbSession
) -> list[SectorResponse]:
    sectors = await services.search.list_sectors(session, mission_id)
    return [_sector_response(s) for s in sectors]


def _sector_response(sector) -> SectorResponse:
    """Render a sector without presenting unmeasured coverage as zero."""
    payload = SectorResponse.model_validate(sector).model_dump()
    payload["progress"] = reported_progress(sector)
    payload["progress_status"] = str(progress_status(sector))
    return SectorResponse.model_validate(payload)


@router.post("", response_model=SectorResponse, status_code=status.HTTP_201_CREATED)
async def create_sector(
    mission_id: uuid.UUID,
    payload: SectorCreateRequest,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> SectorResponse:
    mission = await services.missions.get(session, mission_id)
    await services.geofence.load_for_mission(session, mission_id)
    sector = await services.search.create_sector(
        session,
        mission,
        sector_code=payload.sector_code,
        polygon=payload.polygon.as_tuples(),
        priority=payload.priority,
        search_altitude_m=payload.search_altitude_m,
    )
    return _sector_response(sector)


@router.post("/auto-partition", response_model=list[SectorResponse])
async def auto_partition(
    mission_id: uuid.UUID,
    payload: SectorAutoPartitionRequest,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> list[SectorResponse]:
    """Split the mission search area into strips, one per aircraft."""
    mission = await services.missions.get(session, mission_id)
    sectors = await services.search.auto_partition(
        session, mission, payload.sector_count, prefix=payload.prefix
    )
    return [_sector_response(s) for s in sectors]


@router.get("/coverage", response_model=SectorCoverageResponse)
async def coverage(
    mission_id: uuid.UUID, _: ViewerPrincipal, services: Services, session: DbSession
) -> SectorCoverageResponse:
    """Coverage, computed from real PX4 mission-item progress."""
    return SectorCoverageResponse.model_validate(
        await services.search.coverage_summary(session, mission_id)
    )


@router.get("/geojson")
async def sectors_geojson(
    mission_id: uuid.UUID, _: ViewerPrincipal, services: Services, session: DbSession
) -> dict:
    return await services.search.sectors_geojson(session, mission_id)


@router.post("/auto-assign", response_model=MessageResponse)
async def auto_assign(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MessageResponse:
    """Spread unassigned sectors across the connected scouts."""
    mission = await services.missions.get(session, mission_id)
    assignments = await services.search.auto_assign(session, mission, principal)
    return MessageResponse(
        message=f"{len(assignments)} sector(s) assigned",
        detail={"assignments": [{"sector": s, "drone_id": d} for s, d in assignments]},
    )


@router.post("/{sector_id}/assign", response_model=SectorResponse)
async def assign_sector(
    mission_id: uuid.UUID,
    sector_id: uuid.UUID,
    payload: SectorAssignRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> SectorResponse:
    sector = await services.search.get_sector(session, sector_id)
    sector = await services.search.assign(session, sector, payload.drone_id, principal)
    return _sector_response(sector)


@router.post("/{sector_id}/waypoints", response_model=list[WaypointResponse])
async def generate_waypoints(
    mission_id: uuid.UUID,
    sector_id: uuid.UUID,
    payload: WaypointGenerateRequest,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> list[WaypointResponse]:
    """Generate the lawnmower plan for a sector.

    Give either an explicit ``line_spacing_m`` or the camera
    ``horizontal_fov_deg``, from which the spacing is computed for the search
    altitude at the configured overlap.
    """
    sector = await services.search.get_sector(session, sector_id)
    altitude = payload.altitude_m or sector.search_altitude_m

    spacing = payload.line_spacing_m
    if spacing is None:
        if payload.horizontal_fov_deg is None or not altitude:
            raise ValidationError(
                "Provide line_spacing_m, or horizontal_fov_deg together with a "
                "search altitude, so the spacing actually covers the ground",
                details={"altitude_m": altitude},
            )
        spacing = estimate_line_spacing_m(
            altitude, payload.horizontal_fov_deg, payload.overlap_fraction
        )

    waypoints = await services.search.generate_waypoints(
        session,
        sector,
        line_spacing_m=spacing,
        altitude_m=payload.altitude_m,
        speed_mps=payload.speed_mps,
    )
    return [WaypointResponse.model_validate(w) for w in waypoints]


@router.post("/{sector_id}/start")
async def start_sector(
    mission_id: uuid.UUID,
    sector_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> dict:
    """Upload the sector plan to its aircraft and start it.

    The sector becomes IN_PROGRESS only when PX4 confirms the mission started.
    """
    sector = await services.search.get_sector(session, sector_id)
    return await services.search.upload_and_start(
        session, sector, principal, request_id=request_id
    )


@router.post("/{sector_id}/release", response_model=SectorResponse)
async def release_sector(
    mission_id: uuid.UUID,
    sector_id: uuid.UUID,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
    reason: Annotated[str, Query(min_length=3, max_length=255)] = "released by operator",
) -> SectorResponse:
    sector = await services.search.get_sector(session, sector_id)
    sector = await services.search.release(session, sector, reason)
    return _sector_response(sector)


@router.post("/sync-progress", response_model=MessageResponse)
async def sync_progress(
    mission_id: uuid.UUID,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MessageResponse:
    """Pull current sector coverage from live aircraft mission progress."""
    updated = await services.search.sync_progress(session, mission_id)
    return MessageResponse(
        message=f"{updated} sector(s) updated from live telemetry",
        detail={"updated": updated},
    )
