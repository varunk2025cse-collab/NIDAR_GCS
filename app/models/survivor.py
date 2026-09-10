"""Survivors, the raw detections that produced them, and later observations.

The distinction matters operationally:

* ``SurvivorDetection`` -- a raw event from a companion computer. Immutable
  evidence, kept whether or not it is believed.
* ``Survivor`` -- the GCS conclusion that a physical person is at a location.
  Created from one or more detections, and only ever CONFIRMED against the
  configured evidence rules.
* ``SurvivorObservation`` -- a later sighting linked to an existing survivor.
"""

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

from app.core.enums import DetectionSource, SurvivorState
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Survivor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "survivors"
    __table_args__ = (
        UniqueConstraint("mission_id", "survivor_code", name="uq_survivors_code"),
        Index("ix_survivors_mission_state", "mission_id", "state"),
        Index("ix_survivors_location", "location", postgresql_using="gist"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    #: Operator-facing label, e.g. S001. Allocated by the backend, never by a
    #: client.
    survivor_code: Mapped[str] = mapped_column(String(16), nullable=False)

    state: Mapped[SurvivorState] = mapped_column(
        SAEnum(SurvivorState, name="survivor_state", native_enum=False, length=32),
        nullable=False,
        default=SurvivorState.DETECTED,
    )
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Best current estimate, fused from the linked detections.
    location: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    #: Estimated geolocation accuracy in metres. This is a *position* error,
    #: deliberately separate from detection confidence.
    location_accuracy_m: Mapped[float | None] = mapped_column(Float)
    ground_altitude_amsl_m: Mapped[float | None] = mapped_column(Float)

    #: Highest detection confidence among the linked detections (0.0-1.0).
    best_confidence: Mapped[float | None] = mapped_column(Float)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set when this record was merged into another as a duplicate.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivors.id", ondelete="SET NULL")
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    detections: Mapped[list[SurvivorDetection]] = relationship(
        back_populates="survivor", lazy="selectin", foreign_keys="SurvivorDetection.survivor_id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Survivor {self.survivor_code} {self.state}>"


class SurvivorDetection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Raw detection event as reported by an onboard perception system.

    Stored exactly as received (plus provenance) so the evidence behind a
    survivor record can always be re-examined.
    """

    __tablename__ = "survivor_detections"
    __table_args__ = (
        UniqueConstraint("drone_uuid", "external_detection_id", name="uq_detections_external_id"),
        Index("ix_detections_mission_time", "mission_id", "detected_at"),
        Index("ix_detections_position", "detected_position", postgresql_using="gist"),
    )

    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id", ondelete="CASCADE"), nullable=False
    )
    drone_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False
    )
    survivor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivors.id", ondelete="SET NULL")
    )

    #: Detection id assigned by the companion computer (DET-001...). Unique per
    #: drone, which is what makes ingestion idempotent.
    external_detection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[DetectionSource] = mapped_column(
        SAEnum(DetectionSource, name="detection_source", native_enum=False, length=16),
        nullable=False,
        default=DetectionSource.ONBOARD_AI,
    )

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Where the perception system believes the survivor is.
    detected_position: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    #: Where the aircraft was when it made the detection. Kept separately so
    #: geolocation can be recomputed later from raw evidence.
    drone_position: Mapped[Any | None] = mapped_column(Geometry("POINTZ", srid=4326))
    drone_relative_altitude_m: Mapped[float | None] = mapped_column(Float)
    drone_absolute_altitude_m: Mapped[float | None] = mapped_column(Float)
    drone_heading_deg: Mapped[float | None] = mapped_column(Float)
    drone_roll_deg: Mapped[float | None] = mapped_column(Float)
    drone_pitch_deg: Mapped[float | None] = mapped_column(Float)
    drone_yaw_deg: Mapped[float | None] = mapped_column(Float)

    #: Model confidence in the *classification*, 0.0-1.0.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    #: Estimated error of the *position*, in metres. Unrelated to confidence.
    estimated_accuracy_m: Mapped[float | None] = mapped_column(Float)
    geolocation_method: Mapped[str | None] = mapped_column(String(32))

    #: Pixel coordinates and camera intrinsics/extrinsics if supplied.
    pixel_x: Mapped[int | None] = mapped_column(Integer)
    pixel_y: Mapped[int | None] = mapped_column(Integer)
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    camera_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(32))
    image_reference: Mapped[str | None] = mapped_column(String(255))

    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(255))
    #: True when this detection was matched onto an existing survivor.
    is_duplicate_of_existing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    duplicate_distance_m: Mapped[float | None] = mapped_column(Float)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    survivor: Mapped[Survivor | None] = relationship(
        back_populates="detections", foreign_keys=[survivor_id]
    )


class SurvivorObservation(UUIDPrimaryKeyMixin, Base):
    """A later look at a known survivor (re-detection, operator note, delivery
    overflight) that updates confidence or position without creating a new
    survivor record."""

    __tablename__ = "survivor_observations"
    __table_args__ = (Index("ix_survivor_observations_survivor", "survivor_id", "observed_at"),)

    survivor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivors.id", ondelete="CASCADE"), nullable=False
    )
    detection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("survivor_detections.id", ondelete="SET NULL")
    )
    drone_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="SET NULL")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    position: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326))
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    observation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="DETECTION")
    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
