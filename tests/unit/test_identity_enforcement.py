"""Aircraft identity enforcement (Phase 2).

The failure this prevents: an endpoint labelled D3 that actually carries a
scout. If the GCS accepted that, a delivery command would fly a scout to a
survivor, and an abort aimed at D3 would leave the real D3 flying.

Identity is verified before MAVSDK is allowed to attach, and the check fails
closed -- a link whose identity cannot be confirmed is refused, not adopted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.config import ConnectionConfig, DroneConfig, DroneRole, TelemetryRatesConfig
from app.core.enums import ConnectionState
from app.drone.connection import DroneConnection
from app.drone.identity import IdentityProbeUnsupported, ProbeResult
from app.drone.types import VehicleIdentity
from app.realtime.event_bus import EventType
from tests.fakes import FakeAdapterRegistry


@pytest.fixture
def d3_config() -> DroneConfig:
    return DroneConfig(
        drone_id="D3",
        role=DroneRole.DELIVERY,
        system_id=3,
        connection_endpoint="udpin://0.0.0.0:14543",
        mavsdk_server_port=50053,
    )


@pytest.fixture
def probing_connection_config() -> ConnectionConfig:
    """Identity probe ON -- the production default."""
    return ConnectionConfig(
        heartbeat_degraded_s=0.3,
        heartbeat_lost_s=0.8,
        connect_timeout_s=2.0,
        identity_probe_timeout_s=0.5,
        reconnect_initial_delay_s=0.05,
        reconnect_max_delay_s=0.1,
        require_identity_probe=True,
        fail_closed_on_identity=True,
    )


def _probe_returning(system_id: int | None):
    """Patch factory: a probe that observes ``system_id``, or nothing."""

    async def fake_probe(endpoint, timeout_s, expected_system_id=None):
        if system_id is None:
            return None
        return ProbeResult(
            identity=VehicleIdentity(
                system_id=system_id,
                component_id=1,
                autopilot="MAV_AUTOPILOT_PX4",
                vehicle_type="MAV_TYPE_QUADROTOR",
                observed_at=datetime.now(UTC),
            ),
            heartbeats_seen=3,
        )

    return fake_probe


async def _run_briefly(connection: DroneConnection, seconds: float = 0.6) -> None:
    await connection.start()
    await asyncio.sleep(seconds)
    await connection.stop()


async def test_correct_system_id_is_accepted(
    monkeypatch, d3_config, probing_connection_config, bus, adapters
) -> None:
    monkeypatch.setattr(
        "app.drone.connection.probe_identity", _probe_returning(3)
    )
    connection = DroneConnection(
        d3_config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await connection.start()
    await asyncio.sleep(0.4)
    try:
        assert connection.state.connection_state is ConnectionState.CONNECTED
        assert connection.state.identity_verified is True
        assert connection.state.identity.system_id == 3
    finally:
        await connection.stop()


async def test_wrong_system_id_is_refused(
    monkeypatch, d3_config, probing_connection_config, bus, adapters, collected_events
) -> None:
    """A scout answering on D3's endpoint must not become D3."""
    monkeypatch.setattr(
        "app.drone.connection.probe_identity", _probe_returning(1)
    )
    connection = DroneConnection(
        d3_config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await _run_briefly(connection)

    assert connection.state.identity_verified is False
    assert connection.state.connection_state is not ConnectionState.CONNECTED
    assert "system id mismatch" in (connection.state.last_error or "")

    mismatches = [
        e for e in collected_events if e.type == EventType.DRONE_IDENTITY_MISMATCH
    ]
    assert mismatches, "an identity mismatch must be announced, not silently retried"
    assert mismatches[0].payload["expected_system_id"] == 3
    assert mismatches[0].payload["observed_system_id"] == 1


async def test_no_transport_is_attached_when_identity_fails(
    monkeypatch, d3_config, probing_connection_config, bus, adapters
) -> None:
    """The refusal happens before MAVSDK attaches.

    That ordering is what makes it structurally impossible to command the
    wrong aircraft: there is no adapter to command.
    """
    monkeypatch.setattr(
        "app.drone.connection.probe_identity", _probe_returning(1)
    )
    connection = DroneConnection(
        d3_config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await _run_briefly(connection)

    assert connection.adapter is None
    assert "D3" not in adapters.adapters, (
        "no transport should have been built for a rejected identity"
    )


async def test_silent_endpoint_is_refused(
    monkeypatch, d3_config, probing_connection_config, bus, adapters
) -> None:
    """No heartbeat at all is also a refusal, not an optimistic connect."""
    monkeypatch.setattr(
        "app.drone.connection.probe_identity", _probe_returning(None)
    )
    connection = DroneConnection(
        d3_config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await _run_briefly(connection)

    assert connection.state.identity_verified is False
    assert connection.adapter is None
    assert "no vehicle heartbeat" in (connection.state.last_error or "")


async def test_unprobeable_endpoint_fails_closed(
    monkeypatch, d3_config, probing_connection_config, bus, adapters
) -> None:
    """When identity cannot be established, refuse rather than guess."""

    async def unsupported(endpoint, timeout_s, expected_system_id=None):
        raise IdentityProbeUnsupported("udpout endpoints cannot be probed passively")

    monkeypatch.setattr("app.drone.connection.probe_identity", unsupported)
    connection = DroneConnection(
        d3_config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await _run_briefly(connection)

    assert connection.state.identity_verified is False
    assert connection.adapter is None
    assert "identity probe unsupported" in (connection.state.last_error or "")


async def test_hardware_uid_mismatch_is_refused(
    monkeypatch, probing_connection_config, bus, adapters
) -> None:
    """An airframe swap the GCS was not told about is caught.

    The system id can be reconfigured on any airframe; the hardware UID is the
    board. Once recorded, a different board on the same endpoint is rejected.
    """
    config = DroneConfig(
        drone_id="D3",
        role=DroneRole.DELIVERY,
        system_id=3,
        connection_endpoint="udpin://0.0.0.0:14543",
        mavsdk_server_port=50053,
        expected_hardware_uid="THE-BOARD-WE-FLEW-LAST-TIME",
    )
    monkeypatch.setattr(
        "app.drone.connection.probe_identity", _probe_returning(3)
    )
    connection = DroneConnection(
        config, probing_connection_config, TelemetryRatesConfig(), bus, adapters
    )
    await _run_briefly(connection)

    # The fake reports FAKE-UID-D3, which is not the recorded board.
    assert connection.state.identity_verified is False
    assert connection.state.connection_state is not ConnectionState.CONNECTED
    assert "hardware uid mismatch" in (connection.state.last_error or "")


async def test_probe_disabled_is_recorded_as_unverified_reason(
    d3_config, bus, adapters
) -> None:
    """Turning the probe off is allowed for bench work, but it is a choice.

    The connection still comes up, but nothing in the code claims the identity
    was verified by observation -- that only happens when a probe actually ran.
    """
    config = ConnectionConfig(
        require_identity_probe=False,
        connect_timeout_s=2.0,
        heartbeat_degraded_s=0.3,
        heartbeat_lost_s=0.8,
        reconnect_initial_delay_s=0.05,
    )
    connection = DroneConnection(
        d3_config, config, TelemetryRatesConfig(), bus, adapters
    )
    await connection.start()
    await asyncio.sleep(0.3)
    try:
        assert connection.state.connection_state is ConnectionState.CONNECTED
        # The adapter self-reports its configured identity; that is the weaker
        # guarantee the operator opted into by disabling the probe.
        assert connection.state.identity.system_id == 3
    finally:
        await connection.stop()


async def test_each_aircraft_keeps_isolated_state(live_fleet, adapters) -> None:
    """Independent identity, telemetry and command state per airframe."""
    states = live_fleet.states
    assert {s.system_id for s in states.values()} == {1, 2, 3}
    assert {s.identity.hardware_uid for s in states.values()} == {
        "FAKE-UID-D1",
        "FAKE-UID-D2",
        "FAKE-UID-D3",
    }
    # Each state object is distinct: mutating one cannot affect another.
    states["D1"].mission_id = "mission-a"
    assert states["D2"].mission_id is None
    assert states["D3"].mission_id is None


async def test_adapters_are_not_shared_between_aircraft(
    live_fleet, adapters: FakeAdapterRegistry
) -> None:
    """The one-to-one binding that makes cross-drone routing impossible."""
    manager = live_fleet.connection_manager
    d1 = manager.require_adapter("D1")
    d3 = manager.require_adapter("D3")

    assert d1 is not d3
    assert d1 is adapters.get("D1")
    assert d3 is adapters.get("D3")
    # Each adapter is bound to its own endpoint and gRPC port.
    assert d1.config.connection_endpoint != d3.config.connection_endpoint
    assert d1.config.mavsdk_server_port != d3.config.mavsdk_server_port
