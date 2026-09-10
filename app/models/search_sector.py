"""Search sectors, their assignment to scouts, and planned waypoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
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

from app.core.enums import SectorState
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SearchSector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "search_sectors"
    __table_args__ = (
        UniqueConstraint("mission_id", "sector_code", name="uq_search_sectors_code"),
        Index("ix_search_sectors_mission_state", "mission_id", "state"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    sector_code: Mapped[str] = mapped_column(String(32), nullable=False)
    boundary: Mapped[Any] = mapped_column(Geometry("POLYGON", srid=4326), nullable=False)
    area_m2: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    state: Mapped[SectorState] = mapped_column(
        SAEnum(SectorState, name="sector_state", native_enum=False, length=16),
        nullable=False,
        default=SectorState.UNASSIGNED,
    )
    assigned_drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )

    #: 0.0-1.0, derived from real mission-item progress reported by PX4.
    #: Meaningless unless ``progress_observed_at`` is set: 0.0 on its own is
    #: the initial value, not a measurement that the sector is 0% searched.
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: When PX4 last reported mission progress for this sector. NULL means no
    #: aircraft has ever reported progress, so coverage is NOT_STARTED or
    #: UNKNOWN rather than zero.
    progress_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason: Mapped[str | None] = mapped_column(String(255))

    search_altitude_m: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    waypoints: Mapped[list[Waypoint]] = relationship(
        back_populates="sector", cascade="all, delete-orphan", lazy="selectin",
        order_by="Waypoint.sequence",
    )
    assignments: Mapped[list[SearchAssignment]] = relationship(
        back_populates="sector", cascade="all, delete-orphan", lazy="selectin"
    )


class SearchAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """History of sector-to-aircraft assignments.

    Kept as its own table so a reassignment after a scout drops out is a fact
    in the record rather than an overwritten column.
    """

    __tablename__ = "search_assignments"
    __table_args__ = (Index("ix_search_assignments_sector", "sector_id", "assigned_at"),)

    sector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("search_sectors.id", ondelete="CASCADE"), nullable=False
    )
    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(255))
    #: Mission items uploaded to the aircraft for this assignment.
    uploaded_item_count: Mapped[int | None] = mapped_column(Integer)

    sector: Mapped[SearchSector] = relationship(back_populates="assignments")


class Waypoint(UUIDPrimaryKeyMixin, Base):
    """A planned mission item.

    This is the *plan*. What the aircraft actually flew comes from telemetry,
    never from this table.
    """

    __tablename__ = "waypoints"
    __table_args__ = (
        UniqueConstraint("sector_id", "sequence", name="uq_waypoints_sector_sequence"),
    )

    sector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("search_sectors.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    relative_altitude_m: Mapped[float] = mapped_column(Float, nullable=False)
    speed_mps: Mapped[float | None] = mapped_column(Float)
    acceptance_radius_m: Mapped[float | None] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="WAYPOINT")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    sector: Mapped[SearchSector] = relationship(back_populates="waypoints")
