"""Delivery task endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from geoalchemy2.shape import to_shape

from app.api.deps import (
    DbSession,
    OperatorPrincipal,
    RequestId,
    Services,
    ViewerPrincipal,
    companion_identity,
)
from app.core.enums import DeliveryConfirmationSource, DeliveryState
from app.core.exceptions import AuthorizationError, ConflictError
from app.core.logging import get_logger
from app.schemas.survivor import (
    DeliveryCancelRequest,
    DeliveryConfirmationRequest,
    DeliveryCreateRequest,
    DeliveryDetailResponse,
    DeliveryDispatchResponse,
    DeliveryEventResponse,
    DeliveryReleaseRequest,
    DeliverySafetyResponse,
    DeliveryTaskResponse,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/deliveries", tags=["deliveries"])


@router.get("", response_model=list[DeliveryTaskResponse])
async def list_deliveries(
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    state: Annotated[list[DeliveryState] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeliveryTaskResponse]:
    tasks = await services.deliveries.list_tasks(
        session, mission_id=mission_id, states=state, limit=limit, offset=offset
    )
    return [DeliveryTaskResponse.model_validate(t) for t in tasks]


@router.post(
    "", response_model=DeliveryDispatchResponse, status_code=status.HTTP_201_CREATED
)
async def create_delivery(
    payload: DeliveryCreateRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
) -> DeliveryDispatchResponse:
    """Create and (by default) dispatch a delivery for a confirmed survivor.

    The aircraft is chosen by the dispatcher from live telemetry unless one is
    named. If no aircraft can safely make the round trip with the configured
    reserve, this returns DELIVERY_REJECTED_UNSAFE with the numbers attached
    rather than launching and hoping.
    """
    survivor = await services.survivors.get(session, payload.survivor_id)
    mission = await services.missions.get(session, survivor.mission_id)

    if payload.dispatch_immediately:
        decision = await services.dispatcher.dispatch_for_survivor(
            session,
            mission,
            survivor,
            principal,
            request_id=request_id,
            preferred_drone_id=payload.drone_id,
        )
        return DeliveryDispatchResponse(
            task_code=decision.task_code or "",
            drone_id=decision.drone_id,
            dispatched=decision.dispatched,
            detail=decision.reason,
        )

    task = await services.deliveries.create_task(
        session,
        mission,
        survivor,
        priority=payload.priority,
        payload_type=payload.payload_type,
        payload_mass_g=payload.payload_mass_g,
        operator_id=principal.operator_id,
    )
    return DeliveryDispatchResponse(
        task_code=task.task_code,
        drone_id=None,
        dispatched=False,
        detail="Task created; not yet assigned to an aircraft",
    )


@router.get("/dispatcher/status")
async def dispatcher_status(_: ViewerPrincipal, services: Services) -> dict:
    """Which delivery aircraft are available, and their live battery state."""
    return {
        **services.dispatcher.status(),
        "recent_decisions": services.dispatcher.recent_decisions(),
    }


@router.get("/{task_id}", response_model=DeliveryDetailResponse)
async def get_delivery(
    task_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> DeliveryDetailResponse:
    task = await services.deliveries.get(session, task_id)
    survivor = await services.survivors.get(session, task.survivor_id)
    shape = to_shape(task.target_position)

    payload = DeliveryTaskResponse.model_validate(task).model_dump()
    payload.update(
        {
            "drone_id": (
                services.fleet.drone_id_for_uuid(task.drone_uuid)
                if task.drone_uuid
                else None
            ),
            "survivor_code": survivor.survivor_code,
            "target": {"latitude": shape.y, "longitude": shape.x},
            "events": [
                DeliveryEventResponse.model_validate(e).model_dump() for e in task.events
            ],
        }
    )
    return DeliveryDetailResponse.model_validate(payload)


@router.get("/{task_id}/safety", response_model=DeliverySafetyResponse)
async def delivery_safety(
    task_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    drone_id: str | None = None,
) -> DeliverySafetyResponse:
    """Evaluate, right now, whether an aircraft can safely make this delivery.

    Read-only. Useful before committing, and to show the operator exactly why
    a dispatch would be refused.
    """
    task = await services.deliveries.get(session, task_id)
    target = to_shape(task.target_position)

    resolved = drone_id or (
        services.fleet.drone_id_for_uuid(task.drone_uuid) if task.drone_uuid else None
    )
    if resolved is None:
        available = services.fleet.available_delivery_drones()
        if not available:
            raise ConflictError(
                "No delivery aircraft is connected and free to evaluate",
                details={"task_code": task.task_code},
            )
        resolved = available[0].drone_id

    state = services.fleet.state(resolved)
    report = services.deliveries.evaluate_safety(
        state, target.y, target.x, task.mission_id
    )
    return DeliverySafetyResponse.model_validate(report.as_dict())


@router.post("/{task_id}/dispatch", response_model=DeliveryDispatchResponse)
async def dispatch_delivery(
    task_id: uuid.UUID,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
    drone_id: str | None = None,
) -> DeliveryDispatchResponse:
    """Assign (if needed) and launch an existing delivery task."""
    task = await services.deliveries.get(session, task_id)
    if task.drone_uuid is None:
        if drone_id is None:
            available = services.fleet.available_delivery_drones()
            if not available:
                raise ConflictError(
                    "No delivery aircraft is available",
                    details={"task_code": task.task_code},
                )
            drone_id = available[0].drone_id
        await services.deliveries.assign(session, task, drone_id, principal)

    result = await services.deliveries.dispatch(
        session, task, principal, request_id=request_id
    )
    return DeliveryDispatchResponse.model_validate(result)


@router.post("/{task_id}/release")
async def release_payload(
    task_id: uuid.UUID,
    payload: DeliveryReleaseRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> dict:
    """Trigger the release and wait for physical confirmation.

    The task is only marked DELIVERED when a confirmation provider attests to
    an actual release. If nothing confirms within the timeout, the response
    says so and the task stays DELIVERY_INITIATED.
    """
    task = await services.deliveries.get(session, task_id)
    return await services.deliveries.initiate_release(
        session, task, principal, payload.confirmation_timeout_s
    )


@router.post("/{task_id}/confirm", response_model=DeliveryTaskResponse)
async def confirm_delivery(
    task_id: uuid.UUID,
    payload: DeliveryConfirmationRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> DeliveryTaskResponse:
    """Record operator confirmation that the payload was physically released."""
    if payload.source is not DeliveryConfirmationSource.OPERATOR:
        raise AuthorizationError(
            "Operators may only submit OPERATOR confirmations; machine sources "
            "must report through the companion endpoint",
            details={"source": str(payload.source)},
        )
    task = await services.deliveries.get(session, task_id)
    task = await services.deliveries.submit_confirmation(
        session,
        task,
        payload.source,
        payload.confirmed,
        payload.detail,
        operator_id=principal.operator_id,
        evidence=payload.evidence,
    )
    return DeliveryTaskResponse.model_validate(task)


@router.post("/{task_id}/confirm-onboard", response_model=DeliveryTaskResponse)
async def confirm_delivery_onboard(
    task_id: uuid.UUID,
    payload: DeliveryConfirmationRequest,
    services: Services,
    session: DbSession,
    authenticated_drone_id: Annotated[str, Depends(companion_identity)],
) -> DeliveryTaskResponse:
    """Confirmation from the aircraft itself.

    Used by the payload mechanism, the companion computer or an onboard
    sensor. Authenticated with the same per-drone key as detections, and the
    key must belong to the aircraft that is actually carrying this task.
    """
    if payload.source is DeliveryConfirmationSource.OPERATOR:
        raise AuthorizationError(
            "The onboard endpoint cannot submit OPERATOR confirmations",
            details={"source": str(payload.source)},
        )

    task = await services.deliveries.get(session, task_id)
    assigned = (
        services.fleet.drone_id_for_uuid(task.drone_uuid) if task.drone_uuid else None
    )
    if assigned != authenticated_drone_id:
        raise AuthorizationError(
            "This aircraft is not the one assigned to this delivery",
            details={
                "authenticated_drone_id": authenticated_drone_id,
                "assigned_drone_id": assigned,
            },
        )

    task = await services.deliveries.submit_confirmation(
        session,
        task,
        payload.source,
        payload.confirmed,
        payload.detail,
        evidence=payload.evidence,
    )
    return DeliveryTaskResponse.model_validate(task)


@router.post("/{task_id}/cancel", response_model=DeliveryTaskResponse)
async def cancel_delivery(
    task_id: uuid.UUID,
    payload: DeliveryCancelRequest,
    principal: OperatorPrincipal,
    services: Services,
    session: DbSession,
) -> DeliveryTaskResponse:
    """Cancel a delivery. The survivor returns to the pending queue."""
    task = await services.deliveries.get(session, task_id)
    task = await services.deliveries.cancel(
        session, task, payload.reason, principal.operator_id
    )
    return DeliveryTaskResponse.model_validate(task)


@router.get("/routes/{mission_id}")
async def delivery_routes(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
) -> dict:
    return await services.deliveries.routes_geojson(session, mission_id)
