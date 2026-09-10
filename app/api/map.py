"""Map data.

Everything the map view draws, in GeoJSON. Live aircraft positions come from
current telemetry: an aircraft whose position has gone stale is reported in
``stale_drones`` with the time and place contact was lost, and is *not* drawn
as a live marker. A marker on a map is read as "the aircraft is there now",
so a stale one has to be visibly different.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from geoalchemy2.shape import to_shape

from app.api.deps import DbSession, Services, ViewerPrincipal
from app.core.enums import TelemetryStatus
from app.schemas.mission import MapResponse

router = APIRouter(tags=["map"])


@router.get("/missions/{mission_id}/map", response_model=MapResponse)
async def mission_map(
    mission_id: uuid.UUID,
    _: ViewerPrincipal,
    services: Services,
    session: DbSession,
    trail_points: Annotated[int, Query(ge=0, le=2000)] = 300,
) -> MapResponse:
    mission = await services.missions.get(session, mission_id)
    await services.geofence.load_for_mission(session, mission_id)

    launch_point: dict[str, Any] | None = None
    if mission.launch_point is not None:
        shape = to_shape(mission.launch_point)
        launch_point = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [shape.x, shape.y]},
            "properties": {
                "name": "Launch Point",
                "altitude_amsl_m": mission.launch_altitude_amsl_m,
            },
        }

    search_area: dict[str, Any] | None = None
    if mission.search_area is not None:
        shape = to_shape(mission.search_area)
        search_area = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [list(map(list, shape.exterior.coords))],
            },
            "properties": {"name": "Search Area"},
        }

    drones, trails, stale = _live_drone_features(services, str(mission_id), trail_points)

    return MapResponse(
        mission_id=mission_id,
        generated_at=datetime.now(UTC),
        launch_point=launch_point,
        search_area=search_area,
        geofences=services.geofence.geojson(mission_id),
        sectors=await services.search.sectors_geojson(session, mission_id),
        survivors=await services.survivors.geojson(session, mission_id),
        delivery_routes=await services.deliveries.routes_geojson(session, mission_id),
        drones={**drones, "stale_drones": stale},
        trails=trails,
    )


@router.get("/map/fleet")
async def fleet_map(
    _: ViewerPrincipal,
    services: Services,
    trail_points: Annotated[int, Query(ge=0, le=2000)] = 300,
) -> dict:
    """Live positions for every aircraft, mission or not."""
    drones, trails, stale = _live_drone_features(services, None, trail_points)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "drones": drones,
        "trails": trails,
        "stale_drones": stale,
    }


def _live_drone_features(
    services: Any, mission_id: str | None, trail_points: int
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Build drone markers from live telemetry only."""
    policy = services.fleet.policy
    features: list[dict[str, Any]] = []
    trail_features: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []

    for state in services.fleet.states.values():
        if mission_id is not None and state.mission_id != mission_id:
            continue

        status = policy.status("position", state.position)
        position = state.position.value

        if status is TelemetryStatus.FRESH and position is not None:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [position.longitude, position.latitude],
                    },
                    "properties": {
                        "drone_id": state.drone_id,
                        "role": str(state.role),
                        "connection_state": str(state.connection_state),
                        "heading_deg": state.heading.value,
                        "altitude_relative_m": position.relative_altitude_m,
                        "battery_percent": state.battery_percent(),
                        "flight_mode": state.flight_mode.value,
                        "armed": state.armed.value,
                        "sector_code": state.sector_code,
                        "delivery_task_id": state.delivery_task_id,
                        "position_status": str(status),
                        "position_timestamp": (
                            state.position.wall_at.isoformat()
                            if state.position.wall_at
                            else None
                        ),
                    },
                }
            )
        else:
            # Not drawn as a live marker. The last known point is reported
            # separately and explicitly labelled.
            last = state.last_known_position
            stale.append(
                {
                    "drone_id": state.drone_id,
                    "role": str(state.role),
                    "connection_state": str(state.connection_state),
                    "position_status": str(status),
                    "last_known_position": (
                        {
                            "latitude": last.latitude,
                            "longitude": last.longitude,
                            "relative_altitude_m": last.relative_altitude_m,
                            "at": last.at.isoformat(),
                        }
                        if last
                        else None
                    ),
                    "last_contact_at": (
                        state.last_contact_at.isoformat()
                        if state.last_contact_at
                        else None
                    ),
                    "note": (
                        "Position is not live; this is where the aircraft was last "
                        "seen, not where it is now"
                    ),
                }
            )

        if trail_points:
            trail = state.trail_geojson(trail_points)
            if trail["geometry"]["coordinates"]:
                trail_features.append(trail)

    return (
        {"type": "FeatureCollection", "features": features},
        {"type": "FeatureCollection", "features": trail_features},
        stale,
    )
