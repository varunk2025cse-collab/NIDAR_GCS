"""Survivor records and the companion-computer detection intake."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from geoalchemy2.shape import to_shape

from app.ai.survivor_event_ingestion import DetectionEvent
from app.api.deps import (
    DbSession,
    OperatorPrincipal,
    Services,
    ViewerPrincipal,
    companion_identity,
)
from app.core.enums import SurvivorState
from app.core.exceptions import AuthorizationError
from app.core.logging import get_logger
from app.schemas.survivor import (
    DeliveryTaskResponse,
    DetectionEventRequest,
    DetectionIngestResponse,
    DetectionRecordResponse,
    SurvivorConfirmRequest,
    SurvivorDetailResponse,
    SurvivorDuplicateRequest,
    SurvivorRejectRequest,
    SurvivorResponse,
    SurvivorSummaryResponse,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/survivors", tags=["survivors"])


@router.post(
    "/events",
    response_model=DetectionIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_detection(
    payload: DetectionEventRequest,
    services: Services,
    session: DbSession,
    authenticated_drone_id: Annotated[str, Depends(companion_identity)],
) -> DetectionIngestResponse:
    """Receive a detection from an onboard perception system.

    Authenticated with a per-drone companion key. The authenticated drone_id
    wins over anything in the body, so a compromised scout cannot post
    detections attributed to another aircraft.

    A rejected detection is recorded with its reason rather than discarded --
    a perception stack that starts producing rejects is a fault worth seeing.
    """
    if payload.drone_id.upper() != authenticated_drone_id:
        logger.warning(
            "detection_drone_id_mismatch",
            authenticated=authenticated_drone_id,
            claimed=payload.drone_id,
        )
        raise AuthorizationError(
            "Detection drone_id does not match the authenticated aircraft",
            details={
                "authenticated_drone_id": authenticated_drone_id,
                "claimed_drone_id": payload.drone_id,
            },
        )

    event = DetectionEvent(
        drone_id=authenticated_drone_id,
        detection_id=payload.detection_id,
        timestamp=payload.timestamp,
        confidence=payload.confidence,
        latitude=payload.latitude,
        longitude=payload.longitude,
        source=payload.source,
        pixel_x=payload.pixel_x,
        pixel_y=payload.pixel_y,
        image_width=payload.image_width,
        image_height=payload.image_height,
        horizontal_fov_deg=payload.horizontal_fov_deg,
        vertical_fov_deg=payload.vertical_fov_deg,
        camera_pitch_deg=payload.camera_pitch_deg,
        reported_accuracy_m=payload.estimated_accuracy_m,
        model_name=payload.model_name,
        model_version=payload.model_version,
        image_reference=payload.image_reference,
        camera_metadata=payload.camera_metadata,
        extra=payload.extra,
    )
    result = await services.ingestion.ingest(session, event)
    return DetectionIngestResponse.model_validate(result.as_dict())


@router.get("", response_model=list[SurvivorResponse])
async def list_survivors(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    state: Annotated[list[SurvivorState] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SurvivorResponse]:
    survivors = await services.survivors.list_survivors(
        session, mission_id=mission_id, states=state, limit=limit, offset=offset
    )
    return [SurvivorResponse.model_validate(s) for s in survivors]


@router.get("/summary", response_model=SurvivorSummaryResponse)
async def survivor_summary(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> SurvivorSummaryResponse:
    """Counts for the dashboard.

    ``found`` excludes duplicates and rejected records, so the number means
    distinct people located.
    """
    return SurvivorSummaryResponse.model_validate(
        await services.survivors.summary(session, mission_id)
    )


@router.get("/{survivor_id}", response_model=SurvivorDetailResponse)
async def get_survivor(
    survivor_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> SurvivorDetailResponse:
    """One survivor, with the detections that produced it."""
    survivor = await services.survivors.get(session, survivor_id)
    detections = await services.survivors.detections_for(session, survivor_id)
    tasks = await services.deliveries.tasks_for_survivor(session, survivor_id)
    shape = to_shape(survivor.location)

    payload = SurvivorResponse.model_validate(survivor).model_dump()
    payload.update(
        {
            "latitude": shape.y,
            "longitude": shape.x,
            "detections": [
                DetectionRecordResponse.model_validate(d).model_dump() for d in detections
            ],
            "delivery_tasks": [
                DeliveryTaskResponse.model_validate(t).model_dump() for t in tasks
            ],
        }
    )
    return SurvivorDetailResponse.model_validate(payload)


@router.get("/{survivor_id}/detections", response_model=list[DetectionRecordResponse])
async def survivor_detections(
    survivor_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> list[DetectionRecordResponse]:
    """The raw evidence behind a survivor record."""
    detections = await services.survivors.detections_for(session, survivor_id)
    return [DetectionRecordResponse.model_validate(d) for d in detections]


@router.post("/{survivor_id}/confirm", response_model=SurvivorResponse)
async def confirm_survivor(
    survivor_id: uuid.UUID,
    payload: SurvivorConfirmRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> SurvivorResponse:
    """Operator confirmation, for a survivor the evidence rules could not
    auto-confirm (low confidence, or a position accuracy too poor to fly to)."""
    survivor = await services.survivors.get(session, survivor_id)
    survivor = await services.survivors.confirm_manually(
        session, survivor, principal.operator_id, payload.notes
    )
    return SurvivorResponse.model_validate(survivor)


@router.post("/{survivor_id}/reject", response_model=SurvivorResponse)
async def reject_survivor(
    survivor_id: uuid.UUID,
    payload: SurvivorRejectRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> SurvivorResponse:
    survivor = await services.survivors.get(session, survivor_id)
    survivor = await services.survivors.reject(
        session, survivor, payload.reason, principal.operator_id
    )
    return SurvivorResponse.model_validate(survivor)


@router.post("/{survivor_id}/duplicate", response_model=SurvivorResponse)
async def mark_duplicate(
    survivor_id: uuid.UUID,
    payload: SurvivorDuplicateRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> SurvivorResponse:
    """Merge two records that turned out to be the same person.

    The detections move to the primary record, so no evidence is lost.
    """
    survivor = await services.survivors.get(session, survivor_id)
    primary = await services.survivors.get(session, payload.primary_survivor_id)
    survivor = await services.survivors.mark_duplicate(
        session, survivor, primary, principal.operator_id
    )
    return SurvivorResponse.model_validate(survivor)


@router.get("/geojson/{mission_id}")
async def survivors_geojson(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> dict:
    return await services.survivors.geojson(session, mission_id)
