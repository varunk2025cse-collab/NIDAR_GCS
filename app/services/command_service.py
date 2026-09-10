"""Command lifecycle for physical aircraft.

Every command an operator can send passes through :meth:`CommandService.execute`,
which does the same six things every time:

1. **Precondition check** against live telemetry -- refuse rather than guess.
2. **Idempotency** -- a unique key in the database, so a double-click on ABORT
   cannot start two abort workflows.
3. **Exclusivity** -- one command in flight per aircraft.
4. **Send** through the adapter, recording what the flight controller answered.
5. **Verification** -- watch real telemetry for the state change the command
   was supposed to cause.
6. **Audit** -- persist the whole trail, success or failure.

The distinction that matters: an acknowledged command is not a completed one.
Only an observed state change makes a command COMPLETED. If PX4 accepts a
takeoff but the aircraft never leaves the ground, this service reports UNKNOWN,
not success.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import CheckStatus, CommandState, CommandType, TelemetryStatus
from app.core.exceptions import (
    CommandInProgressError,
    CommandRejectedError,
    DroneNotConnectedError,
    DroneNotReadyError,
)
from app.core.logging import get_logger
from app.core.security import TokenPrincipal
from app.database.session import session_scope_optional
from app.drone.state import DroneState
from app.drone.types import CommandResult
from app.models.command import CommandTransition, DroneCommand
from app.realtime.event_bus import EventBus, EventType
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager

logger = get_logger(__name__)


@dataclass(slots=True)
class Precondition:
    name: str
    status: CheckStatus
    reason: str | None = None
    observed: Any = None

    @property
    def blocking(self) -> bool:
        return self.status is CheckStatus.FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": str(self.status),
            "reason": self.reason,
            "observed": self.observed,
        }


@dataclass(slots=True)
class CommandOutcome:
    """Everything the caller needs to report the real result."""

    command_id: uuid.UUID | None
    command_type: CommandType
    drone_id: str
    state: CommandState
    acknowledged: bool
    verified: bool
    result_code: str
    detail: str | None = None
    preconditions: list[Precondition] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    idempotent_replay: bool = False
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @property
    def success(self) -> bool:
        """True only when the aircraft was observed to comply."""
        return self.state is CommandState.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": str(self.command_id) if self.command_id else None,
            "command_type": str(self.command_type),
            "drone_id": self.drone_id,
            "state": str(self.state),
            "acknowledged": self.acknowledged,
            "verified": self.verified,
            "success": self.success,
            "result_code": self.result_code,
            "detail": self.detail,
            "idempotent_replay": self.idempotent_replay,
            "preconditions": [p.as_dict() for p in self.preconditions],
            "verification": self.verification,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


#: What each command must make true about the aircraft to count as completed.
#: ``None`` means the command has no directly observable state change; those
#: settle at ACKNOWLEDGED and are reported as unverified.
VerificationPredicate = Callable[[DroneState], bool]

_VERIFICATION: dict[CommandType, VerificationPredicate | None] = {
    CommandType.ARM: lambda s: s.armed.value is True,
    CommandType.DISARM: lambda s: s.armed.value is False,
    CommandType.TAKEOFF: lambda s: s.in_air.value is True,
    CommandType.LAND: lambda s: s.in_air.value is False or s.landed_state.value == "ON_GROUND",
    CommandType.RTL: lambda s: s.flight_mode.value == "RETURN_TO_LAUNCH",
    CommandType.HOLD: lambda s: s.flight_mode.value == "HOLD",
    CommandType.START_MISSION: lambda s: s.flight_mode.value == "MISSION",
    CommandType.PAUSE_MISSION: lambda s: s.flight_mode.value in ("HOLD", "PAUSED"),
    CommandType.RESUME_MISSION: lambda s: s.flight_mode.value == "MISSION",
    CommandType.GOTO: None,
    CommandType.UPLOAD_MISSION: None,
    CommandType.CLEAR_MISSION: None,
    CommandType.UPLOAD_GEOFENCE: None,
    CommandType.CLEAR_GEOFENCE: None,
    CommandType.RELEASE_PAYLOAD: None,
}

#: Commands that put an aircraft in the air or keep it there. These get the
#: strictest preconditions.
_FLIGHT_COMMANDS = frozenset(
    {CommandType.ARM, CommandType.TAKEOFF, CommandType.START_MISSION, CommandType.GOTO}
)

#: Recovery commands. These stay available when telemetry is degraded --
#: refusing an RTL because the link is imperfect would be the wrong tradeoff.
_RECOVERY_COMMANDS = frozenset({CommandType.RTL, CommandType.LAND, CommandType.HOLD})


class CommandService:
    def __init__(
        self,
        fleet: FleetManager,
        events: EventService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._events = events
        self._bus = bus
        self._settings = settings
        self._policy = fleet.policy

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    async def execute(
        self,
        *,
        drone_id: str,
        command_type: CommandType,
        principal: TokenPrincipal | None = None,
        mission_id: uuid.UUID | None = None,
        parameters: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        verify: bool = True,
        allow_degraded_link: bool = False,
    ) -> CommandOutcome:
        parameters = parameters or {}
        drone_id = drone_id.upper()
        key = idempotency_key or f"{drone_id}:{command_type}:{uuid.uuid4()}"
        log = logger.bind(
            drone_id=drone_id, command=str(command_type), request_id=request_id,
            idempotency_key=key,
        )

        connection = self._fleet.connection_manager.get(drone_id)
        state = connection.state

        # --- 1. idempotency ------------------------------------------------
        replay = await self._find_existing(key)
        if replay is not None:
            log.info("command_idempotent_replay", command_id=str(replay.id),
                     state=str(replay.state))
            return CommandOutcome(
                command_id=replay.id,
                command_type=command_type,
                drone_id=drone_id,
                state=replay.state,
                acknowledged=replay.acknowledged_at is not None,
                verified=replay.verified,
                result_code=str(replay.acknowledgement.get("result_code", "REPLAY")),
                detail="Replay of an already-issued command; no new command was sent",
                idempotent_replay=True,
                requested_at=replay.requested_at,
                completed_at=replay.completed_at,
            )

        # --- 2. preconditions ---------------------------------------------
        checks = self._evaluate_preconditions(
            state, command_type, parameters, allow_degraded_link=allow_degraded_link
        )
        blocking = [c for c in checks if c.blocking]
        if blocking:
            outcome = CommandOutcome(
                command_id=None,
                command_type=command_type,
                drone_id=drone_id,
                state=CommandState.REJECTED,
                acknowledged=False,
                verified=False,
                result_code="PRECONDITION_FAILED",
                detail="; ".join(f"{c.name}: {c.reason}" for c in blocking),
                preconditions=checks,
            )
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            log.warning(
                "command_blocked_by_precondition",
                failures=[c.name for c in blocking],
            )
            raise DroneNotReadyError(
                f"{drone_id} is not ready for {command_type}",
                details={
                    "drone_id": drone_id,
                    "command": str(command_type),
                    "checks": [c.as_dict() for c in checks],
                },
            )

        # --- 3. exclusivity ------------------------------------------------
        if connection.command_lock.locked():
            raise CommandInProgressError(
                f"{drone_id} is already executing {state.busy_with_command}",
                details={"drone_id": drone_id, "in_progress": state.busy_with_command},
            )

        async with connection.command_lock:
            state.busy_with_command = str(command_type)
            try:
                return await self._run(
                    connection=connection,
                    state=state,
                    command_type=command_type,
                    parameters=parameters,
                    checks=checks,
                    principal=principal,
                    mission_id=mission_id,
                    idempotency_key=key,
                    request_id=request_id,
                    source_ip=source_ip,
                    verify=verify,
                    log=log,
                )
            finally:
                state.busy_with_command = None

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    async def _run(
        self,
        *,
        connection: Any,
        state: DroneState,
        command_type: CommandType,
        parameters: dict[str, Any],
        checks: list[Precondition],
        principal: TokenPrincipal | None,
        mission_id: uuid.UUID | None,
        idempotency_key: str,
        request_id: str | None,
        source_ip: str | None,
        verify: bool,
        log: Any,
    ) -> CommandOutcome:
        drone_id = state.drone_id
        timeout_s = self._timeout_for(command_type)

        record = await self._create_record(
            drone_id=drone_id,
            command_type=command_type,
            mission_id=mission_id,
            operator_id=principal.operator_id if principal else None,
            parameters=parameters,
            idempotency_key=idempotency_key,
            request_id=request_id,
            checks=checks,
            timeout_s=timeout_s,
        )
        command_id = record.id if record else None
        outcome = CommandOutcome(
            command_id=command_id,
            command_type=command_type,
            drone_id=drone_id,
            state=CommandState.REQUESTED,
            acknowledged=False,
            verified=False,
            result_code="REQUESTED",
            preconditions=checks,
        )
        self._publish(outcome)

        adapter = self._fleet.connection_manager.require_adapter(drone_id)

        # --- send ----------------------------------------------------------
        await self._transition(command_id, CommandState.SENT_TO_MAVSDK, {})
        outcome.state = CommandState.SENT_TO_MAVSDK
        self._publish(outcome)

        try:
            result = await asyncio.wait_for(
                self._dispatch(adapter, command_type, parameters), timeout=timeout_s
            )
        except TimeoutError:
            outcome.state = CommandState.TIMEOUT
            outcome.result_code = "TIMEOUT"
            outcome.detail = f"No response from {drone_id} within {timeout_s}s"
            await self._finalise(record, outcome, {})
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            self._publish(outcome)
            log.error("command_timeout", timeout_s=timeout_s)
            return outcome
        except DroneNotConnectedError:
            raise
        except Exception as exc:
            outcome.state = CommandState.FAILED
            outcome.result_code = type(exc).__name__
            outcome.detail = str(exc)
            await self._finalise(record, outcome, {})
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            self._publish(outcome)
            log.error("command_failed", error=str(exc), exc_info=True)
            return outcome

        await self._transition(
            command_id, CommandState.SENT_TO_PX4, {"result_code": result.result_code}
        )

        # --- acknowledgement -----------------------------------------------
        outcome.acknowledged = result.acknowledged
        outcome.result_code = result.result_code
        outcome.detail = result.detail

        if not result.acknowledged:
            outcome.state = CommandState.UNKNOWN
            await self._finalise(record, outcome, {"acknowledgement": result.raw})
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            self._publish(outcome)
            log.warning("command_unacknowledged", result_code=result.result_code)
            return outcome

        if not result.success:
            outcome.state = CommandState.REJECTED
            await self._finalise(record, outcome, {"acknowledgement": result.raw})
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            self._publish(outcome)
            log.warning("command_rejected_by_px4", result_code=result.result_code,
                        detail=result.detail)
            raise CommandRejectedError(
                f"{drone_id} rejected {command_type}: {result.detail or result.result_code}",
                details={
                    "drone_id": drone_id,
                    "command": str(command_type),
                    "result_code": result.result_code,
                    "detail": result.detail,
                    "command_id": str(command_id) if command_id else None,
                },
            )

        outcome.state = CommandState.ACKNOWLEDGED
        await self._transition(command_id, CommandState.ACKNOWLEDGED, result.raw)
        self._publish(outcome)

        # --- verification ---------------------------------------------------
        predicate = _VERIFICATION.get(command_type)
        if not verify or predicate is None:
            # No observable state change to wait for. The command is recorded
            # as acknowledged but explicitly unverified -- never as completed.
            outcome.verification = {
                "verified": False,
                "reason": (
                    "no observable state change defined for this command"
                    if predicate is None
                    else "verification skipped by caller"
                ),
            }
            await self._finalise(record, outcome, {"acknowledgement": result.raw})
            await self._audit(outcome, principal, mission_id, request_id, source_ip)
            self._publish(outcome)
            return outcome

        verified, observed = await self._verify(state, predicate)
        outcome.verification = observed
        if verified:
            outcome.verified = True
            outcome.state = CommandState.COMPLETED
            await self._transition(command_id, CommandState.STATE_CHANGED, observed)
            log.info("command_completed", verification=observed)
        else:
            # PX4 accepted it but the aircraft has not (yet) done it. UNKNOWN
            # is the honest answer.
            outcome.state = CommandState.UNKNOWN
            outcome.detail = (
                f"{drone_id} acknowledged {command_type} but the expected state change "
                f"was not observed within {self._settings.command.verification_timeout_s}s"
            )
            log.warning("command_unverified", verification=observed)

        await self._finalise(record, outcome, {"acknowledgement": result.raw})
        await self._audit(outcome, principal, mission_id, request_id, source_ip)
        self._publish(outcome)
        return outcome

    async def _dispatch(
        self, adapter: Any, command_type: CommandType, parameters: dict[str, Any]
    ) -> CommandResult:
        """Map a command type onto exactly one adapter call.

        This mapping is the whole command surface. There is no generic
        pass-through: a client cannot ask the backend to send an arbitrary
        MAVLink message.
        """
        match command_type:
            case CommandType.ARM:
                return await adapter.arm()
            case CommandType.DISARM:
                return await adapter.disarm()
            case CommandType.TAKEOFF:
                return await adapter.takeoff(parameters.get("altitude_m"))
            case CommandType.LAND:
                return await adapter.land()
            case CommandType.RTL:
                return await adapter.return_to_launch()
            case CommandType.HOLD | CommandType.PAUSE_MISSION:
                if command_type is CommandType.PAUSE_MISSION:
                    return await adapter.pause_mission()
                return await adapter.hold()
            case CommandType.RESUME_MISSION | CommandType.START_MISSION:
                return await adapter.start_mission()
            case CommandType.UPLOAD_MISSION:
                return await adapter.upload_mission(parameters["items"])
            case CommandType.CLEAR_MISSION:
                return await adapter.clear_mission()
            case CommandType.GOTO:
                return await adapter.goto_location(
                    parameters["latitude"],
                    parameters["longitude"],
                    parameters["absolute_altitude_m"],
                    parameters.get("yaw_deg", float("nan")),
                )
            case CommandType.UPLOAD_GEOFENCE:
                return await adapter.upload_geofence(parameters["polygons"])
            case CommandType.CLEAR_GEOFENCE:
                return await adapter.clear_geofence()
            case _:
                raise CommandRejectedError(
                    f"Command {command_type} has no adapter binding",
                    details={"command": str(command_type)},
                )

    async def _verify(
        self, state: DroneState, predicate: VerificationPredicate
    ) -> tuple[bool, dict[str, Any]]:
        """Poll live telemetry until the aircraft shows the expected change."""
        deadline = asyncio.get_running_loop().time() + (
            self._settings.command.verification_timeout_s
        )
        poll = self._settings.command.verification_poll_s
        while asyncio.get_running_loop().time() < deadline:
            if predicate(state):
                return True, {
                    "verified": True,
                    "armed": state.armed.value,
                    "in_air": state.in_air.value,
                    "flight_mode": state.flight_mode.value,
                    "landed_state": state.landed_state.value,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            await asyncio.sleep(poll)
        return False, {
            "verified": False,
            "armed": state.armed.value,
            "in_air": state.in_air.value,
            "flight_mode": state.flight_mode.value,
            "landed_state": state.landed_state.value,
            "timeout_s": self._settings.command.verification_timeout_s,
        }

    # ------------------------------------------------------------------
    # preconditions
    # ------------------------------------------------------------------
    def _evaluate_preconditions(
        self,
        state: DroneState,
        command_type: CommandType,
        parameters: dict[str, Any],
        allow_degraded_link: bool,
    ) -> list[Precondition]:
        checks: list[Precondition] = []
        safety = self._settings.safety
        recovery = command_type in _RECOVERY_COMMANDS

        # -- link ----------------------------------------------------------
        if state.is_commandable:
            checks.append(Precondition("CONNECTION", CheckStatus.PASS,
                                       observed=str(state.connection_state)))
        elif state.is_connected and (allow_degraded_link or recovery):
            checks.append(
                Precondition(
                    "CONNECTION",
                    CheckStatus.WARN,
                    "Link is degraded; proceeding because this is a recovery command",
                    observed=str(state.connection_state),
                )
            )
        else:
            checks.append(
                Precondition(
                    "CONNECTION",
                    CheckStatus.FAIL,
                    f"Link state is {state.connection_state}",
                    observed=str(state.connection_state),
                )
            )

        # -- identity ------------------------------------------------------
        checks.append(
            Precondition(
                "IDENTITY",
                CheckStatus.PASS if state.identity_verified else CheckStatus.FAIL,
                None if state.identity_verified else "MAVLink identity was never verified",
                observed=state.identity_verified,
            )
        )

        # -- telemetry freshness -------------------------------------------
        age = state.contact_age_s()
        max_age = safety.command_requires_telemetry_age_s
        if age is None:
            checks.append(
                Precondition(
                    "TELEMETRY_FRESHNESS",
                    CheckStatus.WARN if recovery else CheckStatus.FAIL,
                    "No telemetry has ever been received from this aircraft",
                )
            )
        elif age > max_age:
            checks.append(
                Precondition(
                    "TELEMETRY_FRESHNESS",
                    CheckStatus.WARN if recovery else CheckStatus.FAIL,
                    f"Telemetry is {age:.1f}s old (limit {max_age}s)",
                    observed=round(age, 2),
                )
            )
        else:
            checks.append(
                Precondition("TELEMETRY_FRESHNESS", CheckStatus.PASS, observed=round(age, 2))
            )

        # -- battery, GPS and health, for commands that begin or extend flight
        if command_type in _FLIGHT_COMMANDS:
            checks.append(self._battery_check(state, safety.battery_min_for_takeoff_pct))
            checks.append(self._gps_check(state))
            checks.append(self._health_check(state, command_type))

        # -- command-specific ------------------------------------------------
        if command_type is CommandType.TAKEOFF:
            if state.in_air.value is True:
                checks.append(
                    Precondition("NOT_AIRBORNE", CheckStatus.FAIL,
                                 "Aircraft already reports being in the air")
                )
            altitude = parameters.get("altitude_m")
            if altitude is not None and (altitude <= 0 or altitude > 500):
                checks.append(
                    Precondition("TAKEOFF_ALTITUDE", CheckStatus.FAIL,
                                 f"Takeoff altitude {altitude} m is out of range",
                                 observed=altitude)
                )
        if command_type is CommandType.DISARM and state.in_air.value is True:
            # Disarming in flight cuts the motors. It is never issued as a
            # routine command from this API.
            checks.append(
                Precondition(
                    "NOT_AIRBORNE",
                    CheckStatus.FAIL,
                    "Refusing to disarm an aircraft that reports being airborne",
                    observed=True,
                )
            )
        if command_type is CommandType.ARM and state.armed.value is True:
            checks.append(
                Precondition("NOT_ARMED", CheckStatus.WARN, "Aircraft is already armed")
            )
        return checks

    def _battery_check(self, state: DroneState, minimum_pct: float) -> Precondition:
        status = self._policy.status("battery", state.battery)
        battery = state.battery.value
        if status is TelemetryStatus.NO_DATA or battery is None:
            return Precondition("BATTERY", CheckStatus.FAIL, "No battery telemetry available")
        if battery.remaining_percent is None:
            return Precondition(
                "BATTERY", CheckStatus.FAIL,
                "Autopilot is not reporting a battery percentage",
                observed={"voltage_v": battery.voltage_v},
            )
        if battery.remaining_percent < minimum_pct:
            return Precondition(
                "BATTERY",
                CheckStatus.FAIL,
                f"Battery {battery.remaining_percent:.0f}% is below the {minimum_pct:.0f}% minimum",
                observed=battery.remaining_percent,
            )
        if status is TelemetryStatus.STALE:
            return Precondition(
                "BATTERY", CheckStatus.WARN, "Battery telemetry is stale",
                observed=battery.remaining_percent,
            )
        return Precondition("BATTERY", CheckStatus.PASS, observed=battery.remaining_percent)

    def _gps_check(self, state: DroneState) -> Precondition:
        gps = state.gps.value
        safety = self._settings.safety
        if gps is None:
            return Precondition("GPS", CheckStatus.FAIL, "No GPS telemetry available")
        if gps.fix_type < safety.min_gps_fix_type:
            return Precondition(
                "GPS", CheckStatus.FAIL,
                f"GPS fix {gps.fix_type_name} is below the required 3D fix",
                observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
            )
        if gps.satellites < safety.min_satellites:
            return Precondition(
                "GPS", CheckStatus.FAIL,
                f"{gps.satellites} satellites is below the minimum of {safety.min_satellites}",
                observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
            )
        return Precondition(
            "GPS", CheckStatus.PASS,
            observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
        )

    def _health_check(self, state: DroneState, command_type: CommandType) -> Precondition:
        health = state.health.value
        if health is None:
            return Precondition("HEALTH", CheckStatus.FAIL, "No health telemetry available")
        if command_type in (CommandType.ARM, CommandType.TAKEOFF) and not health.armable:
            return Precondition(
                "HEALTH", CheckStatus.FAIL,
                "Flight controller reports the aircraft is not armable",
                observed=health.as_dict(),
            )
        if not health.global_position_ok:
            return Precondition(
                "HEALTH", CheckStatus.FAIL, "Global position estimate is not healthy",
                observed=health.as_dict(),
            )
        if not health.home_position_ok:
            return Precondition(
                "HEALTH", CheckStatus.FAIL, "Home position is not set",
                observed=health.as_dict(),
            )
        if not health.all_ok:
            return Precondition(
                "HEALTH", CheckStatus.WARN, "Some health checks are not OK",
                observed=health.as_dict(),
            )
        return Precondition("HEALTH", CheckStatus.PASS, observed=health.as_dict())

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    async def _find_existing(self, idempotency_key: str) -> DroneCommand | None:
        async with session_scope_optional() as session:
            if session is None:
                return None
            result = await session.execute(
                select(DroneCommand).where(DroneCommand.idempotency_key == idempotency_key)
            )
            return result.scalar_one_or_none()

    async def _create_record(
        self,
        *,
        drone_id: str,
        command_type: CommandType,
        mission_id: uuid.UUID | None,
        operator_id: uuid.UUID | None,
        parameters: dict[str, Any],
        idempotency_key: str,
        request_id: str | None,
        checks: list[Precondition],
        timeout_s: float,
    ) -> DroneCommand | None:
        drone_uuid = self._fleet.drone_uuid(drone_id)
        if drone_uuid is None:
            logger.error("command_record_skipped_unknown_drone", drone_id=drone_id)
            return None

        async with session_scope_optional() as session:
            if session is None:
                # The command still goes out -- losing the database must not
                # ground the fleet -- but the gap is logged loudly.
                logger.error(
                    "command_not_recorded_database_unavailable",
                    drone_id=drone_id, command=str(command_type),
                )
                return None
            record = DroneCommand(
                drone_uuid=drone_uuid,
                mission_id=mission_id,
                operator_id=operator_id,
                command_type=command_type,
                state=CommandState.REQUESTED,
                idempotency_key=idempotency_key,
                request_id=request_id,
                parameters=_serialisable(parameters),
                requested_at=datetime.now(UTC),
                timeout_s=timeout_s,
                preconditions={"checks": [c.as_dict() for c in checks]},
            )
            session.add(record)
            try:
                await session.flush()
            except IntegrityError:
                # Lost a race against a concurrent identical request; the
                # winner owns the physical command.
                await session.rollback()
                logger.warning("command_idempotency_race", idempotency_key=idempotency_key)
                return None
            session.add(
                CommandTransition(
                    command_id=record.id,
                    occurred_at=datetime.now(UTC),
                    state=CommandState.REQUESTED,
                    detail={"parameters": _serialisable(parameters)},
                )
            )
            return record

    async def _transition(
        self, command_id: uuid.UUID | None, state: CommandState, detail: dict[str, Any]
    ) -> None:
        if command_id is None:
            return
        async with session_scope_optional() as session:
            if session is None:
                return
            record = await session.get(DroneCommand, command_id)
            if record is None:
                return
            record.state = state
            now = datetime.now(UTC)
            if state is CommandState.SENT_TO_PX4:
                record.sent_at = now
            elif state is CommandState.ACKNOWLEDGED:
                record.acknowledged_at = now
                record.acknowledgement = _serialisable(detail)
            elif state is CommandState.STATE_CHANGED:
                record.state_changed_at = now
                record.verification = _serialisable(detail)
            session.add(
                CommandTransition(
                    command_id=command_id,
                    occurred_at=now,
                    state=state,
                    detail=_serialisable(detail),
                )
            )

    async def _finalise(
        self, record: DroneCommand | None, outcome: CommandOutcome, detail: dict[str, Any]
    ) -> None:
        outcome.completed_at = datetime.now(UTC)
        if record is None:
            return
        async with session_scope_optional() as session:
            if session is None:
                return
            row = await session.get(DroneCommand, record.id)
            if row is None:
                return
            row.state = outcome.state
            row.completed_at = outcome.completed_at
            row.verified = outcome.verified
            row.verification = _serialisable(outcome.verification)
            row.acknowledgement = _serialisable(
                {**detail.get("acknowledgement", {}), "result_code": outcome.result_code}
            )
            if outcome.state is not CommandState.COMPLETED:
                row.failure_reason = (outcome.detail or outcome.result_code)[:255]
            session.add(
                CommandTransition(
                    command_id=row.id,
                    occurred_at=outcome.completed_at,
                    state=outcome.state,
                    detail=_serialisable({"result_code": outcome.result_code,
                                          "detail": outcome.detail}),
                )
            )

    async def _audit(
        self,
        outcome: CommandOutcome,
        principal: TokenPrincipal | None,
        mission_id: uuid.UUID | None,
        request_id: str | None,
        source_ip: str | None,
    ) -> None:
        await self._events.audit(
            action=f"{outcome.command_type}_COMMAND",
            result=str(outcome.state),
            operator_id=principal.operator_id if principal else None,
            operator_username=principal.username if principal else None,
            operator_role=str(principal.role) if principal else None,
            mission_id=mission_id,
            drone_uuid=self._fleet.drone_uuid(outcome.drone_id),
            command_id=outcome.command_id,
            request_id=request_id,
            source_ip=source_ip,
            acknowledgement=outcome.result_code,
            failure_reason=None if outcome.success else outcome.detail,
            detail={
                "verified": outcome.verified,
                "acknowledged": outcome.acknowledged,
                "preconditions": [c.as_dict() for c in outcome.preconditions],
                "verification": outcome.verification,
            },
        )

    def _publish(self, outcome: CommandOutcome) -> None:
        self._bus.emit(
            EventType.COMMAND_UPDATED,
            drone_id=outcome.drone_id,
            payload={
                "command_id": str(outcome.command_id) if outcome.command_id else None,
                "command_type": str(outcome.command_type),
                "state": str(outcome.state),
                "acknowledged": outcome.acknowledged,
                "verified": outcome.verified,
                "result_code": outcome.result_code,
                "detail": outcome.detail,
            },
        )

    def _timeout_for(self, command_type: CommandType) -> float:
        cfg = self._settings.command
        return {
            CommandType.ARM: cfg.arm_timeout_s,
            CommandType.DISARM: cfg.arm_timeout_s,
            CommandType.TAKEOFF: cfg.takeoff_timeout_s,
            CommandType.LAND: cfg.land_timeout_s,
            CommandType.RTL: cfg.rtl_timeout_s,
            CommandType.UPLOAD_MISSION: cfg.mission_upload_timeout_s,
            CommandType.UPLOAD_GEOFENCE: cfg.mission_upload_timeout_s,
        }.get(command_type, cfg.default_timeout_s)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def history(
        self,
        session: AsyncSession,
        drone_uuid: uuid.UUID | None = None,
        mission_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[DroneCommand]:
        stmt = select(DroneCommand).order_by(DroneCommand.created_at.desc())
        if drone_uuid is not None:
            stmt = stmt.where(DroneCommand.drone_uuid == drone_uuid)
        if mission_id is not None:
            stmt = stmt.where(DroneCommand.mission_id == mission_id)
        result = await session.execute(stmt.limit(min(limit, 500)))
        return list(result.scalars().all())


def _serialisable(value: Any) -> Any:
    """Coerce a payload into something JSONB will accept.

    Mission item and geofence specs are dataclasses; they are flattened rather
    than dropped so the record shows exactly what was uploaded.
    """
    if isinstance(value, dict):
        return {str(k): _serialisable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_serialisable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _serialisable(getattr(value, name))
            for name in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    return str(value)
