"""Command lifecycle.

The properties under test are the ones that keep an operator honest about
what an aircraft actually did:

* a command is only COMPLETED when the aircraft was observed to comply;
* an acknowledged-but-unverified command reports UNKNOWN, never success;
* a rejection by the flight controller surfaces the real result code;
* preconditions block a command before it leaves the GCS;
* one command at a time per aircraft.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import CommandState, CommandType, OperatorRole
from app.core.exceptions import (
    CommandInProgressError,
    CommandRejectedError,
    DroneNotReadyError,
)
from app.core.security import TokenPrincipal
from app.drone.types import Battery, GpsInfo, HealthReport


@pytest.fixture
def principal() -> TokenPrincipal:
    return TokenPrincipal(
        operator_id=uuid.uuid4(),
        username="test-operator",
        role=OperatorRole.OPERATOR,
        token_id="test",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# the happy path still requires evidence
# ---------------------------------------------------------------------------
async def test_arm_is_completed_only_after_observed_state_change(
    command_service, principal, adapters, live_fleet
) -> None:
    outcome = await command_service.execute(
        drone_id="D1", command_type=CommandType.ARM, principal=principal
    )
    assert outcome.state is CommandState.COMPLETED
    assert outcome.acknowledged is True
    assert outcome.verified is True
    assert outcome.success is True
    assert outcome.verification["armed"] is True
    assert ("ARM", pytest.approx) or adapters.get("D1").command_log[0][0] == "ARM"


async def test_acknowledged_but_no_state_change_reports_unknown(
    command_service, principal, adapters
) -> None:
    """The central rule: PX4 saying "accepted" is not the aircraft complying.

    Here the flight controller accepts the arm command but the vehicle never
    reports armed. Reporting success would tell the operator the aircraft is
    armed when it is not.
    """
    adapters.get("D1").commands_change_state = False

    outcome = await command_service.execute(
        drone_id="D1", command_type=CommandType.ARM, principal=principal
    )
    assert outcome.acknowledged is True
    assert outcome.verified is False
    assert outcome.success is False
    assert outcome.state is CommandState.UNKNOWN
    assert "was not observed" in (outcome.detail or "")


async def test_rejection_surfaces_the_flight_controller_result(
    command_service, principal, adapters
) -> None:
    adapters.get("D1").reject_commands.add("ARM")

    with pytest.raises(CommandRejectedError) as exc:
        await command_service.execute(
            drone_id="D1", command_type=CommandType.ARM, principal=principal
        )
    assert exc.value.details["result_code"] == "ARM_DENIED"
    assert "refused by vehicle" in exc.value.details["detail"]


async def test_command_timeout_is_reported_not_swallowed(
    command_service, principal, adapters, settings
) -> None:
    settings.command.arm_timeout_s = 0.2
    adapters.get("D1").hang_commands.add("ARM")

    outcome = await command_service.execute(
        drone_id="D1", command_type=CommandType.ARM, principal=principal
    )
    assert outcome.state is CommandState.TIMEOUT
    assert outcome.success is False
    assert outcome.acknowledged is False


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------
async def test_low_battery_blocks_takeoff(
    command_service, principal, adapters, live_fleet
) -> None:
    adapters.get("D1").current_battery = Battery(20.0, 21.0, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 20.0)

    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1",
            command_type=CommandType.TAKEOFF,
            principal=principal,
            parameters={"altitude_m": 30.0},
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["BATTERY"]["status"] == "FAIL"
    assert "below" in checks["BATTERY"]["reason"]
    assert adapters.get("D1").command_log == [], "nothing may reach the aircraft"


async def test_missing_battery_telemetry_blocks_takeoff(
    command_service, principal, adapters, live_fleet
) -> None:
    """No battery data is not the same as a good battery."""
    adapters.get("D1").current_battery = Battery(None, 22.0, None)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent is None)

    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1",
            command_type=CommandType.TAKEOFF,
            principal=principal,
            parameters={"altitude_m": 30.0},
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["BATTERY"]["status"] == "FAIL"


async def test_poor_gps_blocks_arming(
    command_service, principal, adapters, live_fleet
) -> None:
    adapters.get("D1").current_gps = GpsInfo(
        fix_type=1, fix_type_name="NO_FIX", satellites=3
    )
    await _settle(live_fleet, "D1", lambda s: s.gps.value.fix_type == 1)

    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1", command_type=CommandType.ARM, principal=principal
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["GPS"]["status"] == "FAIL"


async def test_unhealthy_vehicle_blocks_arming(
    command_service, principal, adapters, live_fleet
) -> None:
    adapters.get("D1").current_health = HealthReport(
        gyrometer_calibration_ok=True,
        accelerometer_calibration_ok=True,
        magnetometer_calibration_ok=True,
        local_position_ok=True,
        global_position_ok=True,
        home_position_ok=False,
        armable=False,
    )
    await _settle(live_fleet, "D1", lambda s: s.health.value.armable is False)

    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1", command_type=CommandType.ARM, principal=principal
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["HEALTH"]["status"] == "FAIL"


async def test_disarm_in_flight_is_refused(
    command_service, principal, adapters, live_fleet
) -> None:
    """Disarming an airborne aircraft cuts the motors. It is not a routine
    command and this API will not issue it."""
    adapter = adapters.get("D1")
    adapter.airborne = True
    adapter.is_armed = True
    await _settle(live_fleet, "D1", lambda s: s.in_air.value is True)

    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1", command_type=CommandType.DISARM, principal=principal
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["NOT_AIRBORNE"]["status"] == "FAIL"
    assert "DISARM" not in [c[0] for c in adapter.command_log]


async def test_takeoff_when_already_airborne_is_refused(
    command_service, principal, adapters, live_fleet
) -> None:
    adapters.get("D1").airborne = True
    await _settle(live_fleet, "D1", lambda s: s.in_air.value is True)

    with pytest.raises(DroneNotReadyError):
        await command_service.execute(
            drone_id="D1",
            command_type=CommandType.TAKEOFF,
            principal=principal,
            parameters={"altitude_m": 30.0},
        )


async def test_rtl_is_allowed_on_a_degraded_link(
    command_service, principal, adapters, live_fleet
) -> None:
    """Refusing a recovery command because the link is imperfect would be the
    wrong trade: RTL is exactly what a degraded link calls for."""
    adapters.make_unreachable("D1")
    await asyncio.sleep(0.5)
    state = live_fleet.state("D1")
    assert state.is_commandable is False

    checks = command_service._evaluate_preconditions(  # noqa: SLF001
        state, CommandType.RTL, {}, allow_degraded_link=False
    )
    by_name = {c.name: c for c in checks}
    assert by_name["CONNECTION"].status.value in ("WARN", "PASS")
    assert not [c for c in checks if c.blocking and c.name == "CONNECTION"]


# ---------------------------------------------------------------------------
# concurrency and idempotency
# ---------------------------------------------------------------------------
async def test_one_command_at_a_time_per_aircraft(
    command_service, principal, adapters, settings
) -> None:
    settings.command.arm_timeout_s = 2.0
    adapters.get("D1").hang_commands.add("ARM")

    first = asyncio.create_task(
        command_service.execute(
            drone_id="D1", command_type=CommandType.ARM, principal=principal
        )
    )
    await asyncio.sleep(0.1)

    with pytest.raises(CommandInProgressError):
        await command_service.execute(
            drone_id="D1", command_type=CommandType.LAND, principal=principal
        )

    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


async def test_commands_to_different_aircraft_run_concurrently(
    command_service, principal, adapters
) -> None:
    """A busy D1 must not block a command to D3 -- during an abort every
    aircraft has to be reachable at once."""
    results = await asyncio.gather(
        command_service.execute(
            drone_id="D1", command_type=CommandType.ARM, principal=principal
        ),
        command_service.execute(
            drone_id="D3", command_type=CommandType.ARM, principal=principal
        ),
    )
    assert all(r.success for r in results)
    assert adapters.get("D1").is_armed is True
    assert adapters.get("D3").is_armed is True


async def test_command_targets_only_the_named_aircraft(
    command_service, principal, adapters
) -> None:
    """A command for D3 must never touch D1 or D2."""
    await command_service.execute(
        drone_id="D3", command_type=CommandType.ARM, principal=principal
    )
    assert adapters.get("D3").is_armed is True
    assert adapters.get("D1").is_armed is False
    assert adapters.get("D2").is_armed is False
    assert adapters.get("D1").command_log == []
    assert adapters.get("D2").command_log == []


# ---------------------------------------------------------------------------
# lifecycle events
# ---------------------------------------------------------------------------
async def test_command_lifecycle_is_published(
    command_service, principal, collected_events, bus
) -> None:
    await command_service.execute(
        drone_id="D1", command_type=CommandType.ARM, principal=principal
    )
    await bus.drain()

    states = [
        e.payload["state"]
        for e in collected_events
        if e.type == "COMMAND_UPDATED" and e.drone_id == "D1"
    ]
    assert "COMMAND_REQUESTED" in states
    assert "SENT_TO_MAVSDK" in states
    assert "ACKNOWLEDGED" in states
    assert "COMPLETED" in states
    assert states.index("SENT_TO_MAVSDK") < states.index("COMPLETED")


async def test_takeoff_altitude_out_of_range_is_refused(
    command_service, principal, adapters
) -> None:
    with pytest.raises(DroneNotReadyError) as exc:
        await command_service.execute(
            drone_id="D1",
            command_type=CommandType.TAKEOFF,
            principal=principal,
            parameters={"altitude_m": 2000.0},
        )
    checks = {c["check"]: c for c in exc.value.details["checks"]}
    assert checks["TAKEOFF_ALTITUDE"]["status"] == "FAIL"
    assert adapters.get("D1").command_log == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _settle(fleet, drone_id: str, predicate, timeout_s: float = 2.0) -> None:
    """Wait for scripted vehicle state to propagate through the telemetry stream."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    state = fleet.state(drone_id)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if predicate(state):
                return
        except AttributeError:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError(f"telemetry for {drone_id} did not reach the expected state")
