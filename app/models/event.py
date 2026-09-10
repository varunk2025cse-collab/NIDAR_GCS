"""Mission timeline events, alerts, audit log and system health history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AlertCategory, AlertSeverity, ComponentStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MissionEvent(UUIDPrimaryKeyMixin, Base):
    """The mission timeline.

    Everything the operator sees in the "Recent Events" panel and everything
    in the post-mission report comes from this table.
    """

    __tablename__ = "mission_events"
    __table_args__ = (
        Index("ix_mission_events_mission_time", "mission_id", "occurred_at"),
        Index("ix_mission_events_type_time", "event_type", "occurred_at"),
    )

    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE")
    )
    drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )
    survivor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivors.id", ondelete="SET NULL")
    )
    delivery_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_tasks.id", ondelete="SET NULL")
    )
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, name="alert_severity", native_enum=False, length=16),
        nullable=False,
        default=AlertSeverity.INFO,
    )
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A safety condition raised by the safety engine or a service.

    Alerts are deduplicated by ``dedupe_key`` while active, so a drone sitting
    at 24% battery produces one standing alert rather than one per second.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_active", "active", "severity", "raised_at"),
        Index("ix_alerts_mission_time", "mission_id", "raised_at"),
        Index("ix_alerts_dedupe_active", "dedupe_key", "active"),
    )

    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE")
    )
    drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )

    category: Mapped[AlertCategory] = mapped_column(
        SAEnum(AlertCategory, name="alert_category", native_enum=False, length=32), nullable=False
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, name="alert_severity", native_enum=False, length=16), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)

    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )

    #: The measured values that triggered it, with their freshness.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    position: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326))


class AuditLog(UUIDPrimaryKeyMixin, Base):
    """Append-only record of every control action.

    Written for anything that can move an aircraft or change mission state,
    including the ones that were refused.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_time", "occurred_at"),
        Index("ix_audit_logs_operator_time", "operator_id", "occurred_at"),
        Index("ix_audit_logs_drone_time", "drone_uuid", "occurred_at"),
    )

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )
    operator_username: Mapped[str | None] = mapped_column(String(64))
    operator_role: Mapped[str | None] = mapped_column(String(16))

    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL")
    )
    drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drone_commands.id", ondelete="SET NULL")
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_ip: Mapped[str | None] = mapped_column(INET)

    result: Mapped[str] = mapped_column(String(32), nullable=False)
    acknowledgement: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class SystemHealthSample(UUIDPrimaryKeyMixin, Base):
    """Recorded subsystem status, so a post-mission review can see when the
    GCS itself was degraded."""

    __tablename__ = "system_health"
    __table_args__ = (
        UniqueConstraint("component", "sampled_at", name="uq_system_health_component_time"),
        Index("ix_system_health_component_time", "component", "sampled_at"),
    )

    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ComponentStatus] = mapped_column(
        SAEnum(ComponentStatus, name="component_status", native_enum=False, length=16),
        nullable=False,
    )
    latency_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
