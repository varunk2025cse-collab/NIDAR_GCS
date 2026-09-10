"""SurvivorManager -- turning detection events into survivor records.

The central judgement this service makes is *when two detections are the same
person*. D1 and D2 sweeping adjacent sectors will both see someone in the
overlap; creating S001 and S002 for one person would send two delivery
sorties and misreport the survivor count.

The rule used here: a new detection within ``duplicate_radius_m`` (widened by
the position accuracy of both sightings) and ``duplicate_window_s`` of an
existing survivor is treated as another observation of that survivor, not a
new one. The raw detection is always kept and linked, so the provenance of
every survivor record stays inspectable.

Confirmation is evidence-based: a single very high-confidence, well-located
detection confirms; otherwise corroboration from a second sighting is
required. Nothing is confirmed merely because it arrived.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.geolocation import fuse_positions
from app.core.config import Settings
from app.core.enums import SURVIVOR_TRANSITIONS, AlertSeverity, DetectionSource, SurvivorState
from app.core.exceptions import ConflictError, InvalidStateTransitionError, NotFoundError
from app.core.geo import haversine_m
from app.core.logging import get_logger
from app.models.survivor import Survivor, SurvivorDetection, SurvivorObservation
from app.realtime.event_bus import EventBus, EventType
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager

logger = get_logger(__name__)


def duplicate_match_threshold_m(
    base_radius_m: float,
    new_accuracy_m: float | None,
    existing_accuracy_m: float | None,
) -> float:
    """How far apart two sightings can be and still be the same person.

    The base radius is widened by the position uncertainty of both fixes. Two
    sightings each accurate only to +/-15 m can legitimately be 30 m apart and
    still be one survivor; using the bare radius there would create a second
    record and send a second aircraft.
    """
    threshold = base_radius_m
    if new_accuracy_m is not None and new_accuracy_m > 0:
        threshold += new_accuracy_m
    if existing_accuracy_m is not None and existing_accuracy_m > 0:
        threshold += existing_accuracy_m
    return threshold


def is_duplicate_candidate(
    distance_m: float,
    base_radius_m: float,
    new_accuracy_m: float | None,
    existing_accuracy_m: float | None,
) -> bool:
    return distance_m <= duplicate_match_threshold_m(
        base_radius_m, new_accuracy_m, existing_accuracy_m
    )


@dataclass(slots=True)
class DuplicateMatch:
    survivor: Survivor
    distance_m: float
    threshold_m: float
    age_s: float


@dataclass(slots=True)
class IngestResult:
    detection: SurvivorDetection
    survivor: Survivor | None
    created_new: bool
    duplicate_of: DuplicateMatch | None = None
    confirmed: bool = False
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "detection_id": str(self.detection.id),
            "external_detection_id": self.detection.external_detection_id,
            "survivor_id": str(self.survivor.id) if self.survivor else None,
            "survivor_code": self.survivor.survivor_code if self.survivor else None,
            "survivor_state": str(self.survivor.state) if self.survivor else None,
            "created_new_survivor": self.created_new,
            "matched_existing": self.duplicate_of is not None,
            "match_distance_m": (
                round(self.duplicate_of.distance_m, 1) if self.duplicate_of else None
            ),
            "confirmed": self.confirmed,
            "warnings": self.warnings,
        }


class SurvivorManager:
    def __init__(
        self,
        fleet: FleetManager,
        events: EventService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._events = events
        self._bus = bus
        self._settings = settings

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    async def ingest_detection(
        self,
        session: AsyncSession,
        *,
        mission_id: uuid.UUID,
        drone_id: str,
        external_detection_id: str,
        detected_at: datetime,
        latitude: float,
        longitude: float,
        confidence: float,
        estimated_accuracy_m: float | None,
        source: DetectionSource = DetectionSource.ONBOARD_AI,
        geolocation_method: str | None = None,
        drone_position: tuple[float, float] | None = None,
        drone_relative_altitude_m: float | None = None,
        drone_absolute_altitude_m: float | None = None,
        drone_heading_deg: float | None = None,
        drone_attitude: tuple[float, float, float] | None = None,
        pixel: tuple[int, int] | None = None,
        image_size: tuple[int, int] | None = None,
        camera_metadata: dict[str, Any] | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        image_reference: str | None = None,
        payload: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> IngestResult:
        """Record a detection and attach it to a survivor.

        Idempotent on ``(drone, external_detection_id)``: a companion computer
        that retries after a dropped link does not create a second record.
        """
        drone_uuid = self._fleet.require_drone_uuid(drone_id)

        existing = await session.execute(
            select(SurvivorDetection).where(
                SurvivorDetection.drone_uuid == drone_uuid,
                SurvivorDetection.external_detection_id == external_detection_id,
            )
        )
        prior = existing.scalar_one_or_none()
        if prior is not None:
            survivor = (
                await session.get(Survivor, prior.survivor_id) if prior.survivor_id else None
            )
            logger.info(
                "detection_replay_ignored",
                drone_id=drone_id,
                external_detection_id=external_detection_id,
            )
            return IngestResult(
                detection=prior,
                survivor=survivor,
                created_new=False,
                warnings=["Detection was already ingested; no new record was created"],
            )

        detection = SurvivorDetection(
            mission_id=mission_id,
            drone_uuid=drone_uuid,
            external_detection_id=external_detection_id,
            source=source,
            detected_at=detected_at,
            received_at=datetime.now(UTC),
            detected_position=from_shape(Point(longitude, latitude), srid=4326),
            drone_position=(
                from_shape(
                    Point(
                        drone_position[1],
                        drone_position[0],
                        drone_relative_altitude_m or 0.0,
                    ),
                    srid=4326,
                )
                if drone_position
                else None
            ),
            drone_relative_altitude_m=drone_relative_altitude_m,
            drone_absolute_altitude_m=drone_absolute_altitude_m,
            drone_heading_deg=drone_heading_deg,
            drone_roll_deg=drone_attitude[0] if drone_attitude else None,
            drone_pitch_deg=drone_attitude[1] if drone_attitude else None,
            drone_yaw_deg=drone_attitude[2] if drone_attitude else None,
            confidence=confidence,
            estimated_accuracy_m=estimated_accuracy_m,
            geolocation_method=geolocation_method,
            pixel_x=pixel[0] if pixel else None,
            pixel_y=pixel[1] if pixel else None,
            image_width=image_size[0] if image_size else None,
            image_height=image_size[1] if image_size else None,
            camera_metadata=camera_metadata or {},
            model_name=model_name,
            model_version=model_version,
            image_reference=image_reference,
            accepted=True,
            payload=payload or {},
        )
        session.add(detection)
        await session.flush()

        match = await self._find_duplicate(
            session, mission_id, latitude, longitude, estimated_accuracy_m, detected_at
        )

        if match is not None:
            survivor = await self._attach_observation(
                session, match.survivor, detection, latitude, longitude,
                estimated_accuracy_m, confidence,
            )
            detection.survivor_id = survivor.id
            detection.is_duplicate_of_existing = True
            detection.duplicate_distance_m = match.distance_m
            result = IngestResult(
                detection=detection,
                survivor=survivor,
                created_new=False,
                duplicate_of=match,
                warnings=list(warnings or []),
            )
            logger.info(
                "detection_merged_into_survivor",
                survivor_code=survivor.survivor_code,
                drone_id=drone_id,
                distance_m=round(match.distance_m, 1),
            )
            self._bus.emit(
                EventType.SURVIVOR_DUPLICATE_MERGED,
                drone_id=drone_id,
                mission_id=str(mission_id),
                survivor_id=str(survivor.id),
                payload={
                    "survivor_code": survivor.survivor_code,
                    "detection_id": detection.external_detection_id,
                    "distance_m": round(match.distance_m, 1),
                    "observation_count": survivor.observation_count,
                    "message": (
                        f"{drone_id} re-detected {survivor.survivor_code} "
                        f"({match.distance_m:.0f} m from the existing fix)"
                    ),
                },
            )
        else:
            survivor = await self._create_survivor(
                session, mission_id, detection, latitude, longitude,
                estimated_accuracy_m, confidence, detected_at,
            )
            detection.survivor_id = survivor.id
            result = IngestResult(
                detection=detection,
                survivor=survivor,
                created_new=True,
                warnings=list(warnings or []),
            )
            logger.info(
                "survivor_created",
                survivor_code=survivor.survivor_code,
                drone_id=drone_id,
                confidence=confidence,
                accuracy_m=estimated_accuracy_m,
            )
            self._bus.emit(
                EventType.SURVIVOR_DETECTED,
                drone_id=drone_id,
                mission_id=str(mission_id),
                survivor_id=str(survivor.id),
                payload={
                    "survivor_code": survivor.survivor_code,
                    "latitude": latitude,
                    "longitude": longitude,
                    "confidence": confidence,
                    "accuracy_m": estimated_accuracy_m,
                    "state": str(survivor.state),
                    "detection_id": external_detection_id,
                    "detected_at": detected_at.isoformat(),
                },
            )

        await session.flush()

        # Evaluate whether the accumulated evidence confirms this survivor.
        if survivor is not None and await self._try_confirm(session, survivor):
            result.confirmed = True

        return result

    async def _create_survivor(
        self,
        session: AsyncSession,
        mission_id: uuid.UUID,
        detection: SurvivorDetection,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        confidence: float,
        detected_at: datetime,
    ) -> Survivor:
        code = await self._next_code(session, mission_id)
        survivor = Survivor(
            mission_id=mission_id,
            survivor_code=code,
            state=SurvivorState.DETECTED,
            state_changed_at=datetime.now(UTC),
            location=from_shape(Point(longitude, latitude), srid=4326),
            location_accuracy_m=accuracy_m,
            best_confidence=confidence,
            observation_count=1,
            first_detected_at=detected_at,
            last_observed_at=detected_at,
        )
        session.add(survivor)
        await session.flush()

        session.add(
            SurvivorObservation(
                survivor_id=survivor.id,
                detection_id=detection.id,
                drone_uuid=detection.drone_uuid,
                observed_at=detected_at,
                position=from_shape(Point(longitude, latitude), srid=4326),
                accuracy_m=accuracy_m,
                confidence=confidence,
                observation_type="DETECTION",
            )
        )
        await self._events.record(
            event_type="SURVIVOR_DETECTED",
            message=(
                f"Survivor {code} detected at {latitude:.6f}, {longitude:.6f} "
                f"(confidence {confidence:.2f}"
                + (f", accuracy +/-{accuracy_m:.0f} m" if accuracy_m else ", accuracy unknown")
                + ")"
            ),
            mission_id=mission_id,
            drone_uuid=detection.drone_uuid,
            survivor_id=survivor.id,
            data={
                "survivor_code": code,
                "confidence": confidence,
                "accuracy_m": accuracy_m,
                "latitude": latitude,
                "longitude": longitude,
            },
            session=session,
        )
        return survivor

    async def _attach_observation(
        self,
        session: AsyncSession,
        survivor: Survivor,
        detection: SurvivorDetection,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        confidence: float,
    ) -> Survivor:
        """Fold a repeat sighting into an existing survivor record."""
        session.add(
            SurvivorObservation(
                survivor_id=survivor.id,
                detection_id=detection.id,
                drone_uuid=detection.drone_uuid,
                observed_at=detection.detected_at,
                position=from_shape(Point(longitude, latitude), srid=4326),
                accuracy_m=accuracy_m,
                confidence=confidence,
                observation_type="DETECTION",
            )
        )

        # Re-fuse the position from every sighting so the estimate improves
        # with evidence instead of jumping to the newest reading.
        result = await session.execute(
            select(SurvivorObservation).where(
                SurvivorObservation.survivor_id == survivor.id,
                SurvivorObservation.position.isnot(None),
            )
        )
        points: list[tuple[float, float, float | None]] = []
        for observation in result.scalars().all():
            shape = to_shape(observation.position)
            points.append((shape.y, shape.x, observation.accuracy_m))
        points.append((latitude, longitude, accuracy_m))

        fused_lat, fused_lon, fused_accuracy = fuse_positions(points)
        survivor.location = from_shape(Point(fused_lon, fused_lat), srid=4326)
        survivor.location_accuracy_m = fused_accuracy
        survivor.observation_count = len(points)
        survivor.last_observed_at = detection.detected_at
        if survivor.best_confidence is None or confidence > survivor.best_confidence:
            survivor.best_confidence = confidence
        await session.flush()
        return survivor

    async def _find_duplicate(
        self,
        session: AsyncSession,
        mission_id: uuid.UUID,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        detected_at: datetime,
    ) -> DuplicateMatch | None:
        """Find an existing survivor that this detection probably is.

        The match radius is the configured base radius widened by the position
        uncertainty of both fixes: two sightings each accurate to +/-15 m can
        legitimately be 30 m apart and still be the same person.
        """
        config = self._settings.survivor
        window_start = detected_at - timedelta(seconds=config.duplicate_window_s)

        result = await session.execute(
            select(Survivor).where(
                Survivor.mission_id == mission_id,
                Survivor.state.notin_(
                    [
                        SurvivorState.DUPLICATE,
                        SurvivorState.REJECTED,
                        SurvivorState.CANCELLED,
                    ]
                ),
                Survivor.last_observed_at >= window_start,
            )
        )

        best: DuplicateMatch | None = None
        for survivor in result.scalars().all():
            shape = to_shape(survivor.location)
            distance = haversine_m(latitude, longitude, shape.y, shape.x)
            threshold = duplicate_match_threshold_m(
                config.duplicate_radius_m, accuracy_m, survivor.location_accuracy_m
            )

            if distance <= threshold and (best is None or distance < best.distance_m):
                age = (
                    (detected_at - survivor.last_observed_at).total_seconds()
                    if survivor.last_observed_at
                    else 0.0
                )
                best = DuplicateMatch(
                    survivor=survivor,
                    distance_m=distance,
                    threshold_m=threshold,
                    age_s=age,
                )
        return best

    async def _next_code(self, session: AsyncSession, mission_id: uuid.UUID) -> str:
        count = await session.scalar(
            select(func.count(Survivor.id)).where(Survivor.mission_id == mission_id)
        )
        return f"S{(count or 0) + 1:03d}"

    # ------------------------------------------------------------------
    # confirmation
    # ------------------------------------------------------------------
    async def _try_confirm(self, session: AsyncSession, survivor: Survivor) -> bool:
        """Confirm a survivor when the evidence justifies it.

        Two independent routes:

        * one detection above ``auto_confirm_confidence`` that is also located
          well enough to fly to; or
        * ``corroborations_for_confirm`` separate sightings.

        Everything else waits for an operator. A survivor that cannot be
        located accurately enough is never auto-confirmed, however confident
        the classifier was.
        """
        if survivor.state not in (SurvivorState.DETECTED, SurvivorState.VALIDATING):
            return False

        config = self._settings.survivor
        accuracy = survivor.location_accuracy_m
        confidence = survivor.best_confidence or 0.0

        well_located = (
            accuracy is not None and accuracy <= config.max_auto_confirm_accuracy_m
        )
        high_confidence = confidence >= config.auto_confirm_confidence
        corroborated = survivor.observation_count >= config.corroborations_for_confirm

        if not well_located:
            if survivor.state is SurvivorState.DETECTED:
                await self.transition(
                    session,
                    survivor,
                    SurvivorState.VALIDATING,
                    reason=(
                        "Position accuracy is insufficient for automatic confirmation"
                        if accuracy is not None
                        else "Position accuracy is unknown"
                    ),
                )
            return False

        if not (high_confidence or corroborated):
            if survivor.state is SurvivorState.DETECTED:
                await self.transition(
                    session, survivor, SurvivorState.VALIDATING,
                    reason="Awaiting corroboration",
                )
            return False

        await self.transition(
            session,
            survivor,
            SurvivorState.CONFIRMED,
            reason=(
                f"confidence {confidence:.2f}"
                if high_confidence
                else f"{survivor.observation_count} corroborating sightings"
            ),
        )
        await self.transition(session, survivor, SurvivorState.PENDING_DELIVERY,
                              reason="Ready for delivery assignment")
        return True

    async def confirm_manually(
        self,
        session: AsyncSession,
        survivor: Survivor,
        operator_id: uuid.UUID,
        notes: str | None = None,
    ) -> Survivor:
        await self.transition(
            session, survivor, SurvivorState.CONFIRMED,
            reason="Confirmed by operator", operator_id=operator_id,
        )
        if notes:
            survivor.notes = notes
        await self.transition(
            session, survivor, SurvivorState.PENDING_DELIVERY,
            reason="Ready for delivery assignment", operator_id=operator_id,
        )
        return survivor

    async def reject(
        self,
        session: AsyncSession,
        survivor: Survivor,
        reason: str,
        operator_id: uuid.UUID | None = None,
    ) -> Survivor:
        survivor.rejection_reason = reason[:255]
        return await self.transition(
            session, survivor, SurvivorState.REJECTED, reason=reason, operator_id=operator_id
        )

    async def mark_duplicate(
        self,
        session: AsyncSession,
        survivor: Survivor,
        primary: Survivor,
        operator_id: uuid.UUID | None = None,
    ) -> Survivor:
        """Merge one survivor record into another after the fact."""
        if survivor.id == primary.id:
            raise ConflictError("A survivor cannot be a duplicate of itself")
        survivor.duplicate_of_id = primary.id
        await self.transition(
            session,
            survivor,
            SurvivorState.DUPLICATE,
            reason=f"Merged into {primary.survivor_code}",
            operator_id=operator_id,
        )
        await session.execute(
            SurvivorDetection.__table__.update()
            .where(SurvivorDetection.survivor_id == survivor.id)
            .values(survivor_id=primary.id, is_duplicate_of_existing=True)
        )
        primary.observation_count += survivor.observation_count
        await session.flush()
        return survivor

    # ------------------------------------------------------------------
    # state machine
    # ------------------------------------------------------------------
    async def transition(
        self,
        session: AsyncSession,
        survivor: Survivor,
        to_state: SurvivorState,
        *,
        reason: str | None = None,
        operator_id: uuid.UUID | None = None,
    ) -> Survivor:
        current = survivor.state
        if to_state is current:
            return survivor
        allowed = SURVIVOR_TRANSITIONS.get(current, frozenset())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Survivor {survivor.survivor_code}", str(current), str(to_state)
            )

        survivor.state = to_state
        survivor.state_changed_at = datetime.now(UTC)
        if to_state is SurvivorState.CONFIRMED:
            survivor.confirmed_at = datetime.now(UTC)
        if to_state is SurvivorState.DELIVERED:
            survivor.delivered_at = datetime.now(UTC)
        await session.flush()

        message = (
            f"Survivor {survivor.survivor_code}: {current} -> {to_state}"
            + (f" ({reason})" if reason else "")
        )
        await self._events.record(
            event_type=(
                "SURVIVOR_CONFIRMED"
                if to_state is SurvivorState.CONFIRMED
                else "SURVIVOR_UPDATED"
            ),
            message=message,
            severity=AlertSeverity.INFO,
            mission_id=survivor.mission_id,
            survivor_id=survivor.id,
            operator_id=operator_id,
            data={
                "survivor_code": survivor.survivor_code,
                "from_state": str(current),
                "to_state": str(to_state),
                "reason": reason,
            },
            session=session,
        )
        self._bus.emit(
            EventType.SURVIVOR_CONFIRMED
            if to_state is SurvivorState.CONFIRMED
            else EventType.SURVIVOR_UPDATED,
            mission_id=str(survivor.mission_id),
            survivor_id=str(survivor.id),
            payload={
                "survivor_code": survivor.survivor_code,
                "from_state": str(current),
                "to_state": str(to_state),
                "reason": reason,
                "confidence": survivor.best_confidence,
                "accuracy_m": survivor.location_accuracy_m,
                "message": message,
            },
        )
        logger.info(
            "survivor_state_changed",
            survivor_code=survivor.survivor_code,
            from_state=str(current),
            to_state=str(to_state),
            reason=reason,
        )
        return survivor

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def get(self, session: AsyncSession, survivor_id: uuid.UUID) -> Survivor:
        survivor = await session.get(Survivor, survivor_id)
        if survivor is None:
            raise NotFoundError(
                f"Survivor {survivor_id} does not exist",
                details={"survivor_id": str(survivor_id)},
            )
        return survivor

    async def list_survivors(
        self,
        session: AsyncSession,
        mission_id: uuid.UUID | None = None,
        states: list[SurvivorState] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Survivor]:
        stmt = select(Survivor).order_by(Survivor.first_detected_at.desc())
        if mission_id is not None:
            stmt = stmt.where(Survivor.mission_id == mission_id)
        if states:
            stmt = stmt.where(Survivor.state.in_(states))
        result = await session.execute(stmt.limit(min(limit, 1000)).offset(max(offset, 0)))
        return list(result.scalars().all())

    async def pending_delivery(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> list[Survivor]:
        result = await session.execute(
            select(Survivor)
            .where(
                Survivor.mission_id == mission_id,
                Survivor.state == SurvivorState.PENDING_DELIVERY,
            )
            .order_by(Survivor.priority.desc(), Survivor.first_detected_at)
        )
        return list(result.scalars().all())

    async def summary(self, session: AsyncSession, mission_id: uuid.UUID) -> dict[str, Any]:
        result = await session.execute(
            select(Survivor.state, func.count(Survivor.id))
            .where(Survivor.mission_id == mission_id)
            .group_by(Survivor.state)
        )
        by_state = {str(state): count for state, count in result.all()}
        # "Found" excludes records that turned out to be duplicates or false
        # positives, so the dashboard count means what an operator thinks it
        # means.
        excluded = {
            str(SurvivorState.DUPLICATE),
            str(SurvivorState.REJECTED),
            str(SurvivorState.CANCELLED),
        }
        return {
            "found": sum(v for k, v in by_state.items() if k not in excluded),
            "delivered": by_state.get(str(SurvivorState.DELIVERED), 0),
            "pending_delivery": by_state.get(str(SurvivorState.PENDING_DELIVERY), 0),
            "confirmed": by_state.get(str(SurvivorState.CONFIRMED), 0),
            "duplicates": by_state.get(str(SurvivorState.DUPLICATE), 0),
            "rejected": by_state.get(str(SurvivorState.REJECTED), 0),
            "by_state": by_state,
        }

    async def geojson(self, session: AsyncSession, mission_id: uuid.UUID) -> dict[str, Any]:
        survivors = await self.list_survivors(session, mission_id, limit=1000)
        features = []
        for survivor in survivors:
            if survivor.state in (SurvivorState.DUPLICATE, SurvivorState.REJECTED):
                continue
            shape = to_shape(survivor.location)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [shape.x, shape.y]},
                    "properties": {
                        "id": str(survivor.id),
                        "survivor_code": survivor.survivor_code,
                        "state": str(survivor.state),
                        "confidence": survivor.best_confidence,
                        "accuracy_m": survivor.location_accuracy_m,
                        "observation_count": survivor.observation_count,
                        "first_detected_at": survivor.first_detected_at.isoformat(),
                        "delivered_at": (
                            survivor.delivered_at.isoformat()
                            if survivor.delivered_at
                            else None
                        ),
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    async def detections_for(
        self, session: AsyncSession, survivor_id: uuid.UUID
    ) -> list[SurvivorDetection]:
        result = await session.execute(
            select(SurvivorDetection)
            .where(SurvivorDetection.survivor_id == survivor_id)
            .order_by(SurvivorDetection.detected_at)
        )
        return list(result.scalars().all())

    async def record_rejected_detection(
        self,
        session: AsyncSession,
        *,
        mission_id: uuid.UUID | None,
        drone_id: str,
        external_detection_id: str,
        detected_at: datetime,
        latitude: float,
        longitude: float,
        confidence: float,
        reason: str,
        code: str,
        payload: dict[str, Any] | None = None,
    ) -> SurvivorDetection | None:
        """Keep a rejected detection as evidence.

        A perception stack that starts producing rejects (bad clock, drifted
        geofence, low confidence) is a fault worth seeing, and it is invisible
        if rejected events are dropped.
        """
        if mission_id is None:
            logger.warning(
                "detection_rejected_unrecorded",
                drone_id=drone_id, reason=reason, code=code,
            )
            self._bus.emit(
                EventType.DETECTION_REJECTED,
                drone_id=drone_id,
                payload={"reason": reason, "code": code,
                         "detection_id": external_detection_id},
            )
            return None

        drone_uuid = self._fleet.drone_uuid(drone_id)
        if drone_uuid is None:
            return None

        detection = SurvivorDetection(
            mission_id=mission_id,
            drone_uuid=drone_uuid,
            external_detection_id=external_detection_id,
            source=DetectionSource.ONBOARD_AI,
            detected_at=detected_at,
            received_at=datetime.now(UTC),
            detected_position=from_shape(Point(longitude, latitude), srid=4326),
            confidence=confidence,
            accepted=False,
            rejection_reason=f"{code}: {reason}"[:255],
            payload=payload or {},
        )
        session.add(detection)
        await session.flush()

        self._bus.emit(
            EventType.DETECTION_REJECTED,
            drone_id=drone_id,
            mission_id=str(mission_id),
            payload={
                "detection_id": external_detection_id,
                "code": code,
                "reason": reason,
                "message": f"Detection from {drone_id} rejected: {reason}",
            },
        )
        logger.warning(
            "detection_rejected",
            drone_id=drone_id,
            external_detection_id=external_detection_id,
            code=code,
            reason=reason,
        )
        return detection
