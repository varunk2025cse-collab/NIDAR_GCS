"""Staged validation against real PX4 hardware.

These tests talk to actual flight controllers. They are skipped unless you
pass ``--run-hardware``, and each stage is additionally gated behind its own
flag, so you cannot reach an arming test by running the whole file.

Run them in order, and only move to the next stage when the current one has
passed. The full procedure, including the physical preparation each stage
assumes, is in ``docs/hardware-validation.md``.

    # Stage 1-2: link and telemetry. Propellers removed.
    pytest tests/hardware --run-hardware -k "stage1 or stage2"

    # Stage 3: command acknowledgement. Propellers still removed.
    pytest tests/hardware --run-hardware --stage3 -k stage3

    # Stage 4: bench arming. Airframe secured, propellers removed.
    pytest tests/hardware --run-hardware --stage4 -k stage4

Nothing here commands a takeoff. Flight validation (stages 5 and 6) is done
by a pilot with a transmitter in hand, following the checklist in the doc --
not by a test runner.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_fleet_config, get_settings
from app.core.enums import ConnectionState
from app.drone.identity import probe_identity
from app.drone.manager import DroneConnectionManager
from app.realtime.event_bus import EventBus

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def hardware_settings():
    settings = get_settings()
    if settings.environment == "development":
        pytest.skip(
            "Set ENVIRONMENT=bench or field before running hardware validation"
        )
    return settings


@pytest.fixture(scope="module")
def hardware_fleet():
    fleet = get_fleet_config()
    if not fleet.drones:
        pytest.skip("No aircraft configured in config/fleet.yaml")
    return fleet


@pytest.fixture
async def live_manager(hardware_settings, hardware_fleet):
    bus = EventBus()
    await bus.start()
    manager = DroneConnectionManager.from_settings(
        hardware_settings, hardware_fleet, bus
    )
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()
        await bus.stop()


async def _wait_connected(manager, drone_id: str, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if manager.state(drone_id).connection_state is ConnectionState.CONNECTED:
            return
        await asyncio.sleep(0.5)
    state = manager.state(drone_id)
    pytest.fail(
        f"{drone_id} did not connect within {timeout_s}s "
        f"(state={state.connection_state}, last_error={state.last_error})"
    )


# ---------------------------------------------------------------------------
# Stage 1 -- MAVLink reaches the GCS, and it is the aircraft we think it is
# ---------------------------------------------------------------------------
async def test_stage1_identity_probe_sees_the_expected_system_id(
    hardware_settings, hardware_fleet
) -> None:
    """Acceptance: every configured endpoint carries a vehicle heartbeat whose
    system id matches the fleet file.

    A mismatch here means the fleet file and the radio wiring disagree. Fix
    that before anything else -- every later stage assumes this mapping.
    """
    for drone in hardware_fleet.drones:
        if not drone.enabled:
            continue
        result = await probe_identity(
            drone.identity_endpoint or drone.connection_endpoint,
            hardware_settings.connection.identity_probe_timeout_s,
            expected_system_id=drone.system_id,
        )
        assert result is not None, (
            f"No vehicle heartbeat on {drone.connection_endpoint} for "
            f"{drone.drone_id}. Check the radio, MAVLink Router and power."
        )
        assert result.identity.system_id == drone.system_id, (
            f"{drone.drone_id} is configured as system {drone.system_id} but "
            f"system {result.identity.system_id} answered on its endpoint"
        )
        print(
            f"{drone.drone_id}: sysid={result.identity.system_id} "
            f"autopilot={result.identity.autopilot} "
            f"type={result.identity.vehicle_type}"
        )


# ---------------------------------------------------------------------------
# Stage 2 -- real telemetry, with real freshness
# ---------------------------------------------------------------------------
async def test_stage2_all_aircraft_connect(live_manager, hardware_fleet) -> None:
    """Acceptance: every enabled aircraft reaches CONNECTED with a verified
    identity, and reports a hardware UID."""
    for drone in hardware_fleet.drones:
        if not drone.enabled:
            continue
        await _wait_connected(live_manager, drone.drone_id, timeout_s=60)
        state = live_manager.state(drone.drone_id)
        assert state.identity_verified is True
        assert state.identity is not None
        print(
            f"{drone.drone_id}: uid={state.identity.hardware_uid} "
            f"fw={state.identity.firmware_version} "
            f"product={state.identity.product_name}"
        )


async def test_stage2_telemetry_is_real_and_plausible(
    live_manager, hardware_fleet
) -> None:
    """Acceptance: position, battery, GPS and flight mode all arrive, and the
    values are physically plausible for an aircraft sitting on the ground.

    Record the hardware UIDs printed by the previous test into
    ``config/fleet.yaml`` as ``expected_hardware_uid`` once this passes.
    """
    for drone in hardware_fleet.drones:
        if not drone.enabled:
            continue
        await _wait_connected(live_manager, drone.drone_id, timeout_s=60)
        state = live_manager.state(drone.drone_id)

        # Give the streams a few seconds to populate.
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if all(
                v.value is not None
                for v in (state.position, state.battery, state.gps, state.flight_mode)
            ):
                break
            await asyncio.sleep(0.5)

        assert state.position.value is not None, f"{drone.drone_id}: no position"
        assert state.battery.value is not None, f"{drone.drone_id}: no battery"
        assert state.gps.value is not None, f"{drone.drone_id}: no GPS"

        position = state.position.value
        battery = state.battery.value
        gps = state.gps.value

        assert -90 <= position.latitude <= 90
        assert -180 <= position.longitude <= 180
        assert not (abs(position.latitude) < 1e-9 and abs(position.longitude) < 1e-9), (
            f"{drone.drone_id} reports Null Island; the GPS has no fix yet"
        )
        if battery.remaining_percent is not None:
            assert 0 <= battery.remaining_percent <= 100

        age = state.contact_age_s()
        assert age is not None and age < 5, (
            f"{drone.drone_id} telemetry is {age}s old; check the link budget"
        )
        print(
            f"{drone.drone_id}: {position.latitude:.6f},{position.longitude:.6f} "
            f"alt={position.relative_altitude_m}m "
            f"batt={battery.remaining_percent}% "
            f"gps={gps.fix_type_name}/{gps.satellites} "
            f"mode={state.flight_mode.value}"
        )


async def test_stage2_link_loss_is_detected(
    request, live_manager, hardware_fleet
) -> None:
    """Acceptance: powering down one aircraft moves it to DEGRADED then
    DISCONNECTED, and the GCS stops reporting a live position for it.

    This test is interactive: it waits for you to switch an aircraft off.
    """
    if not request.config.getoption("--interactive", default=False):
        pytest.skip("needs an operator to power an aircraft down; pass --interactive")

    drone_id = hardware_fleet.drones[0].drone_id
    await _wait_connected(live_manager, drone_id, timeout_s=60)

    print(f"\n>>> Power down {drone_id} now. Waiting up to 120s for loss detection.")
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        state = live_manager.state(drone_id)
        if state.connection_state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
            ConnectionState.CONNECTING,
            ConnectionState.IDENTIFYING,
        ):
            assert state.position.value is None, (
                "live position must be dropped once the link is lost"
            )
            assert state.last_known_position is not None
            print(
                f"{drone_id}: loss detected, last seen at "
                f"{state.last_known_position.at.isoformat()}"
            )
            return
        await asyncio.sleep(1.0)
    pytest.fail(f"{drone_id} link loss was not detected within 120s")


# ---------------------------------------------------------------------------
# Stage 3 -- command acknowledgement, with propulsion disabled
# ---------------------------------------------------------------------------
async def test_stage3_read_only_commands_acknowledge(
    request, live_manager, hardware_fleet
) -> None:
    """Acceptance: PX4 acknowledges a command that cannot move the aircraft.

    Uses a geofence upload: it exercises the full command path -- send,
    acknowledge, record -- without any possibility of motor movement.

    PROPELLERS MUST BE REMOVED before running this.
    """
    if not request.config.getoption("--stage3", default=False):
        pytest.skip("stage 3 requires --stage3 and propellers removed")

    from app.drone.types import GeofencePolygonSpec

    drone = hardware_fleet.drones[0]
    await _wait_connected(live_manager, drone.drone_id, timeout_s=60)
    state = live_manager.state(drone.drone_id)
    position = state.position.value
    assert position is not None, "need a position fix to build a test geofence"

    from app.core.geo import destination_point

    corners = [
        destination_point(position.latitude, position.longitude, bearing, 200.0)
        for bearing in (45, 135, 225, 315)
    ]
    spec = GeofencePolygonSpec(
        points=[(p.latitude, p.longitude) for p in corners], inclusion=True
    )

    adapter = live_manager.require_adapter(drone.drone_id)
    result = await adapter.upload_geofence([spec])

    assert result.acknowledged is True, (
        f"{drone.drone_id} did not acknowledge the geofence upload: "
        f"{result.result_code} {result.detail}"
    )
    assert result.success is True, (
        f"{drone.drone_id} rejected the geofence upload: "
        f"{result.result_code} {result.detail}"
    )
    print(f"{drone.drone_id}: geofence upload acknowledged ({result.result_code})")

    await adapter.clear_geofence()


# ---------------------------------------------------------------------------
# Stage 4 -- bench arming, airframe secured
# ---------------------------------------------------------------------------
async def test_stage4_arm_and_disarm_on_the_bench(
    request, live_manager, hardware_fleet
) -> None:
    """Acceptance: the aircraft arms, the GCS observes armed=True through real
    telemetry, and it disarms again.

    THE AIRFRAME MUST BE PHYSICALLY SECURED AND THE PROPELLERS REMOVED.
    Do not run this with propellers fitted.

    This is the first test that energises the motors, which is why it needs
    its own flag on top of --run-hardware.
    """
    if not request.config.getoption("--stage4", default=False):
        pytest.skip(
            "stage 4 arms a real aircraft; requires --stage4, a secured "
            "airframe and propellers removed"
        )

    drone = hardware_fleet.drones[0]
    await _wait_connected(live_manager, drone.drone_id, timeout_s=60)
    state = live_manager.state(drone.drone_id)
    adapter = live_manager.require_adapter(drone.drone_id)

    assert state.armed.value is not True, "aircraft is already armed"

    arm = await adapter.arm()
    assert arm.acknowledged and arm.success, (
        f"arm rejected: {arm.result_code} {arm.detail}"
    )

    # The point of the stage: confirm through telemetry, not through the ack.
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if state.armed.value is True:
            break
        await asyncio.sleep(0.2)
    armed_observed = state.armed.value is True

    disarm = await adapter.disarm()
    assert disarm.acknowledged, "disarm was not acknowledged -- disarm manually now"

    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if state.armed.value is False:
            break
        await asyncio.sleep(0.2)

    assert armed_observed, (
        "PX4 acknowledged the arm command but telemetry never reported armed; "
        "this is exactly the case the GCS reports as UNKNOWN rather than success"
    )
    assert state.armed.value is False, "aircraft did not disarm -- disarm it manually"
    print(f"{drone.drone_id}: arm and disarm both confirmed through telemetry")
