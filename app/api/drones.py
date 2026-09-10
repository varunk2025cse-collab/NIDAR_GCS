"""Drone state, telemetry and per-aircraft commands.

Every command endpoint here goes through :class:`CommandService`, which means
every one is authenticated, authorised, rate-limited, precondition-checked,
idempotent, audited and verified against real telemetry.

There is deliberately no endpoint that forwards an arbitrary MAVLink message.
The command surface is exactly the set of operations below.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from geoalchemy2.shape import to_shape
from sqlalchemy import select

from app.api.deps import (
    ClientIp,
    DbSession,
    OperatorPrincipal,
    RequestId,
    Services,
    ViewerPrincipal,
    check_command_rate_limit,
)
from app.core.enums import CommandType
from app.core.logging import get_logger
from app.models.drone import Drone
from app.schemas.common import CommandAcceptedResponse
from app.schemas.drone import (
    CommandRecordResponse,
    CommandRequestBase,
    DroneCardResponse,
    DroneDetailResponse,
    DroneHealthResponse,
    DroneRegistryResponse,
    FleetResponse,
    GotoRequest,
    TakeoffRequest,
    TelemetryHistoryResponse,
    TelemetrySampleResponse,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/drones", tags=["drones"])


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
@router.get("", response_model=FleetResponse)
async def list_drones(_: ViewerPrincipal, services: Services) -> FleetResponse:
    """Live state of every configured aircraft."""
    return FleetResponse.model_validate(services.fleet.fleet_payload())


@router.get("/registry", response_model=list[DroneRegistryResponse])
async def drone_registry(
    _: ViewerPrincipal, session: DbSession
) -> list[DroneRegistryResponse]:
    """Persisted airframe records, including observed hardware identity."""
    result = await session.execute(select(Drone).order_by(Drone.drone_id))
    return [DroneRegistryResponse.model_validate(d) for d in result.scalars().all()]


@router.get("/{drone_id}", response_model=DroneDetailResponse)
async def get_drone(
    drone_id: str, _: ViewerPrincipal, services: Services
) -> DroneDetailResponse:
    return DroneDetailResponse.model_validate(services.fleet.snapshot(drone_id))


@router.get("/{drone_id}/card", response_model=DroneCardResponse)
async def get_drone_card(
    drone_id: str, _: ViewerPrincipal, services: Services
) -> DroneCardResponse:
    return DroneCardResponse.model_validate(services.fleet.card(drone_id))


@router.get("/{drone_id}/telemetry", response_model=DroneDetailResponse)
async def get_telemetry(
    drone_id: str, _: ViewerPrincipal, services: Services
) -> DroneDetailResponse:
    """Current telemetry, every field carrying its own freshness."""
    return DroneDetailResponse.model_validate(services.fleet.snapshot(drone_id))


@router.get("/{drone_id}/telemetry/history", response_model=TelemetryHistoryResponse)
async def telemetry_history(
    drone_id: str,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 15,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> TelemetryHistoryResponse:
    """Recorded samples, for the telemetry chart.

    These are the values that actually arrived from the flight controller;
    nothing is interpolated to fill gaps in the link.
    """
    drone_uuid = services.fleet.require_drone_uuid(drone_id)
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    samples = await services.telemetry.history(
        session, drone_uuid, since=since, limit=limit
    )

    items: list[TelemetrySampleResponse] = []
    for sample in samples:
        latitude = longitude = None
        if sample.position is not None:
            shape = to_shape(sample.position)
            latitude, longitude = shape.y, shape.x
        items.append(
            TelemetrySampleResponse(
                sampled_at=sample.sampled_at,
                latitude=latitude,
                longitude=longitude,
                relative_altitude_m=sample.relative_altitude_m,
                absolute_altitude_m=sample.absolute_altitude_m,
                ground_speed_mps=sample.ground_speed_mps,
                vertical_speed_mps=sample.vertical_speed_mps,
                heading_deg=sample.heading_deg,
                roll_deg=sample.roll_deg,
                pitch_deg=sample.pitch_deg,
                yaw_deg=sample.yaw_deg,
                battery_percent=sample.battery_percent,
                battery_voltage_v=sample.battery_voltage_v,
                battery_current_a=sample.battery_current_a,
                gps_fix_type=sample.gps_fix_type,
                satellites=sample.satellites,
                armed=sample.armed,
                in_air=sample.in_air,
                flight_mode=sample.flight_mode,
                freshness=sample.freshness,
            )
        )
    return TelemetryHistoryResponse(
        drone_id=drone_id.upper(),
        sample_count=len(items),
        since=since,
        until=datetime.now(UTC),
        samples=items,
    )


@router.get("/{drone_id}/health", response_model=DroneHealthResponse)
async def drone_health(
    drone_id: str, _: ViewerPrincipal, services: Services
) -> DroneHealthResponse:
    state = services.fleet.state(drone_id)
    policy = services.fleet.policy

    return DroneHealthResponse(
        drone_id=state.drone_id,
        connection_state=state.connection_state,
        identity_verified=state.identity_verified,
        health=policy.render("health", state.health),
        gps=policy.render("gps", state.gps),
        battery=policy.render("battery", state.battery),
        telemetry_freshness={
            stream: str(policy.status(stream, holder))
            for stream, holder in (
                ("position", state.position),
                ("velocity", state.velocity),
                ("attitude", state.attitude),
                ("battery", state.battery),
                ("gps", state.gps),
                ("health", state.health),
                ("flight_mode", state.flight_mode),
            )
        },
        link=services.fleet.connection_manager.get(drone_id).link_diagnostics(),
        active_alerts=[
            a for a in services.safety.active_alerts() if a["drone_id"] == state.drone_id
        ],
    )


@router.get("/{drone_id}/commands", response_model=list[CommandRecordResponse])
async def command_history(
    drone_id: str,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CommandRecordResponse]:
    drone_uuid = services.fleet.require_drone_uuid(drone_id)
    records = await services.commands.history(session, drone_uuid=drone_uuid, limit=limit)
    return [CommandRecordResponse.model_validate(r) for r in records]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def _run_command(
    *,
    services: Services,
    principal: OperatorPrincipal,
    drone_id: str,
    command_type: CommandType,
    payload: CommandRequestBase,
    request_id: str,
    source_ip: str | None,
    parameters: dict | None = None,
) -> CommandAcceptedResponse:
    check_command_rate_limit(services, principal, drone_id.upper())
    outcome = await services.commands.execute(
        drone_id=drone_id,
        command_type=command_type,
        principal=principal,
        mission_id=payload.mission_id,
        parameters=parameters,
        idempotency_key=payload.idempotency_key,
        request_id=request_id,
        source_ip=source_ip,
    )
    return CommandAcceptedResponse.model_validate(outcome.as_dict())


@router.post("/{drone_id}/arm", response_model=CommandAcceptedResponse)
async def arm(
    drone_id: str,
    payload: CommandRequestBase,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    """Arm the aircraft.

    Blocked unless battery, GPS, health and telemetry freshness all pass.
    """
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.ARM, payload=payload,
        request_id=request_id, source_ip=source_ip,
    )


@router.post("/{drone_id}/disarm", response_model=CommandAcceptedResponse)
async def disarm(
    drone_id: str,
    payload: CommandRequestBase,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    """Disarm the aircraft. Refused while it reports being airborne."""
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.DISARM, payload=payload,
        request_id=request_id, source_ip=source_ip,
    )


@router.post("/{drone_id}/takeoff", response_model=CommandAcceptedResponse)
async def takeoff(
    drone_id: str,
    payload: TakeoffRequest,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.TAKEOFF, payload=payload,
        request_id=request_id, source_ip=source_ip,
        parameters={"altitude_m": payload.altitude_m},
    )


@router.post("/{drone_id}/land", response_model=CommandAcceptedResponse)
async def land(
    drone_id: str,
    payload: CommandRequestBase,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.LAND, payload=payload,
        request_id=request_id, source_ip=source_ip,
    )


@router.post("/{drone_id}/rtl", response_model=CommandAcceptedResponse)
async def return_to_launch(
    drone_id: str,
    payload: CommandRequestBase,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    """Send the aircraft home.

    PX4 flies the return using its own configured RTL behaviour; the GCS only
    requests it and reports whether the aircraft entered the mode.
    """
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.RTL, payload=payload,
        request_id=request_id, source_ip=source_ip,
    )


@router.post("/{drone_id}/hold", response_model=CommandAcceptedResponse)
async def hold(
    drone_id: str,
    payload: CommandRequestBase,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    """Hold position.

    This is what the dashboard "manual control" affordance maps onto: it stops
    autonomous execution and parks the aircraft so a pilot can take over on
    the RC transmitter. The GCS does not offer stick-level control -- manual
    flight belongs on the transmitter, not on a network link.
    """
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.HOLD, payload=payload,
        request_id=request_id, source_ip=source_ip,
    )


@router.post("/{drone_id}/goto", response_model=CommandAcceptedResponse)
async def goto(
    drone_id: str,
    payload: GotoRequest,
    principal: OperatorPrincipal,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> CommandAcceptedResponse:
    """Fly to a specific point.

    The target is checked against the mission geofence before the command is
    sent, so the GCS does not ask an aircraft to fly somewhere PX4 will refuse.
    """
    if payload.mission_id is not None:
        services.geofence.require_inside(
            payload.mission_id, payload.latitude, payload.longitude, what="goto target"
        )
    return await _run_command(
        services=services, principal=principal, drone_id=drone_id,
        command_type=CommandType.GOTO, payload=payload,
        request_id=request_id, source_ip=source_ip,
        parameters={
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "absolute_altitude_m": payload.absolute_altitude_m,
            "yaw_deg": payload.yaw_deg if payload.yaw_deg is not None else float("nan"),
        },
    )


@router.get("/{drone_id}/trail")
async def drone_trail(
    drone_id: str,
    _: ViewerPrincipal,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=2000)] = 600,
) -> dict:
    """Live flight path as GeoJSON, built from received positions only."""
    return services.fleet.state(drone_id).trail_geojson(limit)


@router.get("/{drone_id}/persisted-track")
async def persisted_track(
    drone_id: str,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    mission_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 2000,
) -> dict:
    """Recorded flight path, so a reloaded dashboard sees the whole sortie."""
    drone_uuid = services.fleet.require_drone_uuid(drone_id)
    points = await services.telemetry.track(
        session, drone_uuid, mission_id=mission_id, limit=limit
    )
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[lon, lat] for lat, lon, _ in points],
        },
        "properties": {
            "drone_id": drone_id.upper(),
            "point_count": len(points),
            "from": points[0][2].isoformat() if points else None,
            "to": points[-1][2].isoformat() if points else None,
        },
    }
