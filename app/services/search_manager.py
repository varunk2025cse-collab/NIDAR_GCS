"""SearchSectorManager -- dividing the real search area and tracking coverage.

A sector is a geographic polygon with an owning aircraft and a coverage
percentage. The percentage is not a guess: it comes from the mission-item
progress PX4 reports for the plan that was uploaded to that aircraft, so
"Sector 3B 60% searched" means the aircraft has actually flown 60% of the
uploaded items.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point
from shapely.geometry import Polygon as ShapelyPolygon
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DroneRole, Settings
from app.core.enums import SECTOR_TRANSITIONS, CommandType, SectorState
from app.core.exceptions import (
    ConflictError,
    DroneNotReadyError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from app.core.geo import (
    Polygon,
    haversine_m,
    point_in_polygon,
    polygon_area_m2,
    polygon_bounds,
    polygon_centroid,
)
from app.core.logging import get_logger
from app.core.security import TokenPrincipal
from app.drone.types import MissionItemSpec
from app.models.mission import Mission
from app.models.search_sector import SearchAssignment, SearchSector, Waypoint
from app.realtime.event_bus import EventBus, EventType
from app.services.command_service import CommandService
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService

logger = get_logger(__name__)


class ProgressStatus(StrEnum):
    """How much the coverage figure for a sector can be trusted."""

    #: No aircraft has flown it yet. Coverage is not zero, it is unmeasured.
    NOT_STARTED = "NOT_STARTED"
    #: Assigned or in progress, but PX4 has not reported mission progress.
    #: Usually a link or telemetry problem, and worth showing as such.
    UNKNOWN = "UNKNOWN"
    #: Real mission-item progress was received from the aircraft.
    MEASURED = "MEASURED"


def progress_status(sector: SearchSector) -> ProgressStatus:
    """Classify a sector coverage figure.

    The distinction that matters: a sector nobody has flown and a sector
    genuinely 0% searched look identical in the ``progress`` column, and only
    one of them means the ground has been looked at.
    """
    if sector.progress_observed_at is not None:
        return ProgressStatus.MEASURED
    if sector.state in (SectorState.UNASSIGNED, SectorState.ASSIGNED):
        return ProgressStatus.NOT_STARTED
    if sector.state is SectorState.COMPLETED:
        # Completed without ever reporting progress: the record says done but
        # nothing measured it. Say so rather than claiming 100%.
        return ProgressStatus.UNKNOWN
    return ProgressStatus.UNKNOWN


def reported_progress(sector: SearchSector) -> float | None:
    """Coverage fraction, or None when it was never measured."""
    return (
        sector.progress
        if progress_status(sector) is ProgressStatus.MEASURED
        else None
    )


@dataclass(slots=True)
class SectorPlan:
    sector_code: str
    polygon: Polygon
    priority: int = 0


class SearchSectorManager:
    def __init__(
        self,
        fleet: FleetManager,
        commands: CommandService,
        geofence: GeofenceService,
        events: EventService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._commands = commands
        self._geofence = geofence
        self._events = events
        self._bus = bus
        self._settings = settings

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    async def create_sector(
        self,
        session: AsyncSession,
        mission: Mission,
        *,
        sector_code: str,
        polygon: Polygon,
        priority: int = 0,
        search_altitude_m: float | None = None,
    ) -> SearchSector:
        if len(polygon) < 3:
            raise ValidationError(
                "A sector polygon needs at least three vertices",
                details={"vertex_count": len(polygon)},
            )
        # Refuse a sector that would send an aircraft outside the fence.
        for lat, lon in polygon:
            self._geofence.require_inside(
                mission.id, lat, lon, what=f"sector {sector_code} vertex"
            )

        existing = await session.execute(
            select(SearchSector).where(
                SearchSector.mission_id == mission.id,
                SearchSector.sector_code == sector_code,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(
                f"Sector {sector_code} already exists for this mission",
                details={"sector_code": sector_code},
            )

        sector = SearchSector(
            mission_id=mission.id,
            sector_code=sector_code,
            boundary=from_shape(
                ShapelyPolygon([(lon, lat) for lat, lon in polygon]), srid=4326
            ),
            area_m2=round(polygon_area_m2(polygon), 1),
            priority=priority,
            state=SectorState.UNASSIGNED,
            search_altitude_m=search_altitude_m or mission.search_altitude_m,
        )
        session.add(sector)
        await session.flush()
        await self._emit_update(session, sector, "Sector created")
        return sector

    async def auto_partition(
        self,
        session: AsyncSession,
        mission: Mission,
        sector_count: int,
        *,
        prefix: str = "S",
    ) -> list[SearchSector]:
        """Split the mission search area into vertical strips.

        Strips are the simple, predictable division for a lawnmower search:
        each aircraft gets a contiguous band, so their flight paths do not
        cross and a sector can be reassigned without replanning the others.
        """
        if mission.search_area is None:
            raise ValidationError("Mission has no search area to partition")
        if sector_count < 1:
            raise ValidationError("sector_count must be at least 1")

        shape = to_shape(mission.search_area)
        area: Polygon = [(lat, lon) for lon, lat in shape.exterior.coords]
        bounds = polygon_bounds(area)
        if bounds is None:
            raise ValidationError("Search area polygon is empty")
        min_lat, min_lon, max_lat, max_lon = bounds

        sectors: list[SearchSector] = []
        step = (max_lon - min_lon) / sector_count
        for index in range(sector_count):
            strip_min_lon = min_lon + index * step
            strip_max_lon = min_lon + (index + 1) * step
            strip = ShapelyPolygon(
                [
                    (strip_min_lon, min_lat),
                    (strip_max_lon, min_lat),
                    (strip_max_lon, max_lat),
                    (strip_min_lon, max_lat),
                ]
            )
            clipped = shape.intersection(strip)
            if clipped.is_empty:
                continue
            # A concave search area can clip into several pieces; take the
            # largest so a sector is always one contiguous region.
            if clipped.geom_type == "MultiPolygon":
                clipped = max(clipped.geoms, key=lambda g: g.area)
            if clipped.geom_type != "Polygon":
                continue

            polygon: Polygon = [(lat, lon) for lon, lat in clipped.exterior.coords]
            sector = SearchSector(
                mission_id=mission.id,
                sector_code=f"{prefix}{index + 1}",
                boundary=from_shape(clipped, srid=4326),
                area_m2=round(polygon_area_m2(polygon), 1),
                priority=0,
                state=SectorState.UNASSIGNED,
                search_altitude_m=mission.search_altitude_m,
            )
            session.add(sector)
            sectors.append(sector)

        await session.flush()
        logger.info(
            "sectors_auto_partitioned",
            mission_id=str(mission.id),
            requested=sector_count,
            created=len(sectors),
        )
        for sector in sectors:
            await self._emit_update(session, sector, "Sector created by auto-partition")
        return sectors

    # ------------------------------------------------------------------
    # waypoint generation
    # ------------------------------------------------------------------
    async def generate_waypoints(
        self,
        session: AsyncSession,
        sector: SearchSector,
        *,
        line_spacing_m: float,
        altitude_m: float | None = None,
        speed_mps: float | None = None,
    ) -> list[Waypoint]:
        """Lay a boustrophedon (lawnmower) path over the sector.

        ``line_spacing_m`` should come from the camera footprint at the search
        altitude, not from a guess -- it is what determines whether the ground
        is actually covered.
        """
        if line_spacing_m <= 0:
            raise ValidationError("line_spacing_m must be positive")

        polygon = self._polygon_of(sector)
        bounds = polygon_bounds(polygon)
        if bounds is None:
            raise ValidationError("Sector polygon is empty")
        min_lat, min_lon, max_lat, max_lon = bounds

        altitude = altitude_m or sector.search_altitude_m
        if not altitude or altitude <= 0:
            raise ValidationError("No search altitude is configured for this sector")

        # Metres per degree at this latitude.
        mid_lat = (min_lat + max_lat) / 2
        m_per_deg_lat = haversine_m(mid_lat, min_lon, mid_lat + 0.001, min_lon) / 0.001
        m_per_deg_lon = haversine_m(mid_lat, min_lon, mid_lat, min_lon + 0.001) / 0.001
        if m_per_deg_lat <= 0 or m_per_deg_lon <= 0:
            raise ValidationError("Degenerate sector geometry")

        lat_step = line_spacing_m / m_per_deg_lat
        sample_step = max(line_spacing_m / 4, 5.0) / m_per_deg_lon

        await session.execute(
            Waypoint.__table__.delete().where(Waypoint.sector_id == sector.id)
        )

        waypoints: list[Waypoint] = []
        sequence = 0
        line_index = 0
        lat = min_lat
        while lat <= max_lat + 1e-12:
            # Walk the line and keep only the parts inside the polygon.
            inside_run: list[tuple[float, float]] = []
            lon = min_lon
            while lon <= max_lon + 1e-12:
                if point_in_polygon(lat, lon, polygon):
                    inside_run.append((lat, lon))
                lon += sample_step

            if len(inside_run) >= 2:
                start, end = inside_run[0], inside_run[-1]
                pair = [start, end] if line_index % 2 == 0 else [end, start]
                for point_lat, point_lon in pair:
                    waypoints.append(
                        Waypoint(
                            sector_id=sector.id,
                            sequence=sequence,
                            position=from_shape(Point(point_lon, point_lat), srid=4326),
                            relative_altitude_m=float(altitude),
                            speed_mps=speed_mps,
                            acceptance_radius_m=max(2.0, line_spacing_m / 4),
                            action="WAYPOINT",
                            parameters={"line": line_index},
                        )
                    )
                    sequence += 1
                line_index += 1
            lat += lat_step

        if not waypoints:
            raise ValidationError(
                "Sector produced no waypoints; the line spacing is larger than the sector",
                details={
                    "sector_code": sector.sector_code,
                    "line_spacing_m": line_spacing_m,
                    "area_m2": sector.area_m2,
                },
            )

        session.add_all(waypoints)
        await session.flush()
        logger.info(
            "sector_waypoints_generated",
            sector_code=sector.sector_code,
            count=len(waypoints),
            spacing_m=line_spacing_m,
            altitude_m=altitude,
        )
        return waypoints

    # ------------------------------------------------------------------
    # assignment
    # ------------------------------------------------------------------
    async def assign(
        self,
        session: AsyncSession,
        sector: SearchSector,
        drone_id: str,
        principal: TokenPrincipal | None = None,
    ) -> SearchSector:
        state = self._fleet.state(drone_id)
        if state.role is not DroneRole.SCOUT:
            raise DroneNotReadyError(
                f"{drone_id} is a {state.role} aircraft; search sectors need a SCOUT",
                details={"drone_id": drone_id, "role": str(state.role)},
            )

        drone_uuid = self._fleet.require_drone_uuid(drone_id)
        await self._transition(session, sector, SectorState.ASSIGNED)
        sector.assigned_drone_uuid = drone_uuid

        session.add(
            SearchAssignment(
                sector_id=sector.id,
                drone_uuid=drone_uuid,
                assigned_by=principal.operator_id if principal else None,
                assigned_at=datetime.now(UTC),
            )
        )
        self._fleet.assign_sector(drone_id, str(sector.id), sector.sector_code)
        await session.flush()
        await self._emit_update(
            session, sector, f"Sector {sector.sector_code} assigned to {drone_id}",
            drone_id=drone_id,
        )
        return sector

    async def release(
        self,
        session: AsyncSession,
        sector: SearchSector,
        reason: str,
    ) -> SearchSector:
        """Hand a sector back, e.g. when its scout drops out.

        The assignment history row is closed rather than deleted, so the record
        shows the sector was started by one aircraft and finished by another.
        """
        if sector.assigned_drone_uuid is not None:
            drone_id = self._fleet.drone_id_for_uuid(sector.assigned_drone_uuid)
            result = await session.execute(
                select(SearchAssignment)
                .where(
                    SearchAssignment.sector_id == sector.id,
                    SearchAssignment.released_at.is_(None),
                )
                .order_by(SearchAssignment.assigned_at.desc())
            )
            open_assignment = result.scalars().first()
            if open_assignment is not None:
                open_assignment.released_at = datetime.now(UTC)
                open_assignment.release_reason = reason[:255]
            if drone_id and self._fleet.connection_manager.has(drone_id):
                self._fleet.assign_sector(drone_id, None, None)

        sector.assigned_drone_uuid = None
        await self._transition(session, sector, SectorState.UNASSIGNED)
        await self._emit_update(session, sector, f"Sector released: {reason}")
        return sector

    async def auto_assign(
        self,
        session: AsyncSession,
        mission: Mission,
        principal: TokenPrincipal | None = None,
    ) -> list[tuple[str, str]]:
        """Spread unassigned sectors across the connected scouts.

        Sectors go to the scout with the fewest so far, highest-priority
        sectors first.
        """
        scouts = [s for s in self._fleet.scouts() if s.is_commandable]
        if not scouts:
            raise DroneNotReadyError("No connected scout aircraft to assign sectors to")

        result = await session.execute(
            select(SearchSector)
            .where(
                SearchSector.mission_id == mission.id,
                SearchSector.state == SectorState.UNASSIGNED,
            )
            .order_by(SearchSector.priority.desc(), SearchSector.sector_code)
        )
        sectors = list(result.scalars().all())
        load = {s.drone_id: 0 for s in scouts}
        assignments: list[tuple[str, str]] = []

        for sector in sectors:
            drone_id = min(load, key=lambda d: load[d])
            await self.assign(session, sector, drone_id, principal)
            load[drone_id] += 1
            assignments.append((sector.sector_code, drone_id))

        logger.info("sectors_auto_assigned", mission_id=str(mission.id),
                    assignments=assignments)
        return assignments

    # ------------------------------------------------------------------
    # upload and start
    # ------------------------------------------------------------------
    async def upload_and_start(
        self,
        session: AsyncSession,
        sector: SearchSector,
        principal: TokenPrincipal,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Push the sector plan to its aircraft and start it flying.

        The sector only becomes IN_PROGRESS once PX4 has confirmed the mission
        started; an upload that is not followed by a confirmed start leaves the
        sector ASSIGNED.
        """
        if sector.assigned_drone_uuid is None:
            raise ConflictError(
                f"Sector {sector.sector_code} is not assigned to an aircraft",
                details={"sector_code": sector.sector_code},
            )
        drone_id = self._fleet.drone_id_for_uuid(sector.assigned_drone_uuid)
        if drone_id is None:
            raise NotFoundError("Assigned aircraft is not in the current fleet")

        waypoints = await self._waypoints_of(session, sector)
        if not waypoints:
            raise ConflictError(
                f"Sector {sector.sector_code} has no waypoints to upload",
                details={"sector_code": sector.sector_code},
            )

        items = [
            MissionItemSpec(
                latitude=to_shape(w.position).y,
                longitude=to_shape(w.position).x,
                relative_altitude_m=w.relative_altitude_m,
                speed_mps=w.speed_mps,
                acceptance_radius_m=w.acceptance_radius_m,
            )
            for w in waypoints
        ]

        upload = await self._commands.execute(
            drone_id=drone_id,
            command_type=CommandType.UPLOAD_MISSION,
            principal=principal,
            mission_id=sector.mission_id,
            parameters={"items": items},
            request_id=request_id,
            idempotency_key=f"{sector.id}:upload:{len(items)}",
        )
        if not upload.acknowledged or upload.state.value.startswith("REJECT"):
            raise ConflictError(
                f"{drone_id} did not accept the plan for sector {sector.sector_code}",
                details=upload.as_dict(),
            )

        start = await self._commands.execute(
            drone_id=drone_id,
            command_type=CommandType.START_MISSION,
            principal=principal,
            mission_id=sector.mission_id,
            request_id=request_id,
            idempotency_key=f"{sector.id}:start",
        )

        if start.success:
            await self._transition(session, sector, SectorState.IN_PROGRESS)
            sector.start_time = datetime.now(UTC)
            await self._emit_update(
                session, sector, f"{drone_id} started searching {sector.sector_code}",
                drone_id=drone_id,
            )
        else:
            logger.error(
                "sector_start_unconfirmed",
                sector_code=sector.sector_code,
                drone_id=drone_id,
                state=str(start.state),
            )

        result = await session.execute(
            select(SearchAssignment)
            .where(
                SearchAssignment.sector_id == sector.id,
                SearchAssignment.released_at.is_(None),
            )
            .order_by(SearchAssignment.assigned_at.desc())
        )
        open_assignment = result.scalars().first()
        if open_assignment is not None:
            open_assignment.uploaded_item_count = len(items)

        return {
            "sector_code": sector.sector_code,
            "drone_id": drone_id,
            "waypoints": len(items),
            "upload": upload.as_dict(),
            "start": start.as_dict(),
            "sector_state": str(sector.state),
        }

    # ------------------------------------------------------------------
    # progress
    # ------------------------------------------------------------------
    async def sync_progress(self, session: AsyncSession, mission_id: uuid.UUID) -> int:
        """Update sector coverage from live PX4 mission progress.

        Progress is only taken from an aircraft that is actually assigned to
        the sector and reporting fresh mission telemetry. A sector whose scout
        has dropped out keeps its last real value rather than advancing.
        """
        result = await session.execute(
            select(SearchSector).where(
                SearchSector.mission_id == mission_id,
                SearchSector.state == SectorState.IN_PROGRESS,
            )
        )
        updated = 0
        for sector in result.scalars().all():
            if sector.assigned_drone_uuid is None:
                continue
            drone_id = self._fleet.drone_id_for_uuid(sector.assigned_drone_uuid)
            if drone_id is None or not self._fleet.connection_manager.has(drone_id):
                continue
            state = self._fleet.state(drone_id)
            progress = state.mission_progress.value
            if progress is None or progress.total <= 0:
                continue

            fraction = progress.fraction
            if abs(fraction - sector.progress) < 0.005:
                continue
            sector.progress = fraction
            sector.progress_observed_at = datetime.now(UTC)
            updated += 1

            if progress.current >= progress.total:
                await self._transition(session, sector, SectorState.COMPLETED)
                sector.completion_time = datetime.now(UTC)
                self._fleet.assign_sector(drone_id, None, None)
                await self._emit_update(
                    session, sector,
                    f"{drone_id} completed sector {sector.sector_code}",
                    drone_id=drone_id,
                )
            else:
                await self._emit_update(
                    session, sector,
                    f"Sector {sector.sector_code} {fraction * 100:.0f}% searched",
                    drone_id=drone_id, persist=False,
                )
        return updated

    async def block(
        self, session: AsyncSession, sector: SearchSector, reason: str
    ) -> SearchSector:
        await self._transition(session, sector, SectorState.BLOCKED)
        sector.blocked_reason = reason[:255]
        await self._emit_update(session, sector, f"Sector blocked: {reason}")
        return sector

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def list_sectors(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> list[SearchSector]:
        result = await session.execute(
            select(SearchSector)
            .where(SearchSector.mission_id == mission_id)
            .order_by(SearchSector.sector_code)
        )
        return list(result.scalars().all())

    async def get_sector(self, session: AsyncSession, sector_id: uuid.UUID) -> SearchSector:
        sector = await session.get(SearchSector, sector_id)
        if sector is None:
            raise NotFoundError(
                f"Sector {sector_id} does not exist", details={"sector_id": str(sector_id)}
            )
        return sector

    async def coverage_summary(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> dict[str, Any]:
        sectors = await self.list_sectors(session, mission_id)
        total_area = sum(s.area_m2 or 0.0 for s in sectors)

        by_state: dict[str, int] = {}
        by_progress: dict[str, int] = {}
        covered = 0.0
        unmeasured_area = 0.0
        for sector in sectors:
            by_state[str(sector.state)] = by_state.get(str(sector.state), 0) + 1
            status = progress_status(sector)
            by_progress[str(status)] = by_progress.get(str(status), 0) + 1
            area = sector.area_m2 or 0.0
            if status is ProgressStatus.MEASURED:
                covered += area * sector.progress
            else:
                # Never counted as searched. An unflown sector contributes
                # nothing to coverage, and its area is reported separately so
                # the operator can see how much of the area is unaccounted for.
                unmeasured_area += area

        return {
            "sector_count": len(sectors),
            "by_state": by_state,
            "by_progress_status": by_progress,
            "total_area_m2": round(total_area, 1),
            "covered_area_m2": round(covered, 1),
            "unmeasured_area_m2": round(unmeasured_area, 1),
            "coverage_fraction": round(covered / total_area, 4) if total_area else 0.0,
            "completed": by_state.get(str(SectorState.COMPLETED), 0),
            "unmeasured_sectors": by_progress.get(str(ProgressStatus.NOT_STARTED), 0)
            + by_progress.get(str(ProgressStatus.UNKNOWN), 0),
        }

    async def sectors_geojson(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> dict[str, Any]:
        sectors = await self.list_sectors(session, mission_id)
        features = []
        for sector in sectors:
            shape = to_shape(sector.boundary)
            drone_id = (
                self._fleet.drone_id_for_uuid(sector.assigned_drone_uuid)
                if sector.assigned_drone_uuid
                else None
            )
            centroid = polygon_centroid(self._polygon_of(sector))
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [list(map(list, shape.exterior.coords))],
                    },
                    "properties": {
                        "id": str(sector.id),
                        "sector_code": sector.sector_code,
                        "state": str(sector.state),
                        "assigned_drone": drone_id,
                        "progress": (
                            round(value, 4)
                            if (value := reported_progress(sector)) is not None
                            else None
                        ),
                        "progress_status": str(progress_status(sector)),
                        "priority": sector.priority,
                        "area_m2": sector.area_m2,
                        "start_time": (
                            sector.start_time.isoformat() if sector.start_time else None
                        ),
                        "completion_time": (
                            sector.completion_time.isoformat()
                            if sector.completion_time
                            else None
                        ),
                        "label_position": (
                            [centroid.longitude, centroid.latitude] if centroid else None
                        ),
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _polygon_of(self, sector: SearchSector) -> Polygon:
        shape = to_shape(sector.boundary)
        return [(lat, lon) for lon, lat in shape.exterior.coords]

    async def _waypoints_of(
        self, session: AsyncSession, sector: SearchSector
    ) -> list[Waypoint]:
        result = await session.execute(
            select(Waypoint)
            .where(Waypoint.sector_id == sector.id)
            .order_by(Waypoint.sequence)
        )
        return list(result.scalars().all())

    async def _transition(
        self, session: AsyncSession, sector: SearchSector, to_state: SectorState
    ) -> None:
        if to_state is sector.state:
            return
        allowed = SECTOR_TRANSITIONS.get(sector.state, frozenset())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Sector {sector.sector_code}", str(sector.state), str(to_state)
            )
        sector.state = to_state
        await session.flush()

    async def _emit_update(
        self,
        session: AsyncSession,
        sector: SearchSector,
        message: str,
        drone_id: str | None = None,
        persist: bool = True,
    ) -> None:
        payload = {
            "sector_id": str(sector.id),
            "sector_code": sector.sector_code,
            "state": str(sector.state),
            "progress": (
                round(value, 4)
                if (value := reported_progress(sector)) is not None
                else None
            ),
            "progress_status": str(progress_status(sector)),
            "assigned_drone": drone_id,
            "message": message,
        }
        self._bus.emit(
            EventType.SEARCH_SECTOR_UPDATED,
            mission_id=str(sector.mission_id),
            drone_id=drone_id,
            payload=payload,
        )
        if persist:
            await self._events.record(
                event_type="SEARCH_SECTOR_UPDATED",
                message=message,
                mission_id=sector.mission_id,
                drone_uuid=sector.assigned_drone_uuid,
                data=payload,
                session=session,
            )


def estimate_line_spacing_m(
    altitude_m: float, horizontal_fov_deg: float, overlap_fraction: float = 0.2
) -> float:
    """Ground track spacing for a given altitude and camera field of view.

    Uses the real camera geometry rather than an arbitrary number, so the
    generated pattern actually covers the ground at the configured overlap.
    """
    if altitude_m <= 0 or not (0 < horizontal_fov_deg < 180):
        raise ValidationError("Invalid altitude or field of view for spacing calculation")
    footprint = 2 * altitude_m * math.tan(math.radians(horizontal_fov_deg / 2))
    return max(1.0, footprint * (1.0 - overlap_fraction))
