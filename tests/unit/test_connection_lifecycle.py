"""Link lifecycle: identity verification, telemetry, staleness, loss.

These cover the acceptance criteria around connection state and telemetry
freshness detection, using the fake transport so the timing is deterministic.
Real-hardware equivalents are in docs/hardware-validation.md, stages 1-3.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.enums import ConnectionState
from app.core.exceptions import DroneNotConnectedError, DroneNotFoundError
from app.drone.identity import IdentityProbeUnsupported, translate_endpoint
from app.drone.manager import DroneConnectionManager
from app.realtime.event_bus import EventType
from tests.fakes import FakeAdapterRegistry


async def test_links_reach_connected_and_receive_real_telemetry(
    live_fleet, adapters: FakeAdapterRegistry
) -> None:
    for drone_id in ("D1", "D2", "D3"):
        state = live_fleet.state(drone_id)
        assert state.connection_state is ConnectionState.CONNECTED
        assert state.identity_verified is True
        # The values came off the link, not from a default.
        assert state.position.value is not None
        assert state.battery.value is not None
        assert state.position.value.latitude == adapters.get(drone_id).current_position.latitude


async def test_identity_is_taken_from_the_vehicle_not_the_label(live_fleet) -> None:
    state = live_fleet.state("D3")
    assert state.identity is not None
    assert state.identity.system_id == 3
    assert state.identity.hardware_uid == "FAKE-UID-D3"
    assert state.identity.firmware_version == "1.15.0"


async def test_drone_connected_event_is_published(
    connection_manager: DroneConnectionManager, collected_events
) -> None:
    await connection_manager.start()
    await asyncio.sleep(0.3)
    connected = [e for e in collected_events if e.type == EventType.DRONE_CONNECTED]
    assert {e.drone_id for e in connected} == {"D1", "D2", "D3"}
    assert all(e.payload["system_id"] for e in connected)


async def test_telemetry_going_quiet_degrades_then_disconnects(
    live_fleet, adapters: FakeAdapterRegistry, collected_events
) -> None:
    """Silence is the only evidence of link loss the GCS has."""
    state = live_fleet.state("D1")
    assert state.connection_state is ConnectionState.CONNECTED

    adapters.make_unreachable("D1")

    # heartbeat_degraded_s is 0.3 in the test settings.
    await asyncio.sleep(0.5)
    assert state.connection_state is ConnectionState.DEGRADED

    # heartbeat_lost_s is 0.8. The aircraft stays unreachable, so the
    # supervisor keeps retrying rather than silently recovering.
    await asyncio.sleep(0.8)
    assert state.connection_state in (
        ConnectionState.DISCONNECTED,
        ConnectionState.CONNECTING,
        ConnectionState.IDENTIFYING,
        ConnectionState.ERROR,
    )

    disconnects = [e for e in collected_events if e.type == EventType.DRONE_DISCONNECTED]
    assert any(e.drone_id == "D1" for e in disconnects)


async def test_link_loss_clears_live_values_but_keeps_last_known_position(
    live_fleet, adapters: FakeAdapterRegistry
) -> None:
    """The dangerous failure would be leaving a stale marker on the map as
    though the aircraft were still there."""
    state = live_fleet.state("D1")
    original = state.position.value
    assert original is not None

    adapters.make_unreachable("D1")
    await asyncio.sleep(1.4)

    assert state.position.value is None, "live position must be dropped on link loss"
    assert state.battery.value is None
    assert state.last_known_position is not None
    assert state.last_known_position.latitude == original.latitude
    assert state.last_contact_at is not None


async def test_disconnect_event_carries_loss_context(
    live_fleet, adapters: FakeAdapterRegistry, collected_events
) -> None:
    adapters.make_unreachable("D2")
    await asyncio.sleep(1.4)

    events = [
        e
        for e in collected_events
        if e.type == EventType.DRONE_DISCONNECTED and e.drone_id == "D2"
    ]
    assert events, "a disconnect must be announced"
    payload = events[0].payload
    assert payload["last_seen"] is not None
    assert payload["loss_time"] is not None
    assert payload["last_position"] is not None
    assert "reason" in payload


async def test_configured_fleet_is_visible_before_any_link_is_up(offline_fleet) -> None:
    """The dashboard must show three aircraft as offline, not report them
    unknown, when nothing is powered on yet."""
    states = offline_fleet.states
    assert set(states) == {"D1", "D2", "D3"}
    assert all(s.connection_state is ConnectionState.DISCOVERING for s in states.values())
    assert offline_fleet.summary()["total"] == 3
    assert offline_fleet.summary()["online"] == 0


async def test_commands_are_refused_when_not_connected(offline_fleet) -> None:
    with pytest.raises(DroneNotConnectedError) as exc:
        offline_fleet.connection_manager.require_adapter("D1")
    assert exc.value.details["drone_id"] == "D1"
    assert exc.value.code == "DRONE_NOT_CONNECTED"


async def test_unknown_drone_is_rejected(offline_fleet) -> None:
    with pytest.raises(DroneNotFoundError):
        offline_fleet.state("D9")


async def test_degraded_link_is_not_commandable(live_fleet, adapters) -> None:
    """A degraded link means we cannot confirm what the aircraft is doing, so
    routine commands are refused rather than sent hopefully."""
    state = live_fleet.state("D1")
    adapters.make_unreachable("D1")
    await asyncio.sleep(0.5)

    assert state.connection_state is ConnectionState.DEGRADED
    assert state.is_connected is True
    assert state.is_commandable is False


async def test_fleet_summary_counts_real_link_states(live_fleet, adapters) -> None:
    summary = live_fleet.summary()
    assert summary["total"] == 3
    assert summary["online"] == 3
    assert summary["identity_verified"] == 3

    adapters.make_unreachable("D1")
    await asyncio.sleep(0.5)
    assert live_fleet.summary()["degraded"] >= 1


# ---------------------------------------------------------------------------
# identity probe endpoint translation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("udpin://0.0.0.0:14541", "udpin:0.0.0.0:14541"),
        ("udp://0.0.0.0:14540", "udpin:0.0.0.0:14540"),
        ("tcpin://:5760", "tcpin:0.0.0.0:5760"),
        ("tcpout://192.168.1.20:5760", "tcp:192.168.1.20:5760"),
        ("serial:///dev/ttyUSB0:57600", "/dev/ttyUSB0,57600"),
    ],
)
def test_endpoint_translation(endpoint: str, expected: str) -> None:
    assert translate_endpoint(endpoint) == expected


def test_outbound_udp_cannot_be_probed_passively() -> None:
    """Probing must never transmit, so an outbound-only endpoint is refused
    rather than silently skipping identity verification."""
    with pytest.raises(IdentityProbeUnsupported):
        translate_endpoint("udpout://192.168.1.20:14550")


def test_unknown_endpoint_scheme_is_rejected() -> None:
    with pytest.raises(IdentityProbeUnsupported):
        translate_endpoint("carrier-pigeon://somewhere")
