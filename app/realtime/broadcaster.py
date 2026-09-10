"""Bridges the internal event bus to connected WebSocket clients.

Also drives the periodic fleet frame: the dashboard needs a complete picture
on a steady cadence, not only on change, so a client that connects mid-mission
sees the whole fleet immediately rather than waiting for something to move.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.realtime.event_bus import Channel, Event, EventBus, EventType
from app.realtime.websocket_manager import WebSocketManager
from app.services.fleet_manager import FleetManager

logger = get_logger(__name__)


class Broadcaster:
    def __init__(
        self,
        bus: EventBus,
        websockets: WebSocketManager,
        fleet: FleetManager,
        settings: Settings,
    ) -> None:
        self._bus = bus
        self._ws = websockets
        self._fleet = fleet
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._forwarded = 0

    def register(self) -> None:
        """Forward every bus event to the sockets subscribed to it."""
        self._bus.on_any(self._forward)

    async def _forward(self, event: Event) -> None:
        message = event.to_wire()
        delivered = self._ws.broadcast(event.channels(), message)
        self._forwarded += 1
        if delivered and event.type not in _HIGH_RATE_EVENTS:
            logger.debug("event_broadcast", event_type=event.type, clients=delivered)

    # ------------------------------------------------------------------
    # periodic fleet frame
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="fleet-broadcaster")
        logger.info(
            "broadcaster_started", interval_s=self._settings.fleet_broadcast_interval_s
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        interval = self._settings.fleet_broadcast_interval_s
        while self._running:
            try:
                if self._ws.client_count:
                    self._ws.broadcast(
                        {Channel.FLEET},
                        {
                            "type": EventType.DRONE_STATE_UPDATED,
                            "occurred_at": datetime.now(UTC).isoformat(),
                            "payload": self._fleet.fleet_payload(),
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("fleet_broadcast_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval)

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "events_forwarded": self._forwarded,
            "websocket": self._ws.stats(),
        }


#: Events that fire many times a second; not logged individually.
_HIGH_RATE_EVENTS = frozenset(
    {EventType.TELEMETRY_UPDATED, EventType.DRONE_STATE_UPDATED}
)
