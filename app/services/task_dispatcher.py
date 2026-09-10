"""TaskDispatcher -- deciding which aircraft goes to which survivor.

Reacts to a survivor being confirmed, picks a delivery-capable aircraft that
can safely make the trip, and creates and dispatches the task.

Written for more than one delivery aircraft from the start: nothing here names
D3. Candidates come from the fleet by role, and the choice among them is made
on live telemetry -- closest first, then most battery margin.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import to_shape
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import MISSION_ACTIVE_STATES, DeliveryState, SurvivorState
from app.core.exceptions import (
    DeliveryRejectedUnsafeError,
    NoDeliveryDroneAvailableError,
)
from app.core.geo import haversine_m
from app.core.logging import get_logger
from app.core.security import TokenPrincipal
from app.database.session import session_scope_optional
from app.models.mission import Mission
from app.models.survivor import Survivor
from app.realtime.event_bus import Event, EventBus, EventType
from app.services.delivery_manager import DeliveryManager, DeliverySafetyReport
from app.services.fleet_manager import FleetManager
from app.services.survivor_manager import SurvivorManager

logger = get_logger(__name__)


@dataclass(slots=True)
class Candidate:
    drone_id: str
    distance_m: float
    battery_percent: float | None
    report: DeliverySafetyReport

    @property
    def sort_key(self) -> tuple[float, float]:
        # Closest first; among comparable distances, the aircraft with more
        # battery margin left after the trip.
        margin = self.report.battery_after_pct
        return (self.distance_m, -(margin if margin is not None else -1e9))


@dataclass(slots=True)
class DispatchDecision:
    survivor_code: str
    dispatched: bool
    drone_id: str | None = None
    task_code: str | None = None
    reason: str | None = None
    candidates: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "survivor_code": self.survivor_code,
            "dispatched": self.dispatched,
            "drone_id": self.drone_id,
            "task_code": self.task_code,
            "reason": self.reason,
            "candidates": self.candidates,
        }


class TaskDispatcher:
    def __init__(
        self,
        fleet: FleetManager,
        deliveries: DeliveryManager,
        survivors: SurvivorManager,
        bus: EventBus,
        settings: Settings,
        system_principal: TokenPrincipal | None = None,
    ) -> None:
        self._fleet = fleet
        self._deliveries = deliveries
        self._survivors = survivors
        self._bus = bus
        self._settings = settings
        #: Identity used for automatic dispatch, so the audit log distinguishes
        #: a machine decision from an operator one.
        self._system_principal = system_principal
        self._auto_dispatch = False
        self._lock = asyncio.Lock()
        self._decisions: list[DispatchDecision] = []

    def set_system_principal(self, principal: TokenPrincipal) -> None:
        self._system_principal = principal

    def enable_auto_dispatch(self, enabled: bool) -> None:
        """Turn automatic dispatch on or off.

        Off by default: sending an aircraft is an action an operator should
        opt into for a given mission.
        """
        self._auto_dispatch = enabled
        logger.info("auto_dispatch_configured", enabled=enabled)

    @property
    def auto_dispatch_enabled(self) -> bool:
        return self._auto_dispatch

    def register(self) -> None:
        self._bus.on(EventType.SURVIVOR_CONFIRMED, self._on_survivor_confirmed)

    async def _on_survivor_confirmed(self, event: Event) -> None:
        if not self._auto_dispatch or self._system_principal is None:
            return
        if event.survivor_id is None or event.mission_id is None:
            return
        async with session_scope_optional() as session:
            if session is None:
                logger.error("auto_dispatch_skipped_no_database",
                             survivor_id=event.survivor_id)
                return
            survivor = await session.get(Survivor, uuid.UUID(event.survivor_id))
            mission = await session.get(Mission, uuid.UUID(event.mission_id))
            if survivor is None or mission is None:
                return
            if mission.state not in MISSION_ACTIVE_STATES:
                return
            try:
                await self.dispatch_for_survivor(
                    session, mission, survivor, self._system_principal
                )
            except Exception as exc:
                logger.error(
                    "auto_dispatch_failed",
                    survivor_code=survivor.survivor_code,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    async def dispatch_for_survivor(
        self,
        session: AsyncSession,
        mission: Mission,
        survivor: Survivor,
        principal: TokenPrincipal,
        request_id: str | None = None,
        preferred_drone_id: str | None = None,
    ) -> DispatchDecision:
        """Create and launch a delivery for one survivor.

        Serialised across the fleet so two survivors confirmed a moment apart
        cannot both be assigned the same aircraft.
        """
        async with self._lock:
            shape = to_shape(survivor.location)
            candidates = self._rank_candidates(
                mission.id, shape.y, shape.x, preferred_drone_id
            )
            decision = DispatchDecision(
                survivor_code=survivor.survivor_code,
                dispatched=False,
                candidates=[
                    {
                        "drone_id": c.drone_id,
                        "distance_m": round(c.distance_m, 1),
                        "battery_percent": c.battery_percent,
                        "safe": c.report.safe,
                        "blocking": [f.name for f in c.report.failures],
                    }
                    for c in candidates
                ],
            )

            viable = [c for c in candidates if c.report.safe]
            if not viable:
                decision.reason = (
                    "No delivery aircraft is currently safe to dispatch"
                    if candidates
                    else "No delivery-capable aircraft is connected and free"
                )
                self._decisions.append(decision)
                logger.warning(
                    "no_delivery_drone_available",
                    survivor_code=survivor.survivor_code,
                    candidates=decision.candidates,
                )
                self._bus.emit(
                    EventType.DELIVERY_REJECTED,
                    mission_id=str(mission.id),
                    survivor_id=str(survivor.id),
                    payload={
                        "survivor_code": survivor.survivor_code,
                        "reason": decision.reason,
                        "candidates": decision.candidates,
                    },
                )
                if not candidates:
                    raise NoDeliveryDroneAvailableError(
                        decision.reason, details=decision.as_dict()
                    )
                raise DeliveryRejectedUnsafeError(
                    decision.reason, details=decision.as_dict()
                )

            chosen = viable[0]

            if survivor.state is SurvivorState.CONFIRMED:
                await self._survivors.transition(
                    session, survivor, SurvivorState.PENDING_DELIVERY,
                    reason="Queued for delivery dispatch",
                )

            task = await self._deliveries.create_task(
                session, mission, survivor,
                priority=survivor.priority,
                operator_id=principal.operator_id,
            )
            await self._deliveries.assign(session, task, chosen.drone_id, principal)
            result = await self._deliveries.dispatch(
                session, task, principal, request_id=request_id
            )

            decision.dispatched = bool(result.get("dispatched"))
            decision.drone_id = chosen.drone_id
            decision.task_code = task.task_code
            if not decision.dispatched:
                decision.reason = "Aircraft did not acknowledge the delivery command"
            self._decisions.append(decision)
            logger.info(
                "dispatch_decision",
                survivor_code=survivor.survivor_code,
                drone_id=chosen.drone_id,
                task_code=task.task_code,
                dispatched=decision.dispatched,
            )
            return decision

    def _rank_candidates(
        self,
        mission_id: uuid.UUID,
        latitude: float,
        longitude: float,
        preferred_drone_id: str | None = None,
    ) -> list[Candidate]:
        """Evaluate every free delivery aircraft against this target."""
        candidates: list[Candidate] = []
        pool = self._fleet.available_delivery_drones()
        if preferred_drone_id:
            pool = [s for s in pool if s.drone_id == preferred_drone_id.upper()] or pool

        for state in pool:
            position = state.position.value
            if position is None:
                continue
            distance = haversine_m(
                position.latitude, position.longitude, latitude, longitude
            )
            report = self._deliveries.evaluate_safety(
                state, latitude, longitude, mission_id
            )
            candidates.append(
                Candidate(
                    drone_id=state.drone_id,
                    distance_m=distance,
                    battery_percent=state.battery_percent(),
                    report=report,
                )
            )
        return sorted(candidates, key=lambda c: c.sort_key)

    # ------------------------------------------------------------------
    # queue processing
    # ------------------------------------------------------------------
    async def process_pending(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal,
        limit: int = 5,
    ) -> list[DispatchDecision]:
        """Work through the survivors waiting for a delivery.

        Stops as soon as no aircraft is available, rather than churning through
        the queue producing identical rejections.
        """
        pending = await self._survivors.pending_delivery(session, mission.id)
        decisions: list[DispatchDecision] = []
        for survivor in pending[:limit]:
            try:
                decisions.append(
                    await self.dispatch_for_survivor(session, mission, survivor, principal)
                )
            except NoDeliveryDroneAvailableError as exc:
                decisions.append(
                    DispatchDecision(
                        survivor_code=survivor.survivor_code,
                        dispatched=False,
                        reason=exc.message,
                    )
                )
                break
            except DeliveryRejectedUnsafeError as exc:
                decisions.append(
                    DispatchDecision(
                        survivor_code=survivor.survivor_code,
                        dispatched=False,
                        reason=exc.message,
                    )
                )
                break
        return decisions

    def recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self._decisions[-limit:]]

    def status(self) -> dict[str, Any]:
        delivery_states = self._fleet.delivery_drones()
        return {
            "auto_dispatch_enabled": self._auto_dispatch,
            "delivery_aircraft": [
                {
                    "drone_id": s.drone_id,
                    "connection_state": str(s.connection_state),
                    "battery_percent": s.battery_percent(),
                    "current_task": s.delivery_task_id,
                }
                for s in delivery_states
            ],
            "available": [s.drone_id for s in self._fleet.available_delivery_drones()],
            "generated_at": datetime.now(UTC).isoformat(),
        }


def active_delivery_states() -> list[DeliveryState]:
    from app.core.enums import DELIVERY_ACTIVE_STATES

    return list(DELIVERY_ACTIVE_STATES)
