"""WebSocket endpoints.

Four channels, all read-only. The socket carries state *out*; anything that
changes the state of an aircraft goes through the audited REST endpoints, so
there is no path by which a WebSocket frame can command a drone.

Authentication is by the same bearer token as the REST API, supplied either as
a ``token`` query parameter or in the first frame.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_container
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.realtime.event_bus import Channel

logger = get_logger(__name__)
router = APIRouter(tags=["websocket"])

#: Closed by the client policy-violation code when authentication fails.
WS_POLICY_VIOLATION = 1008


async def _serve(
    websocket: WebSocket, channels: set[str], token: str | None, initial: Any = None
) -> None:
    container = get_container(websocket)  # type: ignore[arg-type]
    try:
        principal = container.websockets.authenticate(token)
    except AuthenticationError as exc:
        # Accept then immediately close so the browser sees a clean reason
        # rather than an opaque handshake failure.
        await websocket.accept()
        await websocket.send_json(
            {"type": "ERROR", "payload": {"code": "AUTHENTICATION_FAILED",
                                          "message": exc.message}}
        )
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    client = await container.websockets.connect(websocket, principal, channels)
    try:
        if initial is not None:
            await container.websockets.send_to(client, initial)

        while True:
            message = await websocket.receive_json()
            reply = await container.websockets.handle_client_message(client, message)
            if reply is not None:
                await container.websockets.send_to(client, reply)
    except WebSocketDisconnect:
        await container.websockets.disconnect(client, reason="client disconnected")
    except asyncio.CancelledError:
        await container.websockets.disconnect(client, reason="server shutdown")
        raise
    except Exception as exc:
        logger.warning("websocket_error", client_id=client.client_id, error=str(exc))
        with contextlib.suppress(Exception):
            await container.websockets.disconnect(client, reason=f"error: {exc}")


@router.websocket("/ws/fleet")
async def ws_fleet(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Live fleet state: connection, telemetry, alerts, health.

    Sends a full fleet frame on connect so a client is immediately correct,
    then updates as they happen plus a periodic full frame.
    """
    container = get_container(websocket)  # type: ignore[arg-type]
    initial = {
        "type": "FLEET_SNAPSHOT",
        "payload": container.fleet.fleet_payload(),
    }
    await _serve(websocket, {Channel.FLEET, Channel.EVENTS}, token, initial)


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Every event on the bus: survivors, deliveries, missions, alerts."""
    await _serve(websocket, {Channel.EVENTS}, token)


@router.websocket("/ws/mission/{mission_id}")
async def ws_mission(
    websocket: WebSocket, mission_id: str, token: str | None = Query(default=None)
) -> None:
    """Events scoped to one mission."""
    await _serve(
        websocket, {Channel.mission(mission_id), Channel.EVENTS}, token
    )


@router.websocket("/ws/drone/{drone_id}")
async def ws_drone(
    websocket: WebSocket, drone_id: str, token: str | None = Query(default=None)
) -> None:
    """Telemetry and events for one aircraft."""
    container = get_container(websocket)  # type: ignore[arg-type]
    drone_id = drone_id.upper()

    initial: dict[str, Any] | None = None
    if container.fleet.connection_manager.has(drone_id):
        initial = {
            "type": "DRONE_SNAPSHOT",
            "payload": container.fleet.snapshot(drone_id),
        }
    await _serve(websocket, {Channel.drone(drone_id)}, token, initial)
