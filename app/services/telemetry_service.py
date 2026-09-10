"""Telemetry persistence and fan-out.

The live values already exist in :class:`DroneState`; this service decides
what gets *written down* and what gets pushed to clients, at rates that suit a
database and a WebSocket rather than a 4 Hz MAVLink stream.

Sampling policy: write a row when the configured interval has elapsed, or
whenever the aircraft has moved more than a configured distance. That keeps a
loitering aircraft cheap while never losing the shape of a flight path.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import TelemetryStatus
from app.core.geo import haversine_m
from app.core.logging import get_logger
from app.database.session import session_scope_optional
from app.drone.state import DroneState
from app.models.drone import DroneStateSnapshot
from app.models.telemetry import TelemetrySample
from app.realtime.event_bus import EventBus, EventType
from app.services.fleet_manager import FleetManager

logger = get_logger(__name__)


@dataclass(slots=True)
class _SampleGate:
    """Per-drone throttle state."""

    last_persist_monotonic: float = 0.0
    last_broadcast_monotonic: float = 0.0
    last_latitude: float | None = None
    last_longitude: float | None = None
    written: int = 0
    skipped: int = 0
    failures: int = 0
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class TelemetryService:
    def __init__(self, fleet: FleetManager, bus: EventBus, settings: Settings) -> None:
        self._fleet = fleet
        self._bus = bus
        self._settings = settings
        self._policy = fleet.policy
        self._gates: dict[str, _SampleGate] = {}
        self._database_healthy = True

    def install(self) -> None:
        """Attach the persistence hook to every live link."""
        self._fleet.connection_manager.add_telemetry_hook(self.on_telemetry)
        logger.info("telemetry_service_installed")

    # ------------------------------------------------------------------
    # hot path
    # ------------------------------------------------------------------
    async def on_telemetry(self, state: DroneState, stream: str) -> None:
        """Called after every telemetry update on every link.

        Kept cheap: two monotonic comparisons in the common case. Anything
        slow here would back up a MAVLink stream.
        """
        gate = self._gates.setdefault(state.drone_id, _SampleGate())
        now = time.monotonic()

        if self._should_broadcast(gate, now):
            gate.last_broadcast_monotonic = now
            self._bus.emit(
                EventType.TELEMETRY_UPDATED,
                drone_id=state.drone_id,
                mission_id=state.mission_id,
                payload=state.card(self._policy, now),
            )

        if self._should_persist(state, gate, now, stream):
            await self._persist(state, gate, now)

    def _should_broadcast(self, gate: _SampleGate, now: float) -> bool:
        interval = self._settings.fleet_broadcast_interval_s
        return (now - gate.last_broadcast_monotonic) >= interval

    def _should_persist(
        self, state: DroneState, gate: _SampleGate, now: float, stream: str
    ) -> bool:
        # Only position updates drive the distance rule; other streams fall
        # back to the time rule so a stationary aircraft still gets sampled.
        elapsed = now - gate.last_persist_monotonic
        if elapsed >= self._settings.telemetry_persist_interval_s:
            return True
        if stream != "position":
            return False
        position = state.position.value
        if position is None or gate.last_latitude is None or gate.last_longitude is None:
            return position is not None
        moved = haversine_m(
            gate.last_latitude, gate.last_longitude, position.latitude, position.longitude
        )
        return moved >= self._settings.telemetry_persist_min_distance_m

    async def _persist(self, state: DroneState, gate: _SampleGate, now: float) -> None:
        drone_uuid = self._fleet.drone_uuid(state.drone_id)
        if drone_uuid is None:
            gate.skipped += 1
            return

        position = state.position.value
        velocity = state.velocity.value
        attitude = state.attitude.value
        battery = state.battery.value
        gps = state.gps.value

        sample = TelemetrySample(
            drone_uuid=drone_uuid,
            mission_id=uuid.UUID(state.mission_id) if state.mission_id else None,
            sampled_at=datetime.now(UTC),
            position=(
                from_shape(
                    Point(
                        position.longitude,
                        position.latitude,
                        position.relative_altitude_m or 0.0,
                    ),
                    srid=4326,
                )
                if position
                else None
            ),
            relative_altitude_m=position.relative_altitude_m if position else None,
            absolute_altitude_m=position.absolute_altitude_m if position else None,
            ground_speed_mps=round(velocity.ground_speed_mps, 3) if velocity else None,
            vertical_speed_mps=round(velocity.vertical_speed_mps, 3) if velocity else None,
            heading_deg=state.heading.value,
            roll_deg=attitude.roll_deg if attitude else None,
            pitch_deg=attitude.pitch_deg if attitude else None,
            yaw_deg=attitude.yaw_deg if attitude else None,
            battery_percent=battery.remaining_percent if battery else None,
            battery_voltage_v=battery.voltage_v if battery else None,
            battery_current_a=battery.current_a if battery else None,
            gps_fix_type=gps.fix_type if gps else None,
            satellites=gps.satellites if gps else None,
            horizontal_accuracy_m=gps.horizontal_accuracy_m if gps else None,
            vertical_accuracy_m=gps.vertical_accuracy_m if gps else None,
            armed=state.armed.value,
            in_air=state.in_air.value,
            flight_mode=state.flight_mode.value,
            freshness=self._freshness_map(state),
            extra={
                "connection_state": str(state.connection_state),
                "geofence_status": str(state.geofence_status),
                "sector_code": state.sector_code,
            },
        )

        async with session_scope_optional() as session:
            if session is None:
                gate.failures += 1
                gate.last_error = "database unavailable"
                self._database_healthy = False
                return
            try:
                session.add(sample)
                await session.flush()
            except Exception as exc:
                gate.failures += 1
                gate.last_error = str(exc)
                logger.error("telemetry_persist_failed", drone_id=state.drone_id,
                             error=str(exc))
                raise

        self._database_healthy = True
        gate.written += 1
        gate.last_persist_monotonic = now
        if position is not None:
            gate.last_latitude = position.latitude
            gate.last_longitude = position.longitude

    def _freshness_map(self, state: DroneState) -> dict[str, str]:
        """Freshness of each stream at the instant of sampling.

        Stored with the row so a replay can distinguish a value that was live
        from one that was already ageing.
        """
        return {
            stream: str(self._policy.status(stream, holder))
            for stream, holder in (
                ("position", state.position),
                ("velocity", state.velocity),
                ("attitude", state.attitude),
                ("battery", state.battery),
                ("gps", state.gps),
                ("health", state.health),
                ("flight_mode", state.flight_mode),
            )
        }

    # ------------------------------------------------------------------
    # periodic fused-state snapshots
    # ------------------------------------------------------------------
    async def snapshot_fleet_state(self) -> int:
        """Write one consolidated state row per drone.

        Cheaper to query than raw samples when reconstructing "what did the
        fleet look like at 10:04" for a post-mission review.
        """
        written = 0
        async with session_scope_optional() as session:
            if session is None:
                return 0
            now = datetime.now(UTC)
            for state in self._fleet.states.values():
                drone_uuid = self._fleet.drone_uuid(state.drone_id)
                if drone_uuid is None:
                    continue
                position = state.position.value
                velocity = state.velocity.value
                progress = state.mission_progress.value
                health = state.health.value
                session.add(
                    DroneStateSnapshot(
                        drone_uuid=drone_uuid,
                        mission_id=uuid.UUID(state.mission_id) if state.mission_id else None,
                        recorded_at=now,
                        connection_state=state.connection_state,
                        armed=state.armed.value,
                        in_air=state.in_air.value,
                        flight_mode=state.flight_mode.value,
                        battery_percent=state.battery_percent(),
                        position=(
                            from_shape(
                                Point(
                                    position.longitude,
                                    position.latitude,
                                    position.relative_altitude_m or 0.0,
                                ),
                                srid=4326,
                            )
                            if position
                            else None
                        ),
                        heading_deg=state.heading.value,
                        ground_speed_mps=(
                            round(velocity.ground_speed_mps, 3) if velocity else None
                        ),
                        mission_progress=(
                            {"current": progress.current, "total": progress.total}
                            if progress
                            else {}
                        ),
                        health=health.as_dict() if health else {},
                        freshness=self._freshness_map(state),
                    )
                )
                written += 1
        return written

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def history(
        self,
        session: AsyncSession,
        drone_uuid: uuid.UUID,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[TelemetrySample]:
        """Raw samples for the telemetry chart, oldest first."""
        stmt = select(TelemetrySample).where(TelemetrySample.drone_uuid == drone_uuid)
        if since is not None:
            stmt = stmt.where(TelemetrySample.sampled_at >= since)
        if until is not None:
            stmt = stmt.where(TelemetrySample.sampled_at <= until)
        stmt = stmt.order_by(TelemetrySample.sampled_at.desc()).limit(min(limit, 5000))
        result = await session.execute(stmt)
        return list(reversed(result.scalars().all()))

    async def track(
        self,
        session: AsyncSession,
        drone_uuid: uuid.UUID,
        mission_id: uuid.UUID | None = None,
        limit: int = 2000,
    ) -> list[tuple[float, float, datetime]]:
        """Persisted flight path, for the map trail after a page reload."""
        stmt = select(
            TelemetrySample.position, TelemetrySample.sampled_at
        ).where(TelemetrySample.drone_uuid == drone_uuid, TelemetrySample.position.isnot(None))
        if mission_id is not None:
            stmt = stmt.where(TelemetrySample.mission_id == mission_id)
        stmt = stmt.order_by(TelemetrySample.sampled_at.desc()).limit(min(limit, 10000))
        result = await session.execute(stmt)

        from geoalchemy2.shape import to_shape

        points: list[tuple[float, float, datetime]] = []
        for geom, sampled_at in result.all():
            shape = to_shape(geom)
            points.append((shape.y, shape.x, sampled_at))
        return list(reversed(points))

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "database_healthy": self._database_healthy,
            "drones": {
                drone_id: {
                    "samples_written": gate.written,
                    "samples_skipped": gate.skipped,
                    "failures": gate.failures,
                    "last_error": gate.last_error,
                }
                for drone_id, gate in self._gates.items()
            },
        }

    def live_freshness(self) -> dict[str, dict[str, str]]:
        return {
            drone_id: self._freshness_map(state)
            for drone_id, state in self._fleet.states.items()
        }

    def any_fresh_position(self) -> bool:
        return any(
            self._policy.status("position", s.position) is TelemetryStatus.FRESH
            for s in self._fleet.states.values()
        )
