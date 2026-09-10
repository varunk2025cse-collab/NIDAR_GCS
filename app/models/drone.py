"""Physical aircraft, their link sessions and their observed states."""

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
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import DroneRole
from app.core.enums import ConnectionState
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Drone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A physical airframe.

    ``drone_id`` (D1/D2/D3) is an operator-facing label. The authoritative
    identity is (``system_id``, ``component_id``) as observed on the MAVLink
    link, cross-checked against ``hardware_uid`` when the autopilot reports one.
    """

    __tablename__ = "drones"
    __table_args__ = (
        UniqueConstraint("system_id", "component_id", name="uq_drones_mavlink_identity"),
    )

    drone_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[DroneRole] = mapped_column(
        SAEnum(DroneRole, name="drone_role", native_enum=False, length=16),
        nullable=False,
        default=DroneRole.UNKNOWN,
    )

    system_id: Mapped[int] = mapped_column(Integer, nullable=False)
    component_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    connection_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)

    # Populated from the autopilot itself the first time identity is verified.
    hardware_uid: Mapped[str | None] = mapped_column(String(64), index=True)
    autopilot_type: Mapped[str | None] = mapped_column(String(32))
    vehicle_type: Mapped[str | None] = mapped_column(String(32))
    firmware_version: Mapped[str | None] = mapped_column(String(64))
    firmware_vendor: Mapped[str | None] = mapped_column(String(64))
    product_name: Mapped[str | None] = mapped_column(String(64))

    payload_capacity_g: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Extensible, non-authoritative attributes (airframe notes, serials...).
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    connections: Mapped[list[DroneConnection]] = relationship(
        back_populates="drone", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Drone {self.drone_id} sys={self.system_id} role={self.role}>"


class DroneConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One link session to an aircraft.

    A new row is opened each time the link comes up, so the mission log shows
    exactly when contact was established and lost.
    """

    __tablename__ = "drone_connections"
    __table_args__ = (
        Index("ix_drone_connections_drone_opened", "drone_uuid", "opened_at"),
    )

    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[ConnectionState] = mapped_column(
        SAEnum(ConnectionState, name="connection_state", native_enum=False, length=16),
        nullable=False,
        default=ConnectionState.CONNECTING,
    )

    observed_system_id: Mapped[int | None] = mapped_column(Integer)
    observed_component_id: Mapped[int | None] = mapped_column(Integer)
    identity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    identity_failure_reason: Mapped[str | None] = mapped_column(String(255))

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(255))

    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    drone: Mapped[Drone] = relationship(back_populates="connections")


class DroneStateSnapshot(UUIDPrimaryKeyMixin, Base):
    """Periodic snapshot of the consolidated state shown to the operator.

    Distinct from ``telemetry_samples``: this is the *fused* state (connection,
    flight mode, armed, mission progress) rather than raw stream values.
    """

    __tablename__ = "drone_states"
    __table_args__ = (
        Index("ix_drone_states_drone_recorded", "drone_uuid", "recorded_at"),
    )

    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL")
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    connection_state: Mapped[ConnectionState] = mapped_column(
        SAEnum(ConnectionState, name="connection_state", native_enum=False, length=16),
        nullable=False,
    )
    armed: Mapped[bool | None] = mapped_column(Boolean)
    in_air: Mapped[bool | None] = mapped_column(Boolean)
    flight_mode: Mapped[str | None] = mapped_column(String(32))
    battery_percent: Mapped[float | None] = mapped_column(Float)
    position: Mapped[Any | None] = mapped_column(Geometry("POINTZ", srid=4326, spatial_index=False))
    heading_deg: Mapped[float | None] = mapped_column(Float)
    ground_speed_mps: Mapped[float | None] = mapped_column(Float)
    mission_progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: Per-field freshness at the moment of the snapshot.
    freshness: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
