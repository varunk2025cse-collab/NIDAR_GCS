"""MissionManager -- the mission lifecycle over real aircraft.

Owns the validated mission state machine and the multi-aircraft workflows that
sit on top of it: start, pause, resume, abort and RTL-all.

Two principles run through everything here:

* A transition happens only when it is legal *and* the real aircraft did what
  was asked. A start that fails to arm D2 does not silently become a mission
  with two aircraft.
* Abort and RTL report what actually happened per aircraft. If two of three
  aircraft acknowledge and one does not, the mission ends in
  PARTIAL_ABORT_FAILURE with the detail attached -- never in a clean ABORTED.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point
from shapely.geometry import Polygon as ShapelyPolygon
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DroneRole, Settings
from app.core.enums import (
    MISSION_ACTIVE_STATES,
    MISSION_TRANSITIONS,
    AlertSeverity,
    CommandType,
    MissionState,
)
from app.core.exceptions import (
    InvalidStateTransitionError,
    MissionNotFoundError,
    PreflightFailedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import TokenPrincipal
from app.models.mission import Mission, MissionDroneAssignment
from app.realtime.event_bus import EventBus, EventType
from app.services.command_service import CommandOutcome, CommandService
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService
from app.services.preflight import PreflightReport, PreflightService
from app.services.safety_engine import SafetyEngine

logger = get_logger(__name__)


@dataclass(slots=True)
class DroneActionResult:
    """What one aircraft actually did in a fleet-wide workflow."""

    drone_id: str
    requested: str
    state: str
    acknowledged: bool
    verified: bool
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.verified

    def as_dict(self) -> dict[str, Any]:
        return {
            "drone_id": self.drone_id,
            "requested": self.requested,
            "state": self.state,
            "acknowledged": self.acknowledged,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(slots=True)
class WorkflowResult:
    workflow: str
    mission_id: uuid.UUID
    mission_state: MissionState
    results: list[DroneActionResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    detail: str | None = None

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "mission_id": str(self.mission_id),
            "mission_state": str(self.mission_state),
            "all_succeeded": self.all_ok,
            "results": [r.as_dict() for r in self.results],
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "detail": self.detail,
        }


class MissionManager:
    def __init__(
        self,
        fleet: FleetManager,
        commands: CommandService,
        preflight: PreflightService,
        geofence: GeofenceService,
        safety: SafetyEngine,
        events: EventService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._commands = commands
        self._preflight = preflight
        self._geofence = geofence
        self._safety = safety
        self._events = events
        self._bus = bus
        self._settings = settings
        #: One workflow at a time per mission -- a second ABORT while the first
        #: is running joins the first rather than starting a rival one.
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_workflows: dict[str, str] = {}

    def _lock(self, mission_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(str(mission_id), asyncio.Lock())

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        principal: TokenPrincipal,
        description: str | None = None,
        launch_point: tuple[float, float] | None = None,
        launch_altitude_amsl_m: float | None = None,
        search_area: list[tuple[float, float]] | None = None,
        search_altitude_m: float | None = None,
        delivery_altitude_m: float | None = None,
        max_duration_s: int | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Mission:
        mission = Mission(
            name=name,
            description=description,
            state=MissionState.DRAFT,
            created_by=principal.operator_id,
            launch_point=(
                from_shape(Point(launch_point[1], launch_point[0]), srid=4326)
                if launch_point
                else None
            ),
            launch_altitude_amsl_m=launch_altitude_amsl_m,
            search_area=(
                from_shape(
                    ShapelyPolygon([(lon, lat) for lat, lon in search_area]), srid=4326
                )
                if search_area and len(search_area) >= 3
                else None
            ),
            search_altitude_m=search_altitude_m,
            delivery_altitude_m=delivery_altitude_m,
            max_duration_s=max_duration_s or self._settings.safety.mission_max_duration_s,
            state_changed_at=datetime.now(UTC),
            parameters=parameters or {},
        )
        session.add(mission)
        await session.flush()
        await self._events.record(
            event_type="MISSION_CREATED",
            message=f"Mission {name} created",
            mission_id=mission.id,
            operator_id=principal.operator_id,
            session=session,
            data={"name": name},
        )
        logger.info("mission_created", mission_id=str(mission.id), name=name)
        return mission

    async def get(self, session: AsyncSession, mission_id: uuid.UUID) -> Mission:
        mission = await session.get(Mission, mission_id)
        if mission is None:
            raise MissionNotFoundError(
                f"Mission {mission_id} does not exist", details={"mission_id": str(mission_id)}
            )
        return mission

    async def list_missions(
        self, session: AsyncSession, limit: int = 50, offset: int = 0
    ) -> list[Mission]:
        result = await session.execute(
            select(Mission)
            .order_by(Mission.created_at.desc())
            .limit(min(limit, 200))
            .offset(max(offset, 0))
        )
        return list(result.scalars().all())

    async def active_mission(self, session: AsyncSession) -> Mission | None:
        result = await session.execute(
            select(Mission)
            .where(Mission.state.in_(list(MISSION_ACTIVE_STATES)))
            .order_by(Mission.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def assign_drones(
        self, session: AsyncSession, mission: Mission, drone_ids: list[str]
    ) -> list[MissionDroneAssignment]:
        assignments: list[MissionDroneAssignment] = []
        existing = await session.execute(
            select(MissionDroneAssignment).where(
                MissionDroneAssignment.mission_id == mission.id
            )
        )
        known = {a.drone_uuid for a in existing.scalars().all()}

        for drone_id in drone_ids:
            drone_uuid = self._fleet.require_drone_uuid(drone_id)
            if drone_uuid in known:
                continue
            state = self._fleet.state(drone_id)
            assignment = MissionDroneAssignment(
                mission_id=mission.id,
                drone_uuid=drone_uuid,
                assigned_role=str(state.role),
            )
            session.add(assignment)
            assignments.append(assignment)
            self._fleet.assign_mission(drone_id, str(mission.id))
        await session.flush()
        logger.info("mission_drones_assigned", mission_id=str(mission.id), drones=drone_ids)
        return assignments

    async def mission_drone_ids(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> list[str]:
        result = await session.execute(
            select(MissionDroneAssignment.drone_uuid).where(
                MissionDroneAssignment.mission_id == mission_id,
                MissionDroneAssignment.released_at.is_(None),
            )
        )
        ids: list[str] = []
        for (drone_uuid,) in result.all():
            drone_id = self._fleet.drone_id_for_uuid(drone_uuid)
            if drone_id:
                ids.append(drone_id)
        return ids

    # ------------------------------------------------------------------
    # state machine
    # ------------------------------------------------------------------
    async def transition(
        self,
        session: AsyncSession,
        mission: Mission,
        to_state: MissionState,
        *,
        reason: str | None = None,
        operator_id: uuid.UUID | None = None,
        data: dict[str, Any] | None = None,
    ) -> Mission:
        """Apply a validated state transition.

        Illegal transitions raise. There is no force parameter: a mission that
        has COMPLETED cannot be pushed back into TAKEOFF.
        """
        current = mission.state
        allowed = MISSION_TRANSITIONS.get(current, frozenset())
        if to_state not in allowed:
            raise InvalidStateTransitionError("Mission", str(current), str(to_state))

        mission.state = to_state
        mission.state_changed_at = datetime.now(UTC)

        if to_state is MissionState.ARMING and mission.started_at is None:
            mission.started_at = datetime.now(UTC)
            self._safety.track_mission(
                str(mission.id),
                mission.started_at,
                mission.max_duration_s or self._settings.safety.mission_max_duration_s,
            )
        if to_state in (
            MissionState.COMPLETED,
            MissionState.ABORTED,
            MissionState.PARTIAL_ABORT_FAILURE,
            MissionState.FAILED,
        ):
            mission.ended_at = datetime.now(UTC)
            self._safety.untrack_mission(str(mission.id))
            self._fleet.release_mission(str(mission.id))
            self._geofence.clear_mission(mission.id)
        if to_state is MissionState.ABORTED and reason:
            mission.abort_reason = reason[:255]
        if to_state is MissionState.FAILED and reason:
            mission.failure_reason = reason[:255]

        await session.flush()
        await self._events.record(
            event_type="MISSION_STATE_CHANGED",
            message=f"Mission {current} -> {to_state}" + (f": {reason}" if reason else ""),
            severity=(
                AlertSeverity.CRITICAL
                if to_state
                in (MissionState.EMERGENCY, MissionState.FAILED,
                    MissionState.PARTIAL_ABORT_FAILURE)
                else AlertSeverity.INFO
            ),
            mission_id=mission.id,
            operator_id=operator_id,
            session=session,
            data={"from_state": str(current), "to_state": str(to_state),
                  "reason": reason, **(data or {})},
        )
        self._bus.emit(
            EventType.MISSION_STATE_CHANGED,
            mission_id=str(mission.id),
            payload={
                "from_state": str(current),
                "to_state": str(to_state),
                "reason": reason,
                "mission_name": mission.name,
                **(data or {}),
            },
        )
        logger.info(
            "mission_state_changed",
            mission_id=str(mission.id),
            from_state=str(current),
            to_state=str(to_state),
            reason=reason,
        )
        return mission

    # ------------------------------------------------------------------
    # preflight
    # ------------------------------------------------------------------
    async def run_preflight(
        self, session: AsyncSession, mission: Mission
    ) -> PreflightReport:
        await self._geofence.load_for_mission(session, mission.id)
        drone_ids = await self.mission_drone_ids(session, mission.id)
        report = await self._preflight.run(session, mission, drone_ids)
        await self._events.record(
            event_type="PREFLIGHT_RUN",
            message=(
                "Preflight passed"
                if report.ready
                else f"Preflight failed: {len(report.failures)} blocking check(s)"
            ),
            severity=AlertSeverity.INFO if report.ready else AlertSeverity.WARNING,
            mission_id=mission.id,
            session=session,
            data=report.as_dict(),
        )
        return report

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    async def start(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> tuple[WorkflowResult, PreflightReport]:
        """Preflight, then arm and take off the assigned aircraft.

        The whole start is gated on preflight. If any aircraft fails to arm or
        take off, the mission does not proceed to SEARCHING -- it is left in a
        state the operator must resolve, with the per-aircraft detail attached.
        """
        async with self._lock(mission.id):
            report = await self.run_preflight(session, mission)
            if mission.state is MissionState.DRAFT:
                await self.transition(session, mission, MissionState.READY,
                                      operator_id=principal.operator_id)
            if mission.state is MissionState.READY:
                await self.transition(session, mission, MissionState.PRECHECK,
                                      operator_id=principal.operator_id)

            if not report.ready:
                await self.transition(
                    session, mission, MissionState.READY,
                    reason="preflight failed", operator_id=principal.operator_id,
                )
                raise PreflightFailedError(
                    "Mission start blocked by preflight",
                    details=report.as_dict(),
                )

            drone_ids = await self.mission_drone_ids(session, mission.id)
            result = WorkflowResult(
                workflow="MISSION_START",
                mission_id=mission.id,
                mission_state=mission.state,
            )

            await self.transition(session, mission, MissionState.ARMING,
                                  operator_id=principal.operator_id)
            await self._upload_geofences(mission, drone_ids, principal, request_id, result)

            # Arm every aircraft before taking any of them off, so a failure to
            # arm is discovered while everything is still on the ground.
            arm_results = await self._fan_out(
                drone_ids, CommandType.ARM, mission, principal, request_id
            )
            result.results.extend(arm_results)
            if not all(r.ok for r in arm_results):
                failed = [r.drone_id for r in arm_results if not r.ok]
                await self._disarm_after_failed_start(drone_ids, mission, principal, request_id)
                await self.transition(
                    session, mission, MissionState.FAILED,
                    reason=f"arming failed for {', '.join(failed)}",
                    operator_id=principal.operator_id,
                )
                result.mission_state = mission.state
                result.detail = f"Arming failed for {', '.join(failed)}"
                result.completed_at = datetime.now(UTC)
                return result, report

            await self.transition(session, mission, MissionState.TAKEOFF,
                                  operator_id=principal.operator_id)
            takeoff_results = await self._fan_out(
                drone_ids,
                CommandType.TAKEOFF,
                mission,
                principal,
                request_id,
                parameters={"altitude_m": mission.search_altitude_m},
            )
            result.results.extend(takeoff_results)

            if not all(r.ok for r in takeoff_results):
                failed = [r.drone_id for r in takeoff_results if not r.ok]
                result.detail = (
                    f"Takeoff not confirmed for {', '.join(failed)}; "
                    f"mission held in TAKEOFF for operator decision"
                )
                logger.error("mission_takeoff_incomplete", mission_id=str(mission.id),
                             failed=failed)
                result.mission_state = mission.state
                result.completed_at = datetime.now(UTC)
                return result, report

            await self.transition(session, mission, MissionState.SEARCHING,
                                  operator_id=principal.operator_id)
            result.mission_state = mission.state
            result.completed_at = datetime.now(UTC)
            return result, report

    async def _upload_geofences(
        self,
        mission: Mission,
        drone_ids: list[str],
        principal: TokenPrincipal,
        request_id: str | None,
        result: WorkflowResult,
    ) -> None:
        """Push the mission fence to each aircraft.

        PX4 then enforces it onboard, which is what keeps the boundary
        effective when the link to the GCS is down.
        """
        specs = self._geofence.upload_specs(mission.id)
        if not specs:
            return
        outcomes = await self._fan_out(
            drone_ids,
            CommandType.UPLOAD_GEOFENCE,
            mission,
            principal,
            request_id,
            parameters={"polygons": specs},
        )
        result.results.extend(outcomes)

    async def _disarm_after_failed_start(
        self,
        drone_ids: list[str],
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None,
    ) -> None:
        """Put armed-but-not-flying aircraft back to safe after a failed start."""
        for drone_id in drone_ids:
            state = self._fleet.state(drone_id)
            if state.armed.value is True and state.in_air.value is not True:
                try:
                    await self._commands.execute(
                        drone_id=drone_id,
                        command_type=CommandType.DISARM,
                        principal=principal,
                        mission_id=mission.id,
                        request_id=request_id,
                        idempotency_key=f"{mission.id}:startfail-disarm:{drone_id}",
                    )
                except Exception as exc:
                    logger.error("post_failure_disarm_failed", drone_id=drone_id,
                                 error=str(exc))

    # ------------------------------------------------------------------
    # pause / resume
    # ------------------------------------------------------------------
    async def pause(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> WorkflowResult:
        async with self._lock(mission.id):
            drone_ids = await self.mission_drone_ids(session, mission.id)
            outcomes = await self._fan_out(
                drone_ids, CommandType.PAUSE_MISSION, mission, principal, request_id
            )
            result = WorkflowResult(
                workflow="MISSION_PAUSE",
                mission_id=mission.id,
                mission_state=mission.state,
                results=outcomes,
            )
            if result.any_ok:
                await self.transition(session, mission, MissionState.PAUSED,
                                      operator_id=principal.operator_id)
            else:
                result.detail = "No aircraft confirmed the pause; mission state unchanged"
            result.mission_state = mission.state
            result.completed_at = datetime.now(UTC)
            return result

    async def resume(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> WorkflowResult:
        async with self._lock(mission.id):
            if mission.state is not MissionState.PAUSED:
                raise InvalidStateTransitionError(
                    "Mission", str(mission.state), str(MissionState.SEARCHING)
                )
            drone_ids = await self.mission_drone_ids(session, mission.id)
            outcomes = await self._fan_out(
                drone_ids, CommandType.RESUME_MISSION, mission, principal, request_id
            )
            result = WorkflowResult(
                workflow="MISSION_RESUME",
                mission_id=mission.id,
                mission_state=mission.state,
                results=outcomes,
            )
            if result.any_ok:
                await self.transition(session, mission, MissionState.SEARCHING,
                                      operator_id=principal.operator_id)
            else:
                result.detail = "No aircraft confirmed the resume; mission stays PAUSED"
            result.mission_state = mission.state
            result.completed_at = datetime.now(UTC)
            return result

    # ------------------------------------------------------------------
    # abort
    # ------------------------------------------------------------------
    async def abort(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        reason: str,
        request_id: str | None = None,
    ) -> WorkflowResult:
        """Emergency abort of a live mission.

        The safe action for an airframe that is flying is RTL, not a
        disarm-in-place. Aircraft still on the ground are disarmed. Every
        aircraft result is recorded individually, and the mission only reaches
        ABORTED if every one of them complied.
        """
        mission_key = str(mission.id)
        if self._active_workflows.get(mission_key) == "ABORT":
            logger.info("abort_already_running", mission_id=mission_key)

        async with self._lock(mission.id):
            self._active_workflows[mission_key] = "ABORT"
            try:
                if mission.state not in MISSION_ACTIVE_STATES:
                    raise InvalidStateTransitionError(
                        "Mission", str(mission.state), str(MissionState.ABORTING)
                    )

                await self._events.audit(
                    action="ABORT_COMMAND",
                    result="REQUESTED",
                    operator_id=principal.operator_id,
                    operator_username=principal.username,
                    operator_role=str(principal.role),
                    mission_id=mission.id,
                    request_id=request_id,
                    detail={"reason": reason},
                    session=session,
                )
                await self.transition(
                    session, mission, MissionState.ABORTING,
                    reason=reason, operator_id=principal.operator_id,
                )

                drone_ids = await self.mission_drone_ids(session, mission.id)
                result = WorkflowResult(
                    workflow="MISSION_ABORT",
                    mission_id=mission.id,
                    mission_state=mission.state,
                    detail=reason,
                )

                tasks = [
                    self._abort_one(drone_id, mission, principal, request_id)
                    for drone_id in drone_ids
                ]
                result.results = list(await asyncio.gather(*tasks))

                final = (
                    MissionState.ABORTED
                    if result.all_ok
                    else MissionState.PARTIAL_ABORT_FAILURE
                )
                await self.transition(
                    session, mission, final, reason=reason,
                    operator_id=principal.operator_id,
                    data={"results": [r.as_dict() for r in result.results]},
                )
                result.mission_state = mission.state
                result.completed_at = datetime.now(UTC)

                await self._events.audit(
                    action="ABORT_COMMAND",
                    result=str(final),
                    operator_id=principal.operator_id,
                    operator_username=principal.username,
                    operator_role=str(principal.role),
                    mission_id=mission.id,
                    request_id=request_id,
                    failure_reason=(
                        None
                        if result.all_ok
                        else "; ".join(
                            f"{r.drone_id}: {r.detail or r.state}"
                            for r in result.results
                            if not r.ok
                        )
                    ),
                    detail=result.as_dict(),
                    session=session,
                )
                logger.warning(
                    "mission_aborted",
                    mission_id=mission_key,
                    final_state=str(final),
                    results=[r.as_dict() for r in result.results],
                )
                return result
            finally:
                self._active_workflows.pop(mission_key, None)

    async def _abort_one(
        self,
        drone_id: str,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None,
    ) -> DroneActionResult:
        state = self._fleet.state(drone_id)
        # An airborne aircraft is sent home. One on the ground is disarmed.
        # Never the other way round.
        if state.in_air.value is True or state.armed.value is not False:
            command = (
                CommandType.RTL if state.in_air.value is True else CommandType.DISARM
            )
        else:
            return DroneActionResult(
                drone_id=drone_id,
                requested="NONE",
                state="ALREADY_SAFE",
                acknowledged=True,
                verified=True,
                detail="Aircraft was already disarmed and on the ground",
            )
        return await self._execute_one(
            drone_id,
            command,
            mission,
            principal,
            request_id,
            idempotency_key=f"{mission.id}:abort:{drone_id}",
        )

    # ------------------------------------------------------------------
    # RTL
    # ------------------------------------------------------------------
    async def rtl_all(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> WorkflowResult:
        """Send every mission aircraft home.

        PX4 flies the return; the GCS tracks whether each aircraft actually
        entered RTL and reports the ones that did not.
        """
        async with self._lock(mission.id):
            drone_ids = await self.mission_drone_ids(session, mission.id)
            outcomes = await asyncio.gather(
                *(
                    self._execute_one(
                        drone_id,
                        CommandType.RTL,
                        mission,
                        principal,
                        request_id,
                        idempotency_key=f"{mission.id}:rtl-all:{drone_id}",
                        allow_degraded_link=True,
                    )
                    for drone_id in drone_ids
                )
            )
            result = WorkflowResult(
                workflow="RTL_ALL",
                mission_id=mission.id,
                mission_state=mission.state,
                results=list(outcomes),
            )
            if result.any_ok and mission.state in MISSION_TRANSITIONS and (
                MissionState.RTL in MISSION_TRANSITIONS[mission.state]
            ):
                await self.transition(session, mission, MissionState.RTL,
                                      operator_id=principal.operator_id)
            if not result.all_ok:
                result.detail = "; ".join(
                    f"{r.drone_id}: {r.detail or r.state}" for r in result.results if not r.ok
                )
            result.mission_state = mission.state
            result.completed_at = datetime.now(UTC)
            return result

    async def complete(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal | None = None,
    ) -> Mission:
        """Close out a mission whose aircraft are home and disarmed."""
        airborne = [
            s.drone_id
            for s in self._fleet.drones_for_mission(str(mission.id))
            if s.in_air.value is True
        ]
        if airborne:
            raise ValidationError(
                "Mission cannot be completed while aircraft are still airborne",
                details={"airborne": airborne},
            )
        return await self.transition(
            session,
            mission,
            MissionState.COMPLETED,
            operator_id=principal.operator_id if principal else None,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _fan_out(
        self,
        drone_ids: list[str],
        command_type: CommandType,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None,
        parameters: dict[str, Any] | None = None,
    ) -> list[DroneActionResult]:
        results = await asyncio.gather(
            *(
                self._execute_one(
                    drone_id,
                    command_type,
                    mission,
                    principal,
                    request_id,
                    parameters=parameters,
                    idempotency_key=f"{mission.id}:{command_type}:{drone_id}",
                )
                for drone_id in drone_ids
            )
        )
        return list(results)

    async def _execute_one(
        self,
        drone_id: str,
        command_type: CommandType,
        mission: Mission,
        principal: TokenPrincipal,
        request_id: str | None,
        parameters: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_degraded_link: bool = False,
    ) -> DroneActionResult:
        try:
            outcome: CommandOutcome = await self._commands.execute(
                drone_id=drone_id,
                command_type=command_type,
                principal=principal,
                mission_id=mission.id,
                parameters=parameters,
                request_id=request_id,
                idempotency_key=idempotency_key,
                allow_degraded_link=allow_degraded_link,
            )
            return DroneActionResult(
                drone_id=drone_id,
                requested=str(command_type),
                state=str(outcome.state),
                acknowledged=outcome.acknowledged,
                verified=outcome.verified,
                detail=outcome.detail,
            )
        except Exception as exc:
            # A failure on one aircraft must not stop the workflow reaching the
            # others -- especially during an abort.
            logger.error(
                "workflow_command_failed",
                drone_id=drone_id,
                command=str(command_type),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return DroneActionResult(
                drone_id=drone_id,
                requested=str(command_type),
                state="FAILED",
                acknowledged=False,
                verified=False,
                detail=str(exc),
            )

    # ------------------------------------------------------------------
    # timing / status
    # ------------------------------------------------------------------
    def timing(self, mission: Mission) -> dict[str, Any]:
        max_duration = mission.max_duration_s or self._settings.safety.mission_max_duration_s
        if mission.started_at is None:
            return {
                "started_at": None,
                "elapsed_s": None,
                "remaining_s": None,
                "max_duration_s": max_duration,
                "expired": False,
            }
        end = mission.ended_at or datetime.now(UTC)
        elapsed = (end - mission.started_at).total_seconds()
        remaining = max_duration - elapsed
        return {
            "started_at": mission.started_at.isoformat(),
            "ended_at": mission.ended_at.isoformat() if mission.ended_at else None,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": round(remaining, 1),
            "max_duration_s": max_duration,
            "expired": remaining <= 0,
        }

    async def status(self, session: AsyncSession, mission: Mission) -> dict[str, Any]:
        drone_ids = await self.mission_drone_ids(session, mission.id)
        return {
            "mission_id": str(mission.id),
            "name": mission.name,
            "state": str(mission.state),
            "state_changed_at": (
                mission.state_changed_at.isoformat() if mission.state_changed_at else None
            ),
            "timing": self.timing(mission),
            "drones": [self._fleet.card(d) for d in drone_ids
                       if self._fleet.connection_manager.has(d)],
            "launch_point": _point_to_latlon(mission.launch_point),
            "search_altitude_m": mission.search_altitude_m,
            "delivery_altitude_m": mission.delivery_altitude_m,
            "abort_reason": mission.abort_reason,
            "failure_reason": mission.failure_reason,
        }

    def scouts_for_mission(self, mission_id: uuid.UUID) -> list[str]:
        return [
            s.drone_id
            for s in self._fleet.drones_for_mission(str(mission_id))
            if s.role is DroneRole.SCOUT
        ]


def _point_to_latlon(geom: Any) -> dict[str, float] | None:
    if geom is None:
        return None
    shape = to_shape(geom)
    return {"latitude": shape.y, "longitude": shape.x}
