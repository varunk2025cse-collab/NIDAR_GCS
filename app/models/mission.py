"""Missions, the operators attached to them, and geofences."""

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
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import GeofenceType, MissionState
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Mission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "missions"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[MissionState] = mapped_column(
        SAEnum(MissionState, name="mission_state", native_enum=False, length=32),
        nullable=False,
        default=MissionState.DRAFT,
        index=True,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )

    # Launch / recovery point, from the real survey of the site.
    launch_point: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326))
    launch_altitude_amsl_m: Mapped[float | None] = mapped_column(Float)
    search_area: Mapped[Any | None] = mapped_column(Geometry("POLYGON", srid=4326))

    planned_duration_s: Mapped[int | None] = mapped_column(Integer)
    max_duration_s: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    search_altitude_m: Mapped[float | None] = mapped_column(Float)
    delivery_altitude_m: Mapped[float | None] = mapped_column(Float)

    abort_reason: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(String(255))

    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    assignments: Mapped[list[MissionDroneAssignment]] = relationship(
        back_populates="mission", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Mission {self.name} {self.state}>"


class MissionOperator(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operators authorised on a mission, and their role for it."""

    __tablename__ = "mission_operators"
    __table_args__ = (
        UniqueConstraint("mission_id", "operator_id", name="uq_mission_operators_pair"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    operator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="CASCADE"), nullable=False
    )
    is_commander: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MissionDroneAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Which physical aircraft take part in a mission, and in what capacity."""

    __tablename__ = "mission_drones"
    __table_args__ = (
        UniqueConstraint("mission_id", "drone_uuid", name="uq_mission_drones_pair"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    assigned_role: Mapped[str] = mapped_column(String(16), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mission: Mapped[Mission] = relationship(back_populates="assignments")


class Geofence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable geofence polygon.

    The GCS monitors these for supervision and alerting. PX4 enforces its own
    onboard geofence; the two are uploaded from the same definition but PX4
    remains the authority in the air.
    """

    __tablename__ = "geofences"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    fence_type: Mapped[GeofenceType] = mapped_column(
        SAEnum(GeofenceType, name="geofence_type", native_enum=False, length=16),
        nullable=False,
        default=GeofenceType.INCLUSION,
    )
    boundary: Mapped[Any] = mapped_column(Geometry("POLYGON", srid=4326), nullable=False)
    min_altitude_m: Mapped[float | None] = mapped_column(Float)
    max_altitude_m: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class MissionGeofence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mission_geofences"
    __table_args__ = (
        UniqueConstraint("mission_id", "geofence_id", name="uq_mission_geofences_pair"),
        Index("ix_mission_geofences_mission", "mission_id"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    geofence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("geofences.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_to_vehicles: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
