"""Tracked lifecycle of every command aimed at a physical aircraft.

One row per operator intent. ``state`` never reaches COMPLETED on the strength
of a send alone: a command is complete only when the aircraft was observed to
change state, or is explicitly recorded as ACKNOWLEDGED-but-unverified.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CommandState, CommandType
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DroneCommand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "drone_commands"
    __table_args__ = (
        # The idempotency guarantee: the same key can never launch two
        # physical actions.
        UniqueConstraint("idempotency_key", name="uq_drone_commands_idempotency"),
        Index("ix_drone_commands_drone_time", "drone_uuid", "created_at"),
        Index("ix_drone_commands_mission_time", "mission_id", "created_at"),
        Index("ix_drone_commands_state", "state"),
    )

    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL")
    )
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )

    command_type: Mapped[CommandType] = mapped_column(
        SAEnum(CommandType, name="command_type", native_enum=False, length=32), nullable=False
    )
    state: Mapped[CommandState] = mapped_column(
        SAEnum(CommandState, name="command_state", native_enum=False, length=32),
        nullable=False,
        default=CommandState.REQUESTED,
    )

    #: Client-supplied or server-derived. Unique across all commands.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    timeout_s: Mapped[float | None] = mapped_column(Float)
    #: Raw acknowledgement/result as reported by MAVSDK.
    acknowledgement: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: The observed vehicle state change that verified the command, if any.
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    failure_reason: Mapped[str | None] = mapped_column(String(255))
    #: Preconditions evaluated before the command was allowed to leave the GCS.
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    transitions: Mapped[list[CommandTransition]] = relationship(
        back_populates="command", cascade="all, delete-orphan", lazy="selectin",
        order_by="CommandTransition.occurred_at",
    )


class CommandTransition(UUIDPrimaryKeyMixin, Base):
    """Append-only trace of a command moving through its lifecycle."""

    __tablename__ = "command_transitions"
    __table_args__ = (Index("ix_command_transitions_command", "command_id", "occurred_at"),)

    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drone_commands.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[CommandState] = mapped_column(
        SAEnum(CommandState, name="command_state", native_enum=False, length=32), nullable=False
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    command: Mapped[DroneCommand] = relationship(back_populates="transitions")
