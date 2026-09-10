"""SafetyEngine -- independent supervision of the real fleet.

Runs on its own timer, reads only live telemetry, and raises alerts. It is
deliberately *advisory*:

    PX4 owns the aircraft. This engine owns the operator's attention.

It never commands an aircraft. Battery failsafe, RTL, geofence enforcement and
flight termination are PX4 responsibilities and stay there -- a GCS that tries
to substitute for them adds a second, slower, link-dependent safety authority,
which is worse than none. What this engine does is notice, record and tell the
operator, including when the thing it noticed is that telemetry stopped.

Every threshold comes from configuration; nothing is hard-coded.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.core.enums import (
    AlertCategory,
    AlertSeverity,
    ConnectionState,
    GeofenceStatus,
    TelemetryStatus,
)
from app.core.logging import get_logger
from app.database.session import session_scope_optional
from app.drone.state import DroneState
from app.models.event import Alert
from app.realtime.event_bus import EventBus, EventType
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Condition:
    """One detected safety condition."""

    dedupe_key: str
    category: AlertCategory
    severity: AlertSeverity
    code: str
    message: str
    drone_id: str | None = None
    mission_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveAlert:
    condition: Condition
    alert_id: uuid.UUID | None
    raised_at: datetime
    last_seen_at: datetime


class SafetyEngine:
    """Periodic evaluation of every live drone state."""

    def __init__(
        self,
        fleet: FleetManager,
        geofence: GeofenceService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._geofence = geofence
        self._bus = bus
        self._settings = settings
        self._policy = fleet.policy
        self._active: dict[str, ActiveAlert] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._evaluations = 0
        self._last_evaluation: datetime | None = None
        #: Mission deadline supervision is fed by the mission manager.
        self._mission_deadlines: dict[str, tuple[datetime, int]] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="safety-engine")
        logger.info("safety_engine_started", interval_s=self._settings.safety_engine_interval_s)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("safety_engine_stopped", evaluations=self._evaluations)

    async def _loop(self) -> None:
        interval = self._settings.safety_engine_interval_s
        while self._running:
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("safety_engine_cycle_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # mission deadline registration
    # ------------------------------------------------------------------
    def track_mission(self, mission_id: str, started_at: datetime, max_duration_s: int) -> None:
        self._mission_deadlines[mission_id] = (started_at, max_duration_s)

    def untrack_mission(self, mission_id: str) -> None:
        self._mission_deadlines.pop(mission_id, None)

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    async def evaluate_once(self) -> list[Condition]:
        """One full pass over the fleet. Returns the conditions found."""
        conditions: list[Condition] = []
        for state in self._fleet.states.values():
            conditions.extend(self._evaluate_drone(state))
        conditions.extend(self._evaluate_missions())

        self._evaluations += 1
        self._last_evaluation = datetime.now(UTC)
        await self._reconcile(conditions)
        return conditions

    def _evaluate_drone(self, state: DroneState) -> list[Condition]:
        found: list[Condition] = []
        found.extend(self._check_connection(state))

        # Everything below needs live telemetry. When the link is down the
        # connection alert is the whole story -- inventing battery or GPS
        # alerts from values we are no longer receiving would be noise.
        if not state.is_connected:
            return found

        found.extend(self._check_telemetry_freshness(state))
        found.extend(self._check_battery(state))
        found.extend(self._check_gps(state))
        found.extend(self._check_health(state))
        found.extend(self._check_geofence(state))
        found.extend(self._check_flight_mode(state))
        return found

    # -- individual checks -------------------------------------------------
    def _check_connection(self, state: DroneState) -> list[Condition]:
        key = f"connection:{state.drone_id}"
        age = state.contact_age_s()
        evidence = {
            "connection_state": str(state.connection_state),
            "last_contact_age_s": round(age, 2) if age is not None else None,
            "last_seen": state.last_contact_at.isoformat() if state.last_contact_at else None,
            "last_known_position": (
                {
                    "latitude": state.last_known_position.latitude,
                    "longitude": state.last_known_position.longitude,
                    "at": state.last_known_position.at.isoformat(),
                }
                if state.last_known_position
                else None
            ),
            "reconnect_attempts": state.reconnect_attempts,
        }

        match state.connection_state:
            case ConnectionState.DISCONNECTED | ConnectionState.ERROR:
                return [
                    Condition(
                        dedupe_key=key,
                        category=AlertCategory.CONNECTION,
                        severity=AlertSeverity.CRITICAL,
                        code="LINK_LOST",
                        message=(
                            f"{state.drone_id} communication lost"
                            + (f": {state.last_error}" if state.last_error else "")
                        ),
                        drone_id=state.drone_id,
                        mission_id=state.mission_id,
                        evidence=evidence,
                    )
                ]
            case ConnectionState.DEGRADED:
                return [
                    Condition(
                        dedupe_key=key,
                        category=AlertCategory.CONNECTION,
                        severity=AlertSeverity.WARNING,
                        code="LINK_DEGRADED",
                        message=f"{state.drone_id} telemetry has gone quiet",
                        drone_id=state.drone_id,
                        mission_id=state.mission_id,
                        evidence=evidence,
                    )
                ]
            case _:
                return []

    def _check_telemetry_freshness(self, state: DroneState) -> list[Condition]:
        stale: list[str] = []
        missing: list[str] = []
        for stream, holder in (
            ("position", state.position),
            ("battery", state.battery),
            ("gps", state.gps),
            ("flight_mode", state.flight_mode),
        ):
            status = self._policy.status(stream, holder)
            if status is TelemetryStatus.STALE:
                stale.append(stream)
            elif status is TelemetryStatus.NO_DATA:
                missing.append(stream)

        if not stale and not missing:
            return []
        return [
            Condition(
                dedupe_key=f"freshness:{state.drone_id}",
                category=AlertCategory.TELEMETRY_FRESHNESS,
                severity=AlertSeverity.CRITICAL if missing else AlertSeverity.WARNING,
                code="TELEMETRY_STALE",
                message=(
                    f"{state.drone_id} telemetry incomplete "
                    f"(stale: {', '.join(stale) or 'none'}; "
                    f"missing: {', '.join(missing) or 'none'})"
                ),
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                evidence={"stale": stale, "missing": missing},
            )
        ]

    def _check_battery(self, state: DroneState) -> list[Condition]:
        thresholds = self._settings.safety
        battery = state.battery.value
        status = self._policy.status("battery", state.battery)
        key = f"battery:{state.drone_id}"

        if battery is None or status is TelemetryStatus.NO_DATA:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.BATTERY,
                    severity=AlertSeverity.WARNING,
                    code="BATTERY_NO_DATA",
                    message=f"{state.drone_id} is not reporting battery state",
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                    evidence={"status": str(status)},
                )
            ]
        percent = battery.remaining_percent
        if percent is None:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.BATTERY,
                    severity=AlertSeverity.WARNING,
                    code="BATTERY_NO_PERCENT",
                    message=f"{state.drone_id} reports voltage but no battery percentage",
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                    evidence={"voltage_v": battery.voltage_v},
                )
            ]

        evidence = {
            "remaining_percent": percent,
            "voltage_v": battery.voltage_v,
            "current_a": battery.current_a,
            "freshness": str(status),
        }
        if percent <= thresholds.battery_emergency_pct:
            severity, code = AlertSeverity.EMERGENCY, "BATTERY_EMERGENCY"
        elif percent <= thresholds.battery_critical_pct:
            severity, code = AlertSeverity.CRITICAL, "BATTERY_CRITICAL"
        elif percent <= thresholds.battery_warning_pct:
            severity, code = AlertSeverity.WARNING, "BATTERY_LOW"
        else:
            return []

        return [
            Condition(
                dedupe_key=key,
                category=AlertCategory.BATTERY,
                severity=severity,
                code=code,
                message=f"{state.drone_id} battery at {percent:.0f}%",
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                evidence=evidence,
            )
        ]

    def _check_gps(self, state: DroneState) -> list[Condition]:
        gps = state.gps.value
        key = f"gps:{state.drone_id}"
        if gps is None:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.GPS,
                    severity=AlertSeverity.WARNING,
                    code="GPS_NO_DATA",
                    message=f"{state.drone_id} is not reporting GPS state",
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                )
            ]
        thresholds = self._settings.safety
        evidence = {
            "fix_type": gps.fix_type,
            "fix_type_name": gps.fix_type_name,
            "satellites": gps.satellites,
        }
        if gps.fix_type < thresholds.min_gps_fix_type:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.GPS,
                    severity=AlertSeverity.CRITICAL,
                    code="GPS_FIX_LOST",
                    message=f"{state.drone_id} GPS fix degraded to {gps.fix_type_name}",
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                    evidence=evidence,
                )
            ]
        if gps.satellites < thresholds.min_satellites:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.GPS,
                    severity=AlertSeverity.WARNING,
                    code="GPS_LOW_SATELLITES",
                    message=(
                        f"{state.drone_id} tracking {gps.satellites} satellites "
                        f"(minimum {thresholds.min_satellites})"
                    ),
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                    evidence=evidence,
                )
            ]
        return []

    def _check_health(self, state: DroneState) -> list[Condition]:
        health = state.health.value
        if health is None or health.all_ok:
            return []
        # ``all_ok`` is derived from the others, so including it would both
        # duplicate the report and defeat the suppression below.
        failing = [
            k for k, v in health.as_dict().items() if v is False and k != "all_ok"
        ]
        # An unarmed aircraft on the ground legitimately reports not-armable;
        # alerting on that would train operators to ignore health alerts.
        if failing == ["armable"] and state.armed.value is not True:
            return []
        return [
            Condition(
                dedupe_key=f"health:{state.drone_id}",
                category=AlertCategory.HEALTH,
                severity=AlertSeverity.WARNING,
                code="VEHICLE_HEALTH_DEGRADED",
                message=f"{state.drone_id} health checks failing: {', '.join(failing)}",
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                evidence=health.as_dict(),
            )
        ]

    def _check_geofence(self, state: DroneState) -> list[Condition]:
        evaluation = self._geofence.evaluate_state(state)
        if evaluation is None or evaluation.status in (
            GeofenceStatus.INSIDE,
            GeofenceStatus.UNKNOWN,
        ):
            return []
        key = f"geofence:{state.drone_id}"
        if evaluation.status is GeofenceStatus.BREACHED:
            return [
                Condition(
                    dedupe_key=key,
                    category=AlertCategory.GEOFENCE,
                    severity=AlertSeverity.CRITICAL,
                    code="GEOFENCE_BREACH",
                    message=(
                        f"{state.drone_id} is outside the mission geofence "
                        f"({', '.join(evaluation.breached_fences)})"
                    ),
                    drone_id=state.drone_id,
                    mission_id=state.mission_id,
                    evidence={
                        "breached_fences": evaluation.breached_fences,
                        **evaluation.detail,
                    },
                )
            ]
        return [
            Condition(
                dedupe_key=key,
                category=AlertCategory.GEOFENCE,
                severity=AlertSeverity.WARNING,
                code="GEOFENCE_NEAR_BOUNDARY",
                message=(
                    f"{state.drone_id} is {evaluation.margin_m:.0f} m from the geofence boundary"
                    if evaluation.margin_m is not None
                    else f"{state.drone_id} is approaching the geofence boundary"
                ),
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                evidence={"margin_m": evaluation.margin_m, **evaluation.detail},
            )
        ]

    def _check_flight_mode(self, state: DroneState) -> list[Condition]:
        """Surface autopilot-initiated mode changes.

        If PX4 puts an aircraft into RTL or LAND on its own, that is a failsafe
        firing. The operator needs to know immediately, and the GCS must not
        interfere with it.
        """
        mode = state.flight_mode.value
        if mode not in ("RETURN_TO_LAUNCH", "LAND"):
            return []
        if state.delivery_task_id or state.busy_with_command in ("RTL", "LAND"):
            return []  # commanded by us; already tracked as a command
        return [
            Condition(
                dedupe_key=f"flight_mode:{state.drone_id}",
                category=AlertCategory.FLIGHT_MODE,
                severity=AlertSeverity.CRITICAL,
                code="AUTOPILOT_FAILSAFE_MODE",
                message=(
                    f"{state.drone_id} entered {mode} without a GCS command "
                    f"(likely a PX4 failsafe)"
                ),
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                evidence={"flight_mode": mode, "armed": state.armed.value},
            )
        ]

    def _evaluate_missions(self) -> list[Condition]:
        conditions: list[Condition] = []
        now = datetime.now(UTC)
        warn_at = self._settings.safety.mission_warning_remaining_s
        for mission_id, (started_at, max_duration_s) in self._mission_deadlines.items():
            elapsed = (now - started_at).total_seconds()
            remaining = max_duration_s - elapsed
            evidence = {
                "elapsed_s": round(elapsed, 1),
                "remaining_s": round(remaining, 1),
                "max_duration_s": max_duration_s,
            }
            if remaining <= 0:
                conditions.append(
                    Condition(
                        dedupe_key=f"mission_timeout:{mission_id}",
                        category=AlertCategory.MISSION_TIMEOUT,
                        severity=AlertSeverity.CRITICAL,
                        code="MISSION_TIME_EXPIRED",
                        message=(
                            f"Mission exceeded its {max_duration_s}s limit by "
                            f"{abs(remaining):.0f}s"
                        ),
                        mission_id=mission_id,
                        evidence=evidence,
                    )
                )
            elif remaining <= warn_at:
                conditions.append(
                    Condition(
                        dedupe_key=f"mission_timeout:{mission_id}",
                        category=AlertCategory.MISSION_TIMEOUT,
                        severity=AlertSeverity.WARNING,
                        code="MISSION_TIME_LOW",
                        message=f"{remaining:.0f}s of mission time remaining",
                        mission_id=mission_id,
                        evidence=evidence,
                    )
                )
        return conditions

    # ------------------------------------------------------------------
    # alert reconciliation
    # ------------------------------------------------------------------
    async def _reconcile(self, conditions: list[Condition]) -> None:
        """Raise new alerts, refresh standing ones, clear resolved ones.

        Deduplication by key means a drone sitting at 24% battery produces one
        standing CRITICAL alert, not one per second. A change of severity on
        the same key supersedes the old alert.
        """
        now = datetime.now(UTC)
        current = {c.dedupe_key: c for c in conditions}

        for key, condition in current.items():
            existing = self._active.get(key)
            if existing is None:
                await self._raise(condition, now)
            elif existing.condition.severity is not condition.severity or (
                existing.condition.code != condition.code
            ):
                await self._clear(key, now, reason="superseded")
                await self._raise(condition, now)
            else:
                existing.last_seen_at = now

        for key in [k for k in self._active if k not in current]:
            await self._clear(key, now, reason="condition resolved")

    async def _raise(self, condition: Condition, now: datetime) -> None:
        alert_id: uuid.UUID | None = None
        drone_uuid = (
            self._fleet.drone_uuid(condition.drone_id) if condition.drone_id else None
        )
        async with session_scope_optional() as session:
            if session is not None:
                row = Alert(
                    mission_id=(
                        uuid.UUID(condition.mission_id) if condition.mission_id else None
                    ),
                    drone_uuid=drone_uuid,
                    category=condition.category,
                    severity=condition.severity,
                    code=condition.code,
                    message=condition.message[:512],
                    dedupe_key=condition.dedupe_key,
                    raised_at=now,
                    active=True,
                    evidence=condition.evidence,
                )
                session.add(row)
                await session.flush()
                alert_id = row.id

        self._active[condition.dedupe_key] = ActiveAlert(
            condition=condition, alert_id=alert_id, raised_at=now, last_seen_at=now
        )
        logger.warning(
            "alert_raised",
            code=condition.code,
            severity=str(condition.severity),
            drone_id=condition.drone_id,
            message=condition.message,
        )
        self._bus.emit(
            EventType.ALERT_CREATED,
            drone_id=condition.drone_id,
            mission_id=condition.mission_id,
            payload={
                "alert_id": str(alert_id) if alert_id else None,
                "category": str(condition.category),
                "severity": str(condition.severity),
                "code": condition.code,
                "message": condition.message,
                "evidence": condition.evidence,
                "raised_at": now.isoformat(),
            },
        )
        # Dedicated events the dashboard listens for directly.
        if condition.category is AlertCategory.BATTERY:
            self._bus.emit(
                EventType.BATTERY_WARNING,
                drone_id=condition.drone_id,
                payload={"severity": str(condition.severity), **condition.evidence},
            )
        elif condition.category is AlertCategory.GPS:
            self._bus.emit(
                EventType.GPS_WARNING,
                drone_id=condition.drone_id,
                payload={"severity": str(condition.severity), **condition.evidence},
            )
        elif condition.category is AlertCategory.GEOFENCE:
            self._bus.emit(
                EventType.GEOFENCE_WARNING,
                drone_id=condition.drone_id,
                payload={"severity": str(condition.severity), **condition.evidence},
            )

    async def _clear(self, key: str, now: datetime, reason: str) -> None:
        active = self._active.pop(key, None)
        if active is None:
            return
        if active.alert_id is not None:
            async with session_scope_optional() as session:
                if session is not None:
                    row = await session.get(Alert, active.alert_id)
                    if row is not None:
                        row.active = False
                        row.cleared_at = now
        logger.info("alert_cleared", code=active.condition.code, reason=reason,
                    drone_id=active.condition.drone_id)
        self._bus.emit(
            EventType.ALERT_CLEARED,
            drone_id=active.condition.drone_id,
            mission_id=active.condition.mission_id,
            payload={
                "alert_id": str(active.alert_id) if active.alert_id else None,
                "code": active.condition.code,
                "reason": reason,
                "cleared_at": now.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def active_alerts(self) -> list[dict[str, Any]]:
        return [
            {
                "alert_id": str(a.alert_id) if a.alert_id else None,
                "code": a.condition.code,
                "category": str(a.condition.category),
                "severity": str(a.condition.severity),
                "message": a.condition.message,
                "drone_id": a.condition.drone_id,
                "mission_id": a.condition.mission_id,
                "evidence": a.condition.evidence,
                "raised_at": a.raised_at.isoformat(),
                "last_seen_at": a.last_seen_at.isoformat(),
            }
            for a in sorted(
                self._active.values(),
                key=lambda x: (_SEVERITY_RANK[x.condition.severity], x.raised_at),
                reverse=True,
            )
        ]

    def highest_severity(self) -> AlertSeverity | None:
        if not self._active:
            return None
        return max(
            (a.condition.severity for a in self._active.values()),
            key=lambda s: _SEVERITY_RANK[s],
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "evaluations": self._evaluations,
            "last_evaluation": (
                self._last_evaluation.isoformat() if self._last_evaluation else None
            ),
            "active_alerts": len(self._active),
            "highest_severity": (
                str(sev) if (sev := self.highest_severity()) is not None else None
            ),
            "tracked_missions": len(self._mission_deadlines),
        }

    async def acknowledge(self, alert_id: uuid.UUID, operator_id: uuid.UUID) -> bool:
        async with session_scope_optional() as session:
            if session is None:
                return False
            row = await session.get(Alert, alert_id)
            if row is None:
                return False
            row.acknowledged_at = datetime.now(UTC)
            row.acknowledged_by = operator_id
            return True

    async def load_active_from_db(self) -> None:
        """Restore standing alerts after a backend restart."""
        async with session_scope_optional() as session:
            if session is None:
                return
            result = await session.execute(select(Alert).where(Alert.active.is_(True)))
            for row in result.scalars().all():
                self._active[row.dedupe_key] = ActiveAlert(
                    condition=Condition(
                        dedupe_key=row.dedupe_key,
                        category=row.category,
                        severity=row.severity,
                        code=row.code,
                        message=row.message,
                        drone_id=(
                            self._fleet.drone_id_for_uuid(row.drone_uuid)
                            if row.drone_uuid
                            else None
                        ),
                        mission_id=str(row.mission_id) if row.mission_id else None,
                        evidence=row.evidence,
                    ),
                    alert_id=row.id,
                    raised_at=row.raised_at,
                    last_seen_at=row.raised_at,
                )


_SEVERITY_RANK = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
    AlertSeverity.EMERGENCY: 3,
}
