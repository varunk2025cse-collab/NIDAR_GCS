"""Delivery tasks and their evidence trail."""

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
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DeliveryConfirmationSource, DeliveryState
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DeliveryTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "delivery_tasks"
    __table_args__ = (
        Index("ix_delivery_tasks_mission_state", "mission_id", "state"),
        Index("ix_delivery_tasks_drone_state", "drone_uuid", "state"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    survivor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivors.id", ondelete="CASCADE"), nullable=False
    )
    #: Null until a delivery aircraft is actually assigned.
    drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )

    task_code: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[DeliveryState] = mapped_column(
        SAEnum(DeliveryState, name="delivery_state", native_enum=False, length=32),
        nullable=False,
        default=DeliveryState.PENDING,
    )
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    target_position: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    delivery_altitude_m: Mapped[float | None] = mapped_column(Float)
    #: Planned route, if one was uploaded. What the aircraft actually flew is
    #: in telemetry_samples.
    planned_route: Mapped[Any | None] = mapped_column(Geometry("LINESTRING", srid=4326))
    planned_distance_m: Mapped[float | None] = mapped_column(Float)
    estimated_duration_s: Mapped[float | None] = mapped_column(Float)

    payload_type: Mapped[str | None] = mapped_column(String(64))
    payload_mass_g: Mapped[int | None] = mapped_column(Integer)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Physical confirmation. Without a row here the task can never be
    #: DELIVERED.
    confirmation_source: Mapped[DeliveryConfirmationSource | None] = mapped_column(
        SAEnum(
            DeliveryConfirmationSource,
            name="delivery_confirmation_source",
            native_enum=False,
            length=32,
        )
    )
    confirmation_detail: Mapped[str | None] = mapped_column(String(255))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )

    failure_reason: Mapped[str | None] = mapped_column(String(255))
    cancellation_reason: Mapped[str | None] = mapped_column(String(255))
    #: Snapshot of the safety evaluation that authorised dispatch.
    safety_evaluation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list[DeliveryEvent]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin",
        order_by="DeliveryEvent.occurred_at",
    )


class DeliveryEvent(UUIDPrimaryKeyMixin, Base):
    """Every transition and every piece of evidence behind it."""

    __tablename__ = "delivery_events"
    __table_args__ = (Index("ix_delivery_events_task_time", "task_id", "occurred_at"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_tasks.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    #: What the backend actually observed that justified the transition.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drone_commands.id", ondelete="SET NULL")
    )
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )
    automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str | None] = mapped_column(String(255))

    task: Mapped[DeliveryTask] = relationship(back_populates="events")
