"""Event bus.

Two properties matter for a supervisory system: handlers see events in
publish order, and a slow consumer degrades itself rather than the telemetry
pipeline behind it.
"""

from __future__ import annotations

import asyncio

from app.realtime.event_bus import Channel, Event, EventBus, EventType


async def test_handlers_receive_events_in_publish_order(bus: EventBus) -> None:
    """Ordering is what stops "battery critical" being processed before the
    telemetry update that caused it."""
    seen: list[int] = []

    async def handler(event: Event) -> None:
        seen.append(event.payload["n"])

    bus.on("TEST", handler)
    for n in range(50):
        bus.emit("TEST", payload={"n": n})
    await bus.drain()

    assert seen == list(range(50))


async def test_a_failing_handler_does_not_stop_the_others(bus: EventBus) -> None:
    """A broken audit writer must not take down safety alerting."""
    delivered: list[str] = []

    async def broken(event: Event) -> None:
        raise RuntimeError("handler is broken")

    async def working(event: Event) -> None:
        delivered.append(event.type)

    bus.on("TEST", broken)
    bus.on("TEST", working)
    bus.emit("TEST")
    await bus.drain()

    assert delivered == ["TEST"]
    assert bus.stats()["handler_errors"] == 1


async def test_wildcard_handlers_see_everything(bus: EventBus) -> None:
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.type)

    bus.on_any(handler)
    bus.emit("A")
    bus.emit("B")
    await bus.drain()

    assert seen == ["A", "B"]


async def test_subscriptions_filter_by_topic(bus: EventBus) -> None:
    subscription = bus.subscribe("test", topics={"WANTED"})
    bus.emit("WANTED")
    bus.emit("UNWANTED")
    await bus.drain()

    assert subscription.queue.qsize() == 1
    assert subscription.queue.get_nowait().type == "WANTED"


async def test_slow_subscriber_drops_oldest_and_keeps_running(bus: EventBus) -> None:
    """Backpressure policy: a browser tab that stops reading loses its oldest
    frames; it never blocks the link."""
    subscription = bus.subscribe("slow")
    subscription.queue = asyncio.Queue(maxsize=5)

    for n in range(20):
        bus.emit("TEST", payload={"n": n})
    await bus.drain()

    assert subscription.queue.qsize() == 5
    assert subscription.dropped == 15
    # The newest events survived; the oldest were dropped.
    remaining = [subscription.queue.get_nowait().payload["n"] for _ in range(5)]
    assert remaining == [15, 16, 17, 18, 19]


async def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    subscription = bus.subscribe("temp")
    bus.unsubscribe(subscription)
    bus.emit("TEST")
    await bus.drain()
    assert subscription.queue.qsize() == 0


def test_events_route_to_the_right_channels() -> None:
    event = Event(
        type=EventType.TELEMETRY_UPDATED, drone_id="D1", mission_id="m-1"
    )
    channels = event.channels()
    assert Channel.FLEET in channels
    assert Channel.EVENTS in channels
    assert Channel.drone("D1") in channels
    assert Channel.mission("m-1") in channels


def test_non_fleet_events_stay_off_the_fleet_channel() -> None:
    event = Event(type=EventType.SURVIVOR_CONFIRMED, mission_id="m-1")
    channels = event.channels()
    assert Channel.FLEET not in channels
    assert Channel.EVENTS in channels
    assert Channel.mission("m-1") in channels


def test_sequence_numbers_are_monotonic() -> None:
    first = Event(type="A")
    second = Event(type="B")
    assert second.sequence > first.sequence


def test_wire_format_carries_correlation_ids() -> None:
    event = Event(
        type=EventType.DELIVERY_UPDATED,
        drone_id="D3",
        mission_id="m-1",
        survivor_id="s-1",
        delivery_task_id="t-1",
        payload={"task_code": "DLV-001"},
    )
    wire = event.to_wire()
    assert wire["type"] == "DELIVERY_UPDATED"
    assert wire["drone_id"] == "D3"
    assert wire["survivor_id"] == "s-1"
    assert wire["delivery_task_id"] == "t-1"
    assert wire["payload"]["task_code"] == "DLV-001"
    assert wire["occurred_at"].endswith("+00:00")


async def test_bus_overflow_is_counted_not_hidden() -> None:
    """If the bus itself saturates, the loss is visible in the stats rather
    than silently discarded."""
    small = EventBus(queue_size=4)
    for _ in range(20):
        small.emit("TEST")
    stats = small.stats()
    assert stats["published"] == 20
    assert stats["dropped"] > 0


async def test_stats_reflect_activity(bus: EventBus) -> None:
    bus.emit("TEST")
    await bus.drain()
    stats = bus.stats()
    assert stats["running"] is True
    assert stats["published"] >= 1
    assert stats["dispatched"] >= 1
