"""Geofence definition and upload.

The same polygon drives GCS supervision and the PX4 onboard fence, so the two
authorities cannot disagree about where the boundary is. PX4 enforces it in
the air; this backend only watches and warns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, status
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon as ShapelyPolygon
from sqlalchemy import select

from app.api.deps import (
    DbSession,
    OperatorPrincipal,
    RequestId,
    Services,
    ViewerPrincipal,
)
from app.core.enums import CommandType
from app.core.exceptions import NotFoundError, ValidationError
from app.core.geo import polygon_area_m2
from app.core.logging import get_logger
from app.models.mission import Geofence, MissionGeofence
from app.schemas.common import MessageResponse
from app.schemas.mission import GeofenceCreateRequest, GeofenceResponse

logger = get_logger(__name__)
router = APIRouter(prefix="/geofences", tags=["geofences"])


@router.get("", response_model=list[GeofenceResponse])
async def list_geofences(
    _: ViewerPrincipal, session: DbSession
) -> list[GeofenceResponse]:
    result = await session.execute(select(Geofence).order_by(Geofence.name))
    return [GeofenceResponse.model_validate(g) for g in result.scalars().all()]


@router.post("", response_model=GeofenceResponse, status_code=status.HTTP_201_CREATED)
async def create_geofence(
    payload: GeofenceCreateRequest,
    _: OperatorPrincipal,
    session: DbSession,
) -> GeofenceResponse:
    points = payload.boundary.as_tuples()
    area = polygon_area_m2(points)
    if area < 100:
        raise ValidationError(
            "Geofence polygon is smaller than 100 square metres; check the "
            "coordinate order and units",
            details={"area_m2": round(area, 1)},
        )

    fence = Geofence(
        name=payload.name,
        fence_type=payload.fence_type,
        boundary=from_shape(
            ShapelyPolygon([(lon, lat) for lat, lon in points]), srid=4326
        ),
        min_altitude_m=payload.min_altitude_m,
        max_altitude_m=payload.max_altitude_m,
        attributes={"area_m2": round(area, 1)},
    )
    session.add(fence)
    await session.flush()
    logger.info("geofence_created", name=payload.name, area_m2=round(area, 1))
    return GeofenceResponse.model_validate(fence)


@router.post("/{geofence_id}/attach/{mission_id}", response_model=MessageResponse)
async def attach_to_mission(
    geofence_id: uuid.UUID,
    mission_id: uuid.UUID,
    _: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> MessageResponse:
    fence = await session.get(Geofence, geofence_id)
    if fence is None:
        raise NotFoundError(
            f"Geofence {geofence_id} does not exist",
            details={"geofence_id": str(geofence_id)},
        )
    await services.missions.get(session, mission_id)

    existing = await session.execute(
        select(MissionGeofence).where(
            MissionGeofence.mission_id == mission_id,
            MissionGeofence.geofence_id == geofence_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(MissionGeofence(mission_id=mission_id, geofence_id=geofence_id))
        await session.flush()

    await services.geofence.load_for_mission(session, mission_id)
    return MessageResponse(
        message=f"Geofence {fence.name} attached to the mission",
        detail={"active_fences": len(services.geofence.fences_for(mission_id))},
    )


@router.post("/missions/{mission_id}/upload", response_model=dict)
async def upload_to_aircraft(
    mission_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> dict:
    """Push the mission fence to every assigned aircraft.

    Once uploaded, PX4 enforces the boundary onboard -- which is what keeps it
    effective when the link to the GCS is down.
    """
    await services.missions.get(session, mission_id)
    await services.geofence.load_for_mission(session, mission_id)
    specs = services.geofence.upload_specs(mission_id)
    if not specs:
        raise ValidationError(
            "No geofence is attached to this mission",
            details={"mission_id": str(mission_id)},
        )

    drone_ids = await services.missions.mission_drone_ids(session, mission_id)
    results = []
    for drone_id in drone_ids:
        try:
            outcome = await services.commands.execute(
                drone_id=drone_id,
                command_type=CommandType.UPLOAD_GEOFENCE,
                principal=principal,
                mission_id=mission_id,
                parameters={"polygons": specs},
                request_id=request_id,
                idempotency_key=f"{mission_id}:geofence-upload:{drone_id}",
            )
            results.append({"drone_id": drone_id, **outcome.as_dict()})
        except Exception as exc:
            results.append(
                {
                    "drone_id": drone_id,
                    "state": "FAILED",
                    "acknowledged": False,
                    "verified": False,
                    "detail": str(exc),
                }
            )

    accepted = [r for r in results if r.get("acknowledged")]
    if accepted:
        await session.execute(
            MissionGeofence.__table__.update()
            .where(MissionGeofence.mission_id == mission_id)
            .values(uploaded_to_vehicles=True, uploaded_at=datetime.now(UTC))
        )

    return {
        "mission_id": str(mission_id),
        "polygons": len(specs),
        "accepted_by": [r["drone_id"] for r in accepted],
        "results": results,
    }


@router.get("/missions/{mission_id}/geojson")
async def mission_geofence_geojson(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> dict:
    await services.geofence.load_for_mission(session, mission_id)
    return services.geofence.geojson(mission_id)
