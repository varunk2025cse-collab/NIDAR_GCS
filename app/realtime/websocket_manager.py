"""WebSocket connection management.

Clients authenticate with the same bearer token as the REST API before they
receive anything: a socket that streams live aircraft positions is not a
public endpoint.

Each client gets its own bounded outbound queue. A browser tab that stops
reading gets its oldest frames dropped and is eventually disconnected; it can
never slow down the telemetry pipeline behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.security import TokenPrincipal, decode_access_token

logger = get_logger(__name__)

#: Frames buffered per client before the oldest are dropped.
CLIENT_QUEUE_SIZE = 250
#: Consecutive send failures before a client is dropped.
MAX_SEND_FAILURES = 3


@dataclass
class Client:
    client_id: str
    websocket: WebSocket
    principal: TokenPrincipal
    channels: set[str]
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent: int = 0
    dropped: int = 0
    failures: int = 0

    def offer(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)


class WebSocketManager:
    def __init__(self) -> None:
        self._clients: dict[str, Client] = {}
        self._writers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._total_connections = 0
        self._send_failures = 0
        self._messages_sent = 0

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def authenticate(token: str | None) -> TokenPrincipal:
        """Validate the bearer token supplied on the socket handshake."""
        if not token:
            raise AuthenticationError("A token is required to open a WebSocket")
        return decode_access_token(token)

    async def connect(
        self, websocket: WebSocket, principal: TokenPrincipal, channels: set[str]
    ) -> Client:
        await websocket.accept()
        client = Client(
            client_id=str(uuid.uuid4()),
            websocket=websocket,
            principal=principal,
            channels=channels,
        )
        async with self._lock:
            self._clients[client.client_id] = client
            self._total_connections += 1
            self._writers[client.client_id] = asyncio.create_task(
                self._writer(client), name=f"ws-writer-{client.client_id[:8]}"
            )
        logger.info(
            "websocket_connected",
            client_id=client.client_id,
            operator=principal.username,
            role=str(principal.role),
            channels=sorted(channels),
        )
        await self.send_to(
            client,
            {
                "type": "CONNECTION_ESTABLISHED",
                "payload": {
                    "client_id": client.client_id,
                    "channels": sorted(channels),
                    "operator": principal.username,
                    "role": str(principal.role),
                    "server_time": datetime.now(UTC).isoformat(),
                },
            },
        )
        return client

    async def disconnect(self, client: Client, reason: str = "closed") -> None:
        async with self._lock:
            self._clients.pop(client.client_id, None)
            writer = self._writers.pop(client.client_id, None)
        if writer is not None:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        with contextlib.suppress(Exception):
            if client.websocket.client_state is WebSocketState.CONNECTED:
                await client.websocket.close()
        logger.info(
            "websocket_disconnected",
            client_id=client.client_id,
            reason=reason,
            sent=client.sent,
            dropped=client.dropped,
        )

    async def disconnect_all(self) -> None:
        for client in list(self._clients.values()):
            await self.disconnect(client, reason="server shutdown")

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    async def _writer(self, client: Client) -> None:
        """One task per client, draining its queue to the socket."""
        try:
            while True:
                message = await client.queue.get()
                try:
                    await client.websocket.send_json(message)
                    client.sent += 1
                    self._messages_sent += 1
                    client.failures = 0
                except Exception as exc:
                    client.failures += 1
                    self._send_failures += 1
                    logger.warning(
                        "websocket_send_failed",
                        client_id=client.client_id,
                        failures=client.failures,
                        error=str(exc),
                    )
                    if client.failures >= MAX_SEND_FAILURES:
                        await self.disconnect(client, reason="repeated send failures")
                        return
        except asyncio.CancelledError:
            raise

    async def send_to(self, client: Client, message: dict[str, Any]) -> None:
        client.offer(message)

    def broadcast(self, channels: set[str], message: dict[str, Any]) -> int:
        """Fan a message out to every client on any of these channels.

        Synchronous and non-blocking: it only enqueues. Called from the event
        bus dispatcher, which must not wait on network I/O.
        """
        delivered = 0
        for client in self._clients.values():
            if client.channels & channels:
                client.offer(message)
                delivered += 1
        return delivered

    def broadcast_all(self, message: dict[str, Any]) -> int:
        for client in self._clients.values():
            client.offer(message)
        return len(self._clients)

    # ------------------------------------------------------------------
    # inbound
    # ------------------------------------------------------------------
    async def handle_client_message(
        self, client: Client, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle the small inbound protocol.

        Deliberately tiny: subscribe, unsubscribe, ping. There is no command
        channel over the WebSocket -- anything that moves an aircraft goes
        through the audited REST endpoints.
        """
        action = str(message.get("action", "")).lower()

        if action == "ping":
            return {"type": "PONG", "payload": {"server_time": datetime.now(UTC).isoformat()}}
        if action == "subscribe":
            channels = {str(c) for c in message.get("channels", [])}
            client.channels |= channels
            return {
                "type": "SUBSCRIBED",
                "payload": {"channels": sorted(client.channels)},
            }
        if action == "unsubscribe":
            channels = {str(c) for c in message.get("channels", [])}
            client.channels -= channels
            return {
                "type": "UNSUBSCRIBED",
                "payload": {"channels": sorted(client.channels)},
            }
        return {
            "type": "ERROR",
            "payload": {
                "code": "UNSUPPORTED_ACTION",
                "message": (
                    f"Action {action!r} is not supported. This socket carries "
                    "telemetry only; commands go through the REST API."
                ),
            },
        }

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "clients": len(self._clients),
            "total_connections": self._total_connections,
            "messages_sent": self._messages_sent,
            "send_failures": self._send_failures,
            "dropped_frames": sum(c.dropped for c in self._clients.values()),
            "channels": sorted({c for client in self._clients.values()
                                for c in client.channels}),
        }

    @property
    def client_count(self) -> int:
        return len(self._clients)
