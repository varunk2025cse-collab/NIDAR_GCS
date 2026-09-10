"""In-process event bus.

Everything that happens in the GCS -- telemetry arriving, a link dropping, a
survivor being confirmed, a command completing -- is published here. Services
subscribe; nothing polls another service.

Two guarantees matter for a supervisory system:

* **Ordering.** A single dispatcher task drains the queue, so every handler
  observes events in publish order. "Battery critical" can never be processed
  before the telemetry update that caused it.
* **Backpressure without stalling flight supervision.** A slow WebSocket
  client gets its oldest queued events dropped (and the drop is counted and
  logged); it never blocks the telemetry pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

EventHandler = Callable[["Event"], Awaitable[None]]


class EventType:
    """Canonical event names.

    These are the WebSocket contract; the frontend keys off these strings.
    """

    DRONE_CONNECTED = "DRONE_CONNECTED"
    DRONE_DISCONNECTED = "DRONE_DISCONNECTED"
    DRONE_STATE_UPDATED = "DRONE_STATE_UPDATED"
    DRONE_IDENTITY_VERIFIED = "DRONE_IDENTITY_VERIFIED"
    DRONE_IDENTITY_MISMATCH = "DRONE_IDENTITY_MISMATCH"
    TELEMETRY_UPDATED = "TELEMETRY_UPDATED"

    BATTERY_WARNING = "BATTERY_WARNING"
    GPS_WARNING = "GPS_WARNING"
    GEOFENCE_WARNING = "GEOFENCE_WARNING"
    ALERT_CREATED = "ALERT_CREATED"
    ALERT_CLEARED = "ALERT_CLEARED"

    SURVIVOR_DETECTED = "SURVIVOR_DETECTED"
    SURVIVOR_CONFIRMED = "SURVIVOR_CONFIRMED"
    SURVIVOR_UPDATED = "SURVIVOR_UPDATED"
    SURVIVOR_DUPLICATE_MERGED = "SURVIVOR_DUPLICATE_MERGED"
    DETECTION_REJECTED = "DETECTION_REJECTED"

    DELIVERY_ASSIGNED = "DELIVERY_ASSIGNED"
    DELIVERY_UPDATED = "DELIVERY_UPDATED"
    DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED"
    DELIVERY_REJECTED = "DELIVERY_REJECTED"

    MISSION_STATE_CHANGED = "MISSION_STATE_CHANGED"
    MISSION_TIME_WARNING = "MISSION_TIME_WARNING"
    SEARCH_SECTOR_UPDATED = "SEARCH_SECTOR_UPDATED"

    COMMAND_UPDATED = "COMMAND_UPDATED"
    SYSTEM_HEALTH_UPDATED = "SYSTEM_HEALTH_UPDATED"
    MISSION_EVENT = "MISSION_EVENT"


#: Logical channels a WebSocket client can subscribe to.
class Channel:
    FLEET = "fleet"
    EVENTS = "events"
    MISSION = "mission"
    DRONE = "drone"

    @staticmethod
    def mission(mission_id: str) -> str:
        return f"mission:{mission_id}"

    @staticmethod
    def drone(drone_id: str) -> str:
        return f"drone:{drone_id}"


_sequence = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    drone_id: str | None = None
    mission_id: str | None = None
    survivor_id: str | None = None
    delivery_task_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sequence: int = field(default_factory=lambda: next(_sequence))

    def channels(self) -> set[str]:
        """Channels this event should reach."""
        channels = {Channel.EVENTS}
        if self.type in _FLEET_EVENTS:
            channels.add(Channel.FLEET)
        if self.mission_id:
            channels.add(Channel.mission(self.mission_id))
        if self.drone_id:
            channels.add(Channel.drone(self.drone_id))
        return channels

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "drone_id": self.drone_id,
            "mission_id": self.mission_id,
            "survivor_id": self.survivor_id,
            "delivery_task_id": self.delivery_task_id,
            "payload": self.payload,
        }


_FLEET_EVENTS = frozenset(
    {
        EventType.DRONE_CONNECTED,
        EventType.DRONE_DISCONNECTED,
        EventType.DRONE_STATE_UPDATED,
        EventType.TELEMETRY_UPDATED,
        EventType.DRONE_IDENTITY_VERIFIED,
        EventType.DRONE_IDENTITY_MISMATCH,
        EventType.SYSTEM_HEALTH_UPDATED,
        EventType.ALERT_CREATED,
        EventType.ALERT_CLEARED,
    }
)


@dataclass
class Subscription:
    """A bounded queue of events for one consumer."""

    name: str
    queue: asyncio.Queue[Event]
    topics: set[str] | None = None
    dropped: int = 0

    def matches(self, event: Event) -> bool:
        return self.topics is None or event.type in self.topics

    def offer(self, event: Event) -> None:
        """Enqueue, dropping the oldest event if the consumer is behind."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)


class EventBus:
    """Ordered fan-out to in-process handlers and queue subscribers."""

    def __init__(self, queue_size: int = 2000, subscriber_queue_size: int = 500) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._subscriber_queue_size = subscriber_queue_size
        self._handlers: dict[str, list[EventHandler]] = {}
        self._wildcard_handlers: list[EventHandler] = []
        self._subscriptions: list[Subscription] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._published = 0
        self._dispatched = 0
        self._dropped = 0
        self._handler_errors = 0

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._dispatch_loop(), name="event-bus")
        logger.info("event_bus_started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info(
            "event_bus_stopped",
            published=self._published,
            dispatched=self._dispatched,
            dropped=self._dropped,
        )

    # -- registration ------------------------------------------------------
    def on(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def on_any(self, handler: EventHandler) -> None:
        self._wildcard_handlers.append(handler)

    def subscribe(self, name: str, topics: set[str] | None = None) -> Subscription:
        sub = Subscription(
            name=name,
            queue=asyncio.Queue(maxsize=self._subscriber_queue_size),
            topics=topics,
        )
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)

    # -- publishing --------------------------------------------------------
    def publish(self, event: Event) -> None:
        """Non-blocking publish.

        Callers are telemetry callbacks on the hot path; they must never wait
        on a consumer. If the bus itself is saturated the event is dropped and
        counted -- a visible, measurable loss rather than a stalled link.
        """
        self._published += 1
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning("event_bus_overflow", event_type=event.type, dropped=self._dropped)

    def emit(self, event_type: str, **kwargs: Any) -> Event:
        payload = kwargs.pop("payload", {})
        event = Event(type=event_type, payload=payload, **kwargs)
        self.publish(event)
        return event

    # -- dispatch ----------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "event_dispatch_failed", event_type=event.type, error=str(exc), exc_info=True
                )
            finally:
                self._queue.task_done()

    async def _dispatch(self, event: Event) -> None:
        self._dispatched += 1

        for sub in list(self._subscriptions):
            if sub.matches(event):
                sub.offer(event)

        handlers = [*self._handlers.get(event.type, []), *self._wildcard_handlers]
        for handler in handlers:
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One misbehaving handler must not stop the others: a failing
                # audit writer cannot be allowed to take down safety alerting.
                self._handler_errors += 1
                logger.error(
                    "event_handler_failed",
                    event_type=event.type,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    error=str(exc),
                    exc_info=True,
                )

    # -- introspection -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "published": self._published,
            "dispatched": self._dispatched,
            "dropped": self._dropped,
            "handler_errors": self._handler_errors,
            "queue_depth": self._queue.qsize(),
            "subscribers": len(self._subscriptions),
            "subscriber_drops": sum(s.dropped for s in self._subscriptions),
        }

    async def drain(self, timeout_s: float = 2.0) -> bool:
        """Wait until the queue is empty. Test and shutdown helper."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus | None) -> None:
    """Test hook for injecting a fresh bus."""
    global _bus
    _bus = bus
