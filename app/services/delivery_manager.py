"""DeliveryManager -- dispatching a real aircraft to a real survivor.

The safety gate is the important part. Before D3 is sent anywhere, this
service works out from live telemetry whether the aircraft can get there,
hover, release and come back with the configured reserve still in the pack.
If the answer is no, the delivery is rejected with the numbers attached -- it
is never attempted "to see".

State advances only on evidence:

* EN_ROUTE when the aircraft is actually moving under the uploaded plan.
* AT_TARGET when its real position is within the arrival radius.
* DELIVERED only when a :mod:`delivery_confirmation` provider attests to a
  physical release.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.energy import EnergyEstimate, EnergyModelConfig
from app.core.enums import (
    DELIVERY_ACTIVE_STATES,
    DELIVERY_TRANSITIONS,
    AlertSeverity,
    CheckStatus,
    CommandType,
    DeliveryConfirmationSource,
    DeliveryState,
    SurvivorState,
    TelemetryStatus,
)
from app.core.exceptions import (
    ConflictError,
    DeliveryRejectedUnsafeError,
    InvalidStateTransitionError,
    NotFoundError,
)
from app.core.geo import haversine_m
from app.core.logging import get_logger
from app.core.security import TokenPrincipal
from app.drone.state import DroneState
from app.models.delivery import DeliveryEvent, DeliveryTask
from app.models.mission import Mission
from app.models.survivor import Survivor
from app.realtime.event_bus import EventBus, EventType
from app.services.command_service import CommandService
from app.services.delivery_confirmation import Confirmation, ConfirmationRegistry
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService
from app.services.survivor_manager import SurvivorManager

logger = get_logger(__name__)

#: How close the aircraft must actually be to count as AT_TARGET.
ARRIVAL_RADIUS_M = 15.0


@dataclass(slots=True)
class SafetyCheck:
    name: str
    status: CheckStatus
    reason: str | None = None
    observed: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": str(self.status),
            "reason": self.reason,
            "observed": self.observed,
        }


@dataclass(slots=True)
class DeliverySafetyReport:
    drone_id: str
    checks: list[SafetyCheck] = field(default_factory=list)
    distance_m: float | None = None
    estimated_duration_s: float | None = None
    estimated_battery_cost_pct: float | None = None
    battery_after_pct: float | None = None
    #: The full energy estimate, including whether the model behind it was
    #: ever measured on a real airframe.
    energy_estimate: EnergyEstimate | None = None

    @property
    def failures(self) -> list[SafetyCheck]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def safe(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "drone_id": self.drone_id,
            "safe": self.safe,
            "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
            "estimated_duration_s": (
                round(self.estimated_duration_s, 1)
                if self.estimated_duration_s is not None
                else None
            ),
            "estimated_battery_cost_pct": (
                round(self.estimated_battery_cost_pct, 1)
                if self.estimated_battery_cost_pct is not None
                else None
            ),
            "battery_after_pct": (
                round(self.battery_after_pct, 1)
                if self.battery_after_pct is not None
                else None
            ),
            "energy_model": (
                self.energy_estimate.as_dict() if self.energy_estimate else None
            ),
            "energy_model_calibrated": (
                self.energy_estimate.calibrated if self.energy_estimate else None
            ),
            "checks": [c.as_dict() for c in self.checks],
        }


class DeliveryManager:
    def __init__(
        self,
        fleet: FleetManager,
        commands: CommandService,
        survivors: SurvivorManager,
        geofence: GeofenceService,
        events: EventService,
        confirmations: ConfirmationRegistry,
        bus: EventBus,
        settings: Settings,
        energy_model: EnergyModelConfig | None = None,
    ) -> None:
        self._fleet = fleet
        self._commands = commands
        self._survivors = survivors
        self._geofence = geofence
        self._events = events
        self._confirmations = confirmations
        self._bus = bus
        self._settings = settings
        # Defaults to an explicitly uncalibrated model, which blocks dispatch.
        self._energy = energy_model or EnergyModelConfig()
        self._monitors: dict[str, asyncio.Task[None]] = {}

    @property
    def energy_model(self) -> EnergyModelConfig:
        return self._energy

    # ------------------------------------------------------------------
    # safety evaluation
    # ------------------------------------------------------------------
    def evaluate_safety(
        self,
        state: DroneState,
        target_latitude: float,
        target_longitude: float,
        mission_id: uuid.UUID | str | None = None,
    ) -> DeliverySafetyReport:
        """Can this aircraft, right now, complete this delivery and return?

        Every input is live telemetry. A missing value is a FAIL, not an
        assumption.
        """
        report = DeliverySafetyReport(drone_id=state.drone_id)
        safety = self._settings.safety
        policy = self._fleet.policy

        # -- link ------------------------------------------------------------
        report.checks.append(
            SafetyCheck(
                "CONNECTION",
                CheckStatus.PASS if state.is_commandable else CheckStatus.FAIL,
                None if state.is_commandable else f"Link state is {state.connection_state}",
                observed=str(state.connection_state),
            )
        )

        # -- telemetry freshness ----------------------------------------------
        age = state.contact_age_s()
        fresh = age is not None and age <= safety.command_requires_telemetry_age_s
        report.checks.append(
            SafetyCheck(
                "TELEMETRY",
                CheckStatus.PASS if fresh else CheckStatus.FAIL,
                None if fresh else f"Telemetry age {age}s exceeds the command limit",
                observed=round(age, 2) if age is not None else None,
            )
        )

        # -- mission state ------------------------------------------------------
        busy = state.delivery_task_id is not None
        report.checks.append(
            SafetyCheck(
                "AIRCRAFT_AVAILABLE",
                CheckStatus.FAIL if busy else CheckStatus.PASS,
                f"Already assigned to delivery {state.delivery_task_id}" if busy else None,
                observed=state.delivery_task_id,
            )
        )

        # -- position ------------------------------------------------------------
        position = state.position.value
        position_fresh = (
            policy.status("position", state.position) is not TelemetryStatus.NO_DATA
        )
        if position is None or not position_fresh:
            report.checks.append(
                SafetyCheck(
                    "CURRENT_LOCATION", CheckStatus.FAIL,
                    "No current position telemetry; distance cannot be computed",
                )
            )
            return report

        distance = haversine_m(
            position.latitude, position.longitude, target_latitude, target_longitude
        )
        report.distance_m = distance
        report.checks.append(
            SafetyCheck("CURRENT_LOCATION", CheckStatus.PASS,
                        observed={"latitude": position.latitude,
                                  "longitude": position.longitude})
        )
        report.checks.append(
            SafetyCheck(
                "DISTANCE",
                CheckStatus.PASS
                if distance <= safety.max_delivery_distance_m
                else CheckStatus.FAIL,
                None
                if distance <= safety.max_delivery_distance_m
                else (
                    f"Target is {distance:.0f} m away, beyond the "
                    f"{safety.max_delivery_distance_m:.0f} m limit"
                ),
                observed=round(distance, 1),
            )
        )

        # -- GPS -------------------------------------------------------------------
        gps = state.gps.value
        if gps is None:
            report.checks.append(SafetyCheck("GPS", CheckStatus.FAIL, "No GPS telemetry"))
        elif gps.fix_type < safety.min_gps_fix_type or gps.satellites < safety.min_satellites:
            report.checks.append(
                SafetyCheck(
                    "GPS", CheckStatus.FAIL,
                    f"GPS {gps.fix_type_name} with {gps.satellites} satellites",
                    observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
                )
            )
        else:
            report.checks.append(
                SafetyCheck("GPS", CheckStatus.PASS,
                            observed={"fix": gps.fix_type_name,
                                      "satellites": gps.satellites})
            )

        # -- health --------------------------------------------------------------
        health = state.health.value
        if health is None:
            report.checks.append(SafetyCheck("HEALTH", CheckStatus.FAIL,
                                             "No health telemetry"))
        elif not health.global_position_ok or not health.home_position_ok:
            report.checks.append(
                SafetyCheck("HEALTH", CheckStatus.FAIL,
                            "Position or home estimate is unhealthy",
                            observed=health.as_dict())
            )
        else:
            report.checks.append(SafetyCheck("HEALTH", CheckStatus.PASS,
                                             observed=health.as_dict()))

        # -- energy budget ---------------------------------------------------------
        battery = state.battery.value
        if battery is None or battery.remaining_percent is None:
            report.checks.append(
                SafetyCheck("BATTERY", CheckStatus.FAIL,
                            "No battery percentage; the return trip cannot be budgeted")
            )
            report.checks.append(
                SafetyCheck("RETURN_CAPABILITY", CheckStatus.FAIL,
                            "Cannot verify the aircraft can return")
            )
        else:
            percent = battery.remaining_percent
            config = self._fleet.drone_config(state.drone_id)
            estimate = self._energy.estimate(
                distance, payload_g=config.payload_capacity_g if config else None
            )
            remaining_after = estimate.remaining_after(percent)

            report.energy_estimate = estimate
            report.estimated_battery_cost_pct = estimate.total_pct
            report.battery_after_pct = remaining_after
            report.estimated_duration_s = estimate.duration_s

            report.checks.append(
                SafetyCheck(
                    "BATTERY",
                    CheckStatus.PASS
                    if percent >= safety.battery_min_for_delivery_pct
                    else CheckStatus.FAIL,
                    None
                    if percent >= safety.battery_min_for_delivery_pct
                    else (
                        f"Battery {percent:.0f}% is below the "
                        f"{safety.battery_min_for_delivery_pct:.0f}% dispatch minimum"
                    ),
                    observed=percent,
                )
            )

            # The energy model is only evidence if it was measured on a real
            # airframe. An unmeasured estimate cannot authorise a dispatch.
            if not estimate.calibrated:
                report.checks.append(
                    SafetyCheck(
                        "ENERGY_MODEL_CALIBRATION",
                        CheckStatus.WARN
                        if safety.allow_uncalibrated_delivery
                        else CheckStatus.FAIL,
                        (
                            "Energy model is UNCALIBRATED: the battery cost of a "
                            "delivery has never been measured on this airframe, so "
                            "the return-trip estimate is not evidence the aircraft "
                            "can get home. Run scripts/calibrate_energy.py."
                        ),
                        observed=self._energy.describe(),
                    )
                )

            can_return = estimate.can_return(percent)
            report.checks.append(
                SafetyCheck(
                    "RETURN_CAPABILITY",
                    CheckStatus.PASS
                    if can_return
                    else (
                        CheckStatus.UNKNOWN
                        if not estimate.calibrated
                        else CheckStatus.FAIL
                    ),
                    None
                    if can_return
                    else (
                        (
                            f"Estimate suggests about {estimate.total_pct:.0f}%, leaving "
                            f"{remaining_after:.0f}% against a required "
                            f"{estimate.reserve_pct:.0f}% reserve -- but the model is "
                            "uncalibrated, so this is not a measurement"
                        )
                        if not estimate.calibrated
                        else (
                            f"Round trip needs about {estimate.total_pct:.0f}%, leaving "
                            f"{remaining_after:.0f}% against a required "
                            f"{estimate.reserve_pct:.0f}% reserve"
                        )
                    ),
                    observed={
                        **estimate.as_dict(),
                        "remaining_after_pct": round(remaining_after, 1),
                        "battery_now_pct": percent,
                    },
                )
            )

        # -- mission time --------------------------------------------------------
        if report.estimated_duration_s is not None:
            report.checks.append(
                SafetyCheck(
                    "ESTIMATED_MISSION_TIME", CheckStatus.PASS,
                    observed=round(report.estimated_duration_s, 1),
                )
            )

        # -- geofence -------------------------------------------------------------
        evaluation = self._geofence.evaluate(mission_id, target_latitude, target_longitude)
        from app.core.enums import GeofenceStatus

        report.checks.append(
            SafetyCheck(
                "GEOFENCE",
                CheckStatus.FAIL
                if evaluation.status is GeofenceStatus.BREACHED
                else CheckStatus.PASS,
                (
                    f"Target is outside {', '.join(evaluation.breached_fences)}"
                    if evaluation.status is GeofenceStatus.BREACHED
                    else None
                ),
                observed=str(evaluation.status),
            )
        )

        # -- payload ----------------------------------------------------------------
        config = self._fleet.drone_config(state.drone_id)
        report.checks.append(
            SafetyCheck(
                "PAYLOAD",
                CheckStatus.PASS if config and config.payload_capacity_g else CheckStatus.WARN,
                None
                if config and config.payload_capacity_g
                else "No payload capacity configured for this aircraft",
                observed=config.payload_capacity_g if config else None,
            )
        )
        return report

    # ------------------------------------------------------------------
    # task lifecycle
    # ------------------------------------------------------------------
    async def create_task(
        self,
        session: AsyncSession,
        mission: Mission,
        survivor: Survivor,
        *,
        priority: int = 0,
        payload_type: str | None = None,
        payload_mass_g: int | None = None,
        operator_id: uuid.UUID | None = None,
    ) -> DeliveryTask:
        if survivor.state not in (
            SurvivorState.CONFIRMED,
            SurvivorState.PENDING_DELIVERY,
        ):
            raise ConflictError(
                f"Survivor {survivor.survivor_code} is {survivor.state}; "
                "only a confirmed survivor can receive a delivery",
                details={"survivor_state": str(survivor.state)},
            )

        open_task = await session.execute(
            select(DeliveryTask).where(
                DeliveryTask.survivor_id == survivor.id,
                DeliveryTask.state.in_(list(DELIVERY_ACTIVE_STATES)),
            )
        )
        if open_task.scalar_one_or_none() is not None:
            raise ConflictError(
                f"Survivor {survivor.survivor_code} already has an active delivery",
                details={"survivor_code": survivor.survivor_code},
            )

        shape = to_shape(survivor.location)
        count = await session.scalar(
            select(func.count(DeliveryTask.id)).where(DeliveryTask.mission_id == mission.id)
        )
        task = DeliveryTask(
            mission_id=mission.id,
            survivor_id=survivor.id,
            task_code=f"DLV-{(count or 0) + 1:03d}",
            state=DeliveryState.PENDING,
            state_changed_at=datetime.now(UTC),
            target_position=survivor.location,
            delivery_altitude_m=mission.delivery_altitude_m,
            payload_type=payload_type,
            payload_mass_g=payload_mass_g,
            priority=priority,
        )
        session.add(task)
        await session.flush()
        await self._record_event(
            session, task, "TASK_CREATED", None, DeliveryState.PENDING,
            message=f"Delivery {task.task_code} created for {survivor.survivor_code}",
            operator_id=operator_id, automatic=operator_id is None,
            evidence={"latitude": shape.y, "longitude": shape.x},
        )
        logger.info(
            "delivery_task_created",
            task_code=task.task_code,
            survivor_code=survivor.survivor_code,
        )
        return task

    async def assign(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        drone_id: str,
        principal: TokenPrincipal | None = None,
    ) -> tuple[DeliveryTask, DeliverySafetyReport]:
        """Assign an aircraft, but only if it is safe to dispatch."""
        state = self._fleet.state(drone_id)
        shape = to_shape(task.target_position)
        report = self.evaluate_safety(state, shape.y, shape.x, task.mission_id)

        task.safety_evaluation = report.as_dict()
        if not report.safe:
            await self._record_event(
                session, task, "DISPATCH_REJECTED", task.state, task.state,
                message=(
                    f"{drone_id} rejected for {task.task_code}: "
                    + "; ".join(f"{c.name} - {c.reason}" for c in report.failures)
                ),
                evidence=report.as_dict(),
                operator_id=principal.operator_id if principal else None,
            )
            self._bus.emit(
                EventType.DELIVERY_REJECTED,
                drone_id=drone_id,
                mission_id=str(task.mission_id),
                delivery_task_id=str(task.id),
                payload={
                    "task_code": task.task_code,
                    "reason": "DELIVERY_REJECTED_UNSAFE",
                    "report": report.as_dict(),
                },
            )
            logger.warning(
                "delivery_rejected_unsafe",
                task_code=task.task_code,
                drone_id=drone_id,
                failures=[c.name for c in report.failures],
            )
            raise DeliveryRejectedUnsafeError(
                f"{drone_id} is not safe to dispatch for {task.task_code}",
                details=report.as_dict(),
            )

        await self._transition(session, task, DeliveryState.ASSIGNED,
                               operator_id=principal.operator_id if principal else None)
        task.drone_uuid = self._fleet.require_drone_uuid(drone_id)
        task.assigned_at = datetime.now(UTC)
        task.estimated_duration_s = report.estimated_duration_s
        task.planned_distance_m = report.distance_m
        self._fleet.assign_delivery(drone_id, str(task.id))

        survivor = await session.get(Survivor, task.survivor_id)
        if survivor is not None and survivor.state is SurvivorState.PENDING_DELIVERY:
            await self._survivors.transition(
                session, survivor, SurvivorState.ASSIGNED,
                reason=f"Assigned to {drone_id} as {task.task_code}",
            )

        await session.flush()
        self._bus.emit(
            EventType.DELIVERY_ASSIGNED,
            drone_id=drone_id,
            mission_id=str(task.mission_id),
            delivery_task_id=str(task.id),
            survivor_id=str(task.survivor_id),
            payload={
                "task_code": task.task_code,
                "drone_id": drone_id,
                "survivor_code": survivor.survivor_code if survivor else None,
                "distance_m": report.distance_m,
                "estimated_duration_s": report.estimated_duration_s,
                "safety": report.as_dict(),
            },
        )
        logger.info("delivery_assigned", task_code=task.task_code, drone_id=drone_id)
        return task, report

    async def dispatch(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Send the aircraft to the survivor.

        Uses a single guided goto rather than an uploaded plan: it keeps the
        scout mission plans on the other aircraft untouched, and it is the
        operation PX4 handles most predictably for a point target.
        """
        if task.drone_uuid is None:
            raise ConflictError(f"Delivery {task.task_code} has no assigned aircraft")
        drone_id = self._fleet.drone_id_for_uuid(task.drone_uuid)
        if drone_id is None:
            raise NotFoundError("Assigned aircraft is not in the current fleet")

        state = self._fleet.state(drone_id)
        shape = to_shape(task.target_position)

        # Re-check safety at the moment of dispatch: conditions change between
        # assignment and launch.
        report = self.evaluate_safety(state, shape.y, shape.x, task.mission_id)
        task.safety_evaluation = report.as_dict()
        if not report.safe:
            raise DeliveryRejectedUnsafeError(
                f"{drone_id} is no longer safe to dispatch for {task.task_code}",
                details=report.as_dict(),
            )

        position = state.position.value
        altitude = task.delivery_altitude_m or (
            position.relative_altitude_m if position else None
        )
        if altitude is None or altitude <= 0:
            raise ConflictError(
                f"No delivery altitude configured for {task.task_code}",
                details={"task_code": task.task_code},
            )
        # goto_location takes an absolute (AMSL) altitude.
        base_amsl = (
            (position.absolute_altitude_m - (position.relative_altitude_m or 0.0))
            if position and position.absolute_altitude_m is not None
            else None
        )
        if base_amsl is None:
            raise ConflictError(
                f"{drone_id} is not reporting absolute altitude; "
                "a delivery altitude cannot be commanded safely",
                details={"drone_id": drone_id},
            )
        target_amsl = base_amsl + altitude

        await self._transition(session, task, DeliveryState.ROUTE_PLANNED,
                               operator_id=principal.operator_id)
        if position is not None:
            task.planned_route = from_shape(
                LineString(
                    [(position.longitude, position.latitude), (shape.x, shape.y)]
                ),
                srid=4326,
            )

        outcome = await self._commands.execute(
            drone_id=drone_id,
            command_type=CommandType.GOTO,
            principal=principal,
            mission_id=task.mission_id,
            parameters={
                "latitude": shape.y,
                "longitude": shape.x,
                "absolute_altitude_m": target_amsl,
                "yaw_deg": float("nan"),
            },
            request_id=request_id,
            idempotency_key=f"{task.id}:dispatch",
        )

        if not outcome.acknowledged:
            await self.fail(
                session, task,
                reason=f"{drone_id} did not acknowledge the delivery command",
            )
            return {"task_code": task.task_code, "dispatched": False,
                    "command": outcome.as_dict()}

        await self._transition(session, task, DeliveryState.EN_ROUTE,
                               operator_id=principal.operator_id,
                               evidence={"command": outcome.as_dict()})
        task.departed_at = datetime.now(UTC)

        survivor = await session.get(Survivor, task.survivor_id)
        if survivor is not None and survivor.state is SurvivorState.ASSIGNED:
            await self._survivors.transition(
                session, survivor, SurvivorState.DELIVERY_IN_PROGRESS,
                reason=f"{drone_id} en route",
            )

        self._start_monitor(task.id, drone_id, shape.y, shape.x)
        logger.info("delivery_dispatched", task_code=task.task_code, drone_id=drone_id,
                    target=[shape.y, shape.x])
        return {
            "task_code": task.task_code,
            "drone_id": drone_id,
            "dispatched": True,
            "target": {"latitude": shape.y, "longitude": shape.x,
                       "absolute_altitude_m": target_amsl},
            "command": outcome.as_dict(),
            "safety": report.as_dict(),
        }

    # ------------------------------------------------------------------
    # arrival monitoring
    # ------------------------------------------------------------------
    def _start_monitor(
        self, task_id: uuid.UUID, drone_id: str, latitude: float, longitude: float
    ) -> None:
        key = str(task_id)
        existing = self._monitors.get(key)
        if existing is not None and not existing.done():
            return
        self._monitors[key] = asyncio.create_task(
            self._monitor_arrival(task_id, drone_id, latitude, longitude),
            name=f"delivery-monitor-{key}",
        )

    async def _monitor_arrival(
        self, task_id: uuid.UUID, drone_id: str, latitude: float, longitude: float
    ) -> None:
        """Watch real position until the aircraft is actually over the target.

        Never advances the task on a timer. If the aircraft does not arrive,
        the task stays EN_ROUTE and the operator sees it.
        """
        from app.database.session import session_scope_optional

        state = self._fleet.state(drone_id)
        deadline = asyncio.get_running_loop().time() + max(
            600.0, (self._settings.safety.max_delivery_distance_m / 2) + 300
        )
        try:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(1.0)
                position = state.position.value
                if position is None:
                    continue
                distance = haversine_m(
                    position.latitude, position.longitude, latitude, longitude
                )
                if distance <= ARRIVAL_RADIUS_M:
                    async with session_scope_optional() as session:
                        if session is None:
                            return
                        task = await session.get(DeliveryTask, task_id)
                        if task is None or task.state is not DeliveryState.EN_ROUTE:
                            return
                        await self._transition(
                            session, task, DeliveryState.AT_TARGET,
                            evidence={
                                "observed_distance_m": round(distance, 1),
                                "arrival_radius_m": ARRIVAL_RADIUS_M,
                                "latitude": position.latitude,
                                "longitude": position.longitude,
                                "altitude_m": position.relative_altitude_m,
                            },
                        )
                        task.arrived_at = datetime.now(UTC)
                    logger.info(
                        "delivery_at_target",
                        task_id=str(task_id), drone_id=drone_id,
                        distance_m=round(distance, 1),
                    )
                    return
            logger.warning(
                "delivery_arrival_not_observed", task_id=str(task_id), drone_id=drone_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("delivery_monitor_failed", task_id=str(task_id),
                         error=str(exc), exc_info=True)
        finally:
            self._monitors.pop(str(task_id), None)

    # ------------------------------------------------------------------
    # release and confirmation
    # ------------------------------------------------------------------
    async def initiate_release(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        principal: TokenPrincipal,
        confirmation_timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        """Trigger the payload release and wait for physical confirmation.

        The command going out moves the task to DELIVERY_INITIATED. Only a
        provider signal moves it to DELIVERED.
        """
        if task.state is not DeliveryState.AT_TARGET:
            raise InvalidStateTransitionError(
                f"Delivery {task.task_code}", str(task.state),
                str(DeliveryState.DELIVERY_INITIATED),
            )

        await self._transition(session, task, DeliveryState.DELIVERY_INITIATED,
                               operator_id=principal.operator_id)
        await session.commit()

        confirmation = await self._confirmations.wait(task.id, confirmation_timeout_s)
        if confirmation is None or not confirmation.confirmed:
            logger.warning(
                "delivery_awaiting_confirmation",
                task_code=task.task_code,
                source=str(self._confirmations.primary_source),
            )
            return {
                "task_code": task.task_code,
                "state": str(task.state),
                "delivered": False,
                "detail": (
                    "Release commanded; awaiting physical confirmation from "
                    f"{self._confirmations.primary_source}. The delivery is not "
                    "recorded as complete until confirmation arrives."
                ),
            }

        await self.confirm_delivery(session, task, confirmation)
        return {
            "task_code": task.task_code,
            "state": str(task.state),
            "delivered": True,
            "confirmation": confirmation.as_dict(),
        }

    async def confirm_delivery(
        self, session: AsyncSession, task: DeliveryTask, confirmation: Confirmation
    ) -> DeliveryTask:
        """Record physical confirmation and close out the delivery."""
        if not confirmation.confirmed:
            return await self.fail(
                session, task, reason=f"Release not confirmed: {confirmation.detail}"
            )

        await self._transition(
            session, task, DeliveryState.DELIVERED,
            evidence=confirmation.as_dict(),
            operator_id=confirmation.operator_id,
        )
        task.delivered_at = confirmation.observed_at
        task.completed_at = datetime.now(UTC)
        task.confirmation_source = confirmation.source
        task.confirmation_detail = confirmation.detail[:255]
        task.confirmed_by = confirmation.operator_id

        survivor = await session.get(Survivor, task.survivor_id)
        drone_id = (
            self._fleet.drone_id_for_uuid(task.drone_uuid) if task.drone_uuid else None
        )
        if survivor is not None:
            await self._survivors.transition(
                session, survivor, SurvivorState.DELIVERED,
                reason=f"Confirmed via {confirmation.source}",
            )
        if drone_id:
            self._fleet.assign_delivery(drone_id, None)

        await session.flush()
        self._bus.emit(
            EventType.DELIVERY_CONFIRMED,
            drone_id=drone_id,
            mission_id=str(task.mission_id),
            delivery_task_id=str(task.id),
            survivor_id=str(task.survivor_id),
            payload={
                "task_code": task.task_code,
                "survivor_code": survivor.survivor_code if survivor else None,
                "confirmation_source": str(confirmation.source),
                "detail": confirmation.detail,
                "delivered_at": confirmation.observed_at.isoformat(),
            },
        )
        logger.info(
            "delivery_confirmed",
            task_code=task.task_code,
            source=str(confirmation.source),
            survivor_code=survivor.survivor_code if survivor else None,
        )
        return task

    async def submit_confirmation(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        source: DeliveryConfirmationSource,
        confirmed: bool,
        detail: str,
        operator_id: uuid.UUID | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> DeliveryTask:
        """Accept a confirmation signal arriving out of band."""
        confirmation = Confirmation(
            source=source,
            confirmed=confirmed,
            detail=detail,
            operator_id=operator_id,
            evidence=evidence or {},
        )
        # Release anyone waiting inside initiate_release.
        self._confirmations.submit(task.id, confirmation)
        if task.state is DeliveryState.DELIVERY_INITIATED:
            return await self.confirm_delivery(session, task, confirmation)
        return task

    # ------------------------------------------------------------------
    # termination
    # ------------------------------------------------------------------
    async def fail(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        reason: str,
        operator_id: uuid.UUID | None = None,
    ) -> DeliveryTask:
        await self._transition(session, task, DeliveryState.FAILED, operator_id=operator_id,
                               evidence={"reason": reason})
        task.failure_reason = reason[:255]
        task.completed_at = datetime.now(UTC)
        await self._release_aircraft_and_survivor(session, task, reason)
        logger.error("delivery_failed", task_code=task.task_code, reason=reason)
        return task

    async def cancel(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        reason: str,
        operator_id: uuid.UUID | None = None,
    ) -> DeliveryTask:
        await self._transition(session, task, DeliveryState.CANCELLED,
                               operator_id=operator_id, evidence={"reason": reason})
        task.cancellation_reason = reason[:255]
        task.completed_at = datetime.now(UTC)
        await self._release_aircraft_and_survivor(session, task, reason)
        logger.warning("delivery_cancelled", task_code=task.task_code, reason=reason)
        return task

    async def _release_aircraft_and_survivor(
        self, session: AsyncSession, task: DeliveryTask, reason: str
    ) -> None:
        monitor = self._monitors.pop(str(task.id), None)
        if monitor is not None and not monitor.done():
            monitor.cancel()

        if task.drone_uuid is not None:
            drone_id = self._fleet.drone_id_for_uuid(task.drone_uuid)
            if drone_id and self._fleet.connection_manager.has(drone_id):
                self._fleet.assign_delivery(drone_id, None)

        survivor = await session.get(Survivor, task.survivor_id)
        if survivor is not None and survivor.state in (
            SurvivorState.ASSIGNED,
            SurvivorState.DELIVERY_IN_PROGRESS,
        ):
            # Back to the queue so another aircraft can be dispatched.
            await self._survivors.transition(
                session, survivor, SurvivorState.PENDING_DELIVERY,
                reason=f"Delivery did not complete: {reason}",
            )

    # ------------------------------------------------------------------
    # state machine
    # ------------------------------------------------------------------
    async def _transition(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        to_state: DeliveryState,
        *,
        evidence: dict[str, Any] | None = None,
        operator_id: uuid.UUID | None = None,
    ) -> DeliveryTask:
        current = task.state
        if to_state is current:
            return task
        allowed = DELIVERY_TRANSITIONS.get(current, frozenset())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Delivery {task.task_code}", str(current), str(to_state)
            )
        task.state = to_state
        task.state_changed_at = datetime.now(UTC)
        await session.flush()

        await self._record_event(
            session, task, "STATE_CHANGED", current, to_state,
            message=f"Delivery {task.task_code}: {current} -> {to_state}",
            evidence=evidence or {}, operator_id=operator_id,
            automatic=operator_id is None,
        )
        drone_id = (
            self._fleet.drone_id_for_uuid(task.drone_uuid) if task.drone_uuid else None
        )
        self._bus.emit(
            EventType.DELIVERY_UPDATED,
            drone_id=drone_id,
            mission_id=str(task.mission_id),
            delivery_task_id=str(task.id),
            survivor_id=str(task.survivor_id),
            payload={
                "task_code": task.task_code,
                "from_state": str(current),
                "to_state": str(to_state),
                "drone_id": drone_id,
                "evidence": evidence or {},
            },
        )
        return task

    async def _record_event(
        self,
        session: AsyncSession,
        task: DeliveryTask,
        event_type: str,
        from_state: DeliveryState | None,
        to_state: DeliveryState | None,
        *,
        message: str,
        evidence: dict[str, Any] | None = None,
        operator_id: uuid.UUID | None = None,
        automatic: bool = True,
    ) -> None:
        session.add(
            DeliveryEvent(
                task_id=task.id,
                occurred_at=datetime.now(UTC),
                event_type=event_type,
                from_state=str(from_state) if from_state else None,
                to_state=str(to_state) if to_state else None,
                evidence=evidence or {},
                operator_id=operator_id,
                automatic=automatic,
                message=message[:255],
            )
        )
        await self._events.record(
            event_type=f"DELIVERY_{event_type}",
            message=message,
            severity=(
                AlertSeverity.WARNING
                if to_state in (DeliveryState.FAILED, DeliveryState.CANCELLED)
                else AlertSeverity.INFO
            ),
            mission_id=task.mission_id,
            drone_uuid=task.drone_uuid,
            survivor_id=task.survivor_id,
            delivery_task_id=task.id,
            operator_id=operator_id,
            data={"task_code": task.task_code, **(evidence or {})},
            session=session,
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def get(self, session: AsyncSession, task_id: uuid.UUID) -> DeliveryTask:
        task = await session.get(DeliveryTask, task_id)
        if task is None:
            raise NotFoundError(
                f"Delivery task {task_id} does not exist",
                details={"task_id": str(task_id)},
            )
        return task

    async def list_tasks(
        self,
        session: AsyncSession,
        mission_id: uuid.UUID | None = None,
        states: list[DeliveryState] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DeliveryTask]:
        stmt = select(DeliveryTask).order_by(DeliveryTask.created_at.desc())
        if mission_id is not None:
            stmt = stmt.where(DeliveryTask.mission_id == mission_id)
        if states:
            stmt = stmt.where(DeliveryTask.state.in_(states))
        result = await session.execute(stmt.limit(min(limit, 500)).offset(max(offset, 0)))
        return list(result.scalars().all())

    async def tasks_for_survivor(
        self, session: AsyncSession, survivor_id: uuid.UUID
    ) -> list[DeliveryTask]:
        """Every delivery attempt made for one survivor, oldest first.

        Includes failed and cancelled attempts: a survivor who took three
        sorties to reach is a fact worth keeping.
        """
        result = await session.execute(
            select(DeliveryTask)
            .where(DeliveryTask.survivor_id == survivor_id)
            .order_by(DeliveryTask.created_at)
        )
        return list(result.scalars().all())

    async def routes_geojson(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> dict[str, Any]:
        tasks = await self.list_tasks(session, mission_id, limit=500)
        features = []
        for task in tasks:
            if task.planned_route is None:
                continue
            shape = to_shape(task.planned_route)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(c) for c in shape.coords],
                    },
                    "properties": {
                        "id": str(task.id),
                        "task_code": task.task_code,
                        "state": str(task.state),
                        "drone_id": (
                            self._fleet.drone_id_for_uuid(task.drone_uuid)
                            if task.drone_uuid
                            else None
                        ),
                        "planned_distance_m": task.planned_distance_m,
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    async def shutdown(self) -> None:
        for task in list(self._monitors.values()):
            task.cancel()
        self._monitors.clear()
