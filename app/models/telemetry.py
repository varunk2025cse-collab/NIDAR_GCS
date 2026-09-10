"""Persisted telemetry samples.

Every row is a value that actually arrived from a flight controller. Rows are
written by the telemetry service at a configured rate (and on significant
movement); nothing is interpolated or back-filled.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin


class TelemetrySample(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "telemetry_samples"
    __table_args__ = (
        Index("ix_telemetry_samples_drone_time", "drone_uuid", "sampled_at"),
        Index("ix_telemetry_samples_mission_time", "mission_id", "sampled_at"),
        Index("ix_telemetry_samples_position", "position", postgresql_using="gist"),
    )

    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL")
    )

    #: When the GCS received the value. PX4 boot time, where available, is in
    #: ``extra`` so the two clocks are never conflated.
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # POINTZ carries relative altitude in Z; absolute altitude is separate
    # because the two have different references and must not be confused.
    position: Mapped[Any | None] = mapped_column(Geometry("POINTZ", srid=4326))
    relative_altitude_m: Mapped[float | None] = mapped_column(Float)
    absolute_altitude_m: Mapped[float | None] = mapped_column(Float)

    ground_speed_mps: Mapped[float | None] = mapped_column(Float)
    vertical_speed_mps: Mapped[float | None] = mapped_column(Float)
    heading_deg: Mapped[float | None] = mapped_column(Float)
    roll_deg: Mapped[float | None] = mapped_column(Float)
    pitch_deg: Mapped[float | None] = mapped_column(Float)
    yaw_deg: Mapped[float | None] = mapped_column(Float)

    battery_percent: Mapped[float | None] = mapped_column(Float)
    battery_voltage_v: Mapped[float | None] = mapped_column(Float)
    battery_current_a: Mapped[float | None] = mapped_column(Float)

    gps_fix_type: Mapped[int | None] = mapped_column(Integer)
    satellites: Mapped[int | None] = mapped_column(Integer)
    horizontal_accuracy_m: Mapped[float | None] = mapped_column(Float)
    vertical_accuracy_m: Mapped[float | None] = mapped_column(Float)

    armed: Mapped[bool | None] = mapped_column(Boolean)
    in_air: Mapped[bool | None] = mapped_column(Boolean)
    flight_mode: Mapped[str | None] = mapped_column(String(32))

    #: Per-field freshness at the moment of sampling, so a replay of the log
    #: can distinguish "value known" from "value assumed".
    freshness: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
