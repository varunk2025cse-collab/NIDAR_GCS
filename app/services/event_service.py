"""Mission timeline and audit persistence.

Two append-only records, written from the event bus and from the command path:

* ``mission_events`` -- the operator-visible timeline (the "Recent Events"
  panel, and the post-mission report).
* ``audit_logs`` -- every control action, including the ones that were
  refused, with who asked and what the aircraft answered.

Both writers are best-effort with respect to the database: a database outage
degrades recording, it never blocks flight supervision. Anything that fails to
persist is still written to the structured log.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AlertSeverity
from app.core.logging import get_logger
from app.database.session import session_scope_optional
from app.models.event import AuditLog, MissionEvent
from app.realtime.event_bus import Event, EventBus, EventType

logger = get_logger(__name__)


class EventService:
    """Writes the mission timeline and the audit log."""

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    # ------------------------------------------------------------------
    # mission timeline
    # ------------------------------------------------------------------
    async def record(
        self,
        *,
        event_type: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        mission_id: uuid.UUID | None = None,
        drone_uuid: uuid.UUID | None = None,
        survivor_id: uuid.UUID | None = None,
        delivery_task_id: uuid.UUID | None = None,
        operator_id: uuid.UUID | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> MissionEvent | None:
        """Append one timeline entry.

        Pass ``session`` to join an existing transaction (so the event and the
        state change it describes commit together); omit it for fire-and-forget
        recording from a background loop.
        """
        row = MissionEvent(
            mission_id=mission_id,
            drone_uuid=drone_uuid,
            survivor_id=survivor_id,
            delivery_task_id=delivery_task_id,
            operator_id=operator_id,
            occurred_at=occurred_at or datetime.now(UTC),
            event_type=event_type,
            severity=severity,
            message=message[:512],
            data=data or {},
        )
        if session is not None:
            session.add(row)
            await session.flush()
            return row

        async with session_scope_optional() as scoped:
            if scoped is None:
                logger.warning(
                    "mission_event_not_persisted", event_type=event_type, message=message
                )
                return None
            scoped.add(row)
            await scoped.flush()
            return row

    async def timeline(
        self,
        session: AsyncSession,
        mission_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        event_types: list[str] | None = None,
        min_severity: AlertSeverity | None = None,
    ) -> list[MissionEvent]:
        stmt = select(MissionEvent).order_by(desc(MissionEvent.occurred_at))
        if mission_id is not None:
            stmt = stmt.where(MissionEvent.mission_id == mission_id)
        if event_types:
            stmt = stmt.where(MissionEvent.event_type.in_(event_types))
        if min_severity is not None:
            ranked = _severity_at_least(min_severity)
            stmt = stmt.where(MissionEvent.severity.in_(ranked))
        stmt = stmt.limit(min(limit, 1000)).offset(max(offset, 0))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    async def audit(
        self,
        *,
        action: str,
        result: str,
        operator_id: uuid.UUID | None = None,
        operator_username: str | None = None,
        operator_role: str | None = None,
        mission_id: uuid.UUID | None = None,
        drone_uuid: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        acknowledgement: str | None = None,
        failure_reason: str | None = None,
        detail: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> AuditLog | None:
        """Record a control action.

        Called for every attempt -- authorised, refused, failed or timed out --
        so the log answers "what did the GCS ask the aircraft to do, and what
        happened" without gaps.
        """
        row = AuditLog(
            occurred_at=datetime.now(UTC),
            operator_id=operator_id,
            operator_username=operator_username,
            operator_role=operator_role,
            mission_id=mission_id,
            drone_uuid=drone_uuid,
            command_id=command_id,
            action=action,
            request_id=request_id,
            source_ip=source_ip,
            result=result,
            acknowledgement=acknowledgement[:255] if acknowledgement else None,
            failure_reason=failure_reason[:512] if failure_reason else None,
            detail=detail or {},
        )
        logger.info(
            "audit",
            action=action,
            result=result,
            operator=operator_username,
            request_id=request_id,
            failure_reason=failure_reason,
        )
        if session is not None:
            session.add(row)
            await session.flush()
            return row

        async with session_scope_optional() as scoped:
            if scoped is None:
                logger.error("audit_not_persisted", action=action, result=result)
                return None
            scoped.add(row)
            await scoped.flush()
            return row

    async def audit_trail(
        self,
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        operator_id: uuid.UUID | None = None,
        drone_uuid: uuid.UUID | None = None,
        mission_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(desc(AuditLog.occurred_at))
        if operator_id is not None:
            stmt = stmt.where(AuditLog.operator_id == operator_id)
        if drone_uuid is not None:
            stmt = stmt.where(AuditLog.drone_uuid == drone_uuid)
        if mission_id is not None:
            stmt = stmt.where(AuditLog.mission_id == mission_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        stmt = stmt.limit(min(limit, 1000)).offset(max(offset, 0))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # bus wiring
    # ------------------------------------------------------------------
    def register(self, resolve_drone_uuid: Any, resolve_mission_uuid: Any) -> None:
        """Subscribe the timeline writer to the events worth keeping.

        High-rate telemetry is deliberately excluded: the timeline is the
        operator narrative, not a sample log. Raw samples live in
        ``telemetry_samples``.
        """
        self._resolve_drone_uuid = resolve_drone_uuid
        self._resolve_mission_uuid = resolve_mission_uuid
        for event_type in _TIMELINE_EVENTS:
            self._bus.on(event_type, self._on_event)

    async def _on_event(self, event: Event) -> None:
        drone_uuid = (
            self._resolve_drone_uuid(event.drone_id) if event.drone_id else None
        )
        mission_uuid = (
            self._resolve_mission_uuid(event.mission_id) if event.mission_id else None
        )
        await self.record(
            event_type=event.payload.get("event_type", event.type),
            message=_summarise(event),
            severity=_severity_for(event),
            mission_id=mission_uuid,
            drone_uuid=drone_uuid,
            data=event.payload,
            occurred_at=event.occurred_at,
        )


_TIMELINE_EVENTS = (
    EventType.DRONE_CONNECTED,
    EventType.DRONE_DISCONNECTED,
    EventType.DRONE_IDENTITY_MISMATCH,
    EventType.MISSION_STATE_CHANGED,
    EventType.MISSION_TIME_WARNING,
    EventType.SEARCH_SECTOR_UPDATED,
    EventType.SURVIVOR_DETECTED,
    EventType.SURVIVOR_CONFIRMED,
    EventType.SURVIVOR_UPDATED,
    EventType.SURVIVOR_DUPLICATE_MERGED,
    EventType.DETECTION_REJECTED,
    EventType.DELIVERY_ASSIGNED,
    EventType.DELIVERY_UPDATED,
    EventType.DELIVERY_CONFIRMED,
    EventType.DELIVERY_REJECTED,
    EventType.ALERT_CREATED,
    EventType.COMMAND_UPDATED,
    EventType.MISSION_EVENT,
)

_SEVERITY_ORDER = [
    AlertSeverity.INFO,
    AlertSeverity.WARNING,
    AlertSeverity.CRITICAL,
    AlertSeverity.EMERGENCY,
]


def _severity_at_least(minimum: AlertSeverity) -> list[AlertSeverity]:
    index = _SEVERITY_ORDER.index(minimum)
    return _SEVERITY_ORDER[index:]


def _severity_for(event: Event) -> AlertSeverity:
    explicit = event.payload.get("severity")
    if explicit:
        try:
            return AlertSeverity(str(explicit).upper())
        except ValueError:
            pass
    if event.type in (
        EventType.DRONE_DISCONNECTED,
        EventType.DRONE_IDENTITY_MISMATCH,
        EventType.DELIVERY_REJECTED,
    ):
        return AlertSeverity.WARNING
    return AlertSeverity.INFO


def _summarise(event: Event) -> str:
    """Human-readable one-liner for the timeline panel."""
    payload = event.payload
    if "message" in payload:
        return str(payload["message"])

    match event.type:
        case EventType.DRONE_CONNECTED:
            return f"{event.drone_id} connected (system id {payload.get('system_id')})"
        case EventType.DRONE_DISCONNECTED:
            return f"{event.drone_id} disconnected: {payload.get('reason', 'unknown reason')}"
        case EventType.DRONE_IDENTITY_MISMATCH:
            return f"{event.drone_id} identity check failed: {payload.get('reason')}"
        case EventType.MISSION_STATE_CHANGED:
            return f"Mission {payload.get('from_state')} -> {payload.get('to_state')}"
        case EventType.SURVIVOR_DETECTED:
            return (
                f"{event.drone_id} detected survivor {payload.get('survivor_code', '')} "
                f"(confidence {payload.get('confidence')})"
            )
        case EventType.SURVIVOR_CONFIRMED:
            return f"Survivor {payload.get('survivor_code')} confirmed"
        case EventType.DELIVERY_ASSIGNED:
            return (
                f"Delivery {payload.get('task_code')} assigned to "
                f"{payload.get('drone_id', event.drone_id)}"
            )
        case EventType.DELIVERY_CONFIRMED:
            return (
                f"Delivery {payload.get('task_code')} confirmed via "
                f"{payload.get('confirmation_source')}"
            )
        case EventType.SEARCH_SECTOR_UPDATED:
            return f"Sector {payload.get('sector_code')} -> {payload.get('state')}"
        case EventType.COMMAND_UPDATED:
            return (
                f"{payload.get('command_type')} on {event.drone_id}: "
                f"{payload.get('state')}"
            )
        case _:
            return event.type.replace("_", " ").title()
