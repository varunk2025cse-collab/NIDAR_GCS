"""Geofence supervision.

PX4 enforces the onboard geofence and owns the aircraft response to a breach.
This service is the *supervisor* side: it evaluates every real position
against the mission boundary so the GCS can warn the operator early, refuse to
plan work outside the area, and record the breach.

It never tries to fly the aircraft back. That is the flight controller job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import GeofenceStatus, GeofenceType
from app.core.exceptions import GeofenceViolationError
from app.core.geo import Polygon, distance_to_polygon_boundary_m, point_in_polygon
from app.core.logging import get_logger
from app.drone.state import DroneState
from app.drone.types import GeofencePolygonSpec
from app.models.mission import Geofence, MissionGeofence

logger = get_logger(__name__)


@dataclass(slots=True)
class ActiveFence:
    fence_id: uuid.UUID
    name: str
    fence_type: GeofenceType
    polygon: Polygon
    min_altitude_m: float | None = None
    max_altitude_m: float | None = None


@dataclass(slots=True)
class FenceEvaluation:
    status: GeofenceStatus
    margin_m: float | None
    breached_fences: list[str]
    detail: dict[str, Any]


class GeofenceService:
    """Holds the active fences for a mission and evaluates positions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        #: mission_id (str) -> fences
        self._fences: dict[str, list[ActiveFence]] = {}

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    async def load_for_mission(
        self, session: AsyncSession, mission_id: uuid.UUID
    ) -> list[ActiveFence]:
        stmt = (
            select(Geofence)
            .join(MissionGeofence, MissionGeofence.geofence_id == Geofence.id)
            .where(MissionGeofence.mission_id == mission_id)
        )
        result = await session.execute(stmt)
        fences: list[ActiveFence] = []
        for row in result.scalars().all():
            shape = to_shape(row.boundary)
            # PostGIS stores (lon, lat); the geo helpers work in (lat, lon).
            polygon: Polygon = [(lat, lon) for lon, lat in shape.exterior.coords]
            fences.append(
                ActiveFence(
                    fence_id=row.id,
                    name=row.name,
                    fence_type=row.fence_type,
                    polygon=polygon,
                    min_altitude_m=row.min_altitude_m,
                    max_altitude_m=row.max_altitude_m,
                )
            )
        self._fences[str(mission_id)] = fences
        logger.info("geofences_loaded", mission_id=str(mission_id), count=len(fences))
        return fences

    def clear_mission(self, mission_id: uuid.UUID | str) -> None:
        self._fences.pop(str(mission_id), None)

    def fences_for(self, mission_id: uuid.UUID | str | None) -> list[ActiveFence]:
        if mission_id is None:
            return []
        return self._fences.get(str(mission_id), [])

    def has_fences(self, mission_id: uuid.UUID | str | None) -> bool:
        return bool(self.fences_for(mission_id))

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def evaluate(
        self,
        mission_id: uuid.UUID | str | None,
        latitude: float,
        longitude: float,
        altitude_m: float | None = None,
    ) -> FenceEvaluation:
        fences = self.fences_for(mission_id)
        if not fences:
            return FenceEvaluation(
                status=GeofenceStatus.UNKNOWN,
                margin_m=None,
                breached_fences=[],
                detail={"reason": "no geofence configured for this mission"},
            )

        margin = self._settings.safety.geofence_warning_margin_m
        breached: list[str] = []
        margins: list[float] = []
        near: list[str] = []

        for fence in fences:
            inside = point_in_polygon(latitude, longitude, fence.polygon)
            distance = distance_to_polygon_boundary_m(latitude, longitude, fence.polygon)

            if fence.fence_type is GeofenceType.INCLUSION:
                if not inside:
                    breached.append(fence.name)
                else:
                    margins.append(distance)
                    if distance <= margin:
                        near.append(fence.name)
                if inside and altitude_m is not None:
                    if fence.max_altitude_m is not None and altitude_m > fence.max_altitude_m:
                        breached.append(f"{fence.name} (max altitude)")
                    if fence.min_altitude_m is not None and altitude_m < fence.min_altitude_m:
                        breached.append(f"{fence.name} (min altitude)")
            else:  # exclusion zone
                if inside:
                    breached.append(fence.name)
                else:
                    margins.append(distance)
                    if distance <= margin:
                        near.append(fence.name)

        if breached:
            status = GeofenceStatus.BREACHED
        elif near:
            status = GeofenceStatus.NEAR_BOUNDARY
        else:
            status = GeofenceStatus.INSIDE

        return FenceEvaluation(
            status=status,
            margin_m=round(min(margins), 1) if margins else None,
            breached_fences=breached,
            detail={
                "near_boundary": near,
                "warning_margin_m": margin,
                "fence_count": len(fences),
            },
        )

    def evaluate_state(self, state: DroneState) -> FenceEvaluation | None:
        """Update a drone state from its current real position."""
        position = state.position.value
        if position is None:
            state.geofence_status = GeofenceStatus.UNKNOWN
            state.geofence_margin_m = None
            return None
        evaluation = self.evaluate(
            state.mission_id,
            position.latitude,
            position.longitude,
            position.relative_altitude_m,
        )
        state.geofence_status = evaluation.status
        state.geofence_margin_m = evaluation.margin_m
        return evaluation

    def require_inside(
        self,
        mission_id: uuid.UUID | str | None,
        latitude: float,
        longitude: float,
        what: str = "location",
    ) -> FenceEvaluation:
        """Refuse a location that lies outside the mission boundary.

        Used when planning sectors and dispatching deliveries so the GCS never
        commands an aircraft towards a point PX4 will refuse to fly to.
        """
        evaluation = self.evaluate(mission_id, latitude, longitude)
        if evaluation.status is GeofenceStatus.BREACHED:
            raise GeofenceViolationError(
                f"{what} lies outside the mission geofence",
                details={
                    "latitude": latitude,
                    "longitude": longitude,
                    "breached_fences": evaluation.breached_fences,
                },
            )
        return evaluation

    # ------------------------------------------------------------------
    # upload
    # ------------------------------------------------------------------
    def upload_specs(self, mission_id: uuid.UUID | str) -> list[GeofencePolygonSpec]:
        """Convert the mission fences into adapter upload specs.

        The same definition drives GCS supervision and the PX4 onboard fence,
        so the two cannot disagree about where the boundary is.
        """
        return [
            GeofencePolygonSpec(
                points=list(fence.polygon),
                inclusion=fence.fence_type is GeofenceType.INCLUSION,
            )
            for fence in self.fences_for(mission_id)
        ]

    def geojson(self, mission_id: uuid.UUID | str) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[lon, lat] for lat, lon in fence.polygon]],
                    },
                    "properties": {
                        "id": str(fence.fence_id),
                        "name": fence.name,
                        "fence_type": str(fence.fence_type),
                        "min_altitude_m": fence.min_altitude_m,
                        "max_altitude_m": fence.max_altitude_m,
                    },
                }
                for fence in self.fences_for(mission_id)
            ],
        }
