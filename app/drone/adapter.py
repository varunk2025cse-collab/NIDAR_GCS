"""The controlled interface to a physical aircraft.

Every operation the GCS can perform on an airframe is declared here and
nowhere else. The API layer never touches MAVSDK objects; it calls services,
which call a :class:`DroneAdapter`.

There is exactly one production implementation
(:class:`~app.drone.mavsdk_adapter.MavsdkDroneAdapter`, speaking real MAVLink
to real PX4). Test doubles live under ``tests/`` and are never importable
from application code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.drone.types import (
    Attitude,
    Battery,
    CommandResult,
    GeofencePolygonSpec,
    GpsInfo,
    HealthReport,
    MissionItemSpec,
    MissionProgress,
    Position,
    StatusText,
    VehicleIdentity,
    Velocity,
)


class DroneAdapter(ABC):
    """Abstract transport to one physical vehicle."""

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    async def connect(self, timeout_s: float) -> VehicleIdentity:
        """Open the link and return the identity actually observed.

        Must not return until real vehicle traffic has been seen. Raises on
        timeout; never reports success on the strength of an open socket.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear the link down and release all resources."""

    @abstractmethod
    async def is_connected(self) -> bool:
        """Current link state as reported by the transport itself."""

    # -- telemetry subscriptions ------------------------------------------
    @abstractmethod
    def position(self) -> AsyncIterator[Position]: ...

    @abstractmethod
    def velocity(self) -> AsyncIterator[Velocity]: ...

    @abstractmethod
    def attitude(self) -> AsyncIterator[Attitude]: ...

    @abstractmethod
    def heading(self) -> AsyncIterator[float]: ...

    @abstractmethod
    def battery(self) -> AsyncIterator[Battery]: ...

    @abstractmethod
    def gps_info(self) -> AsyncIterator[GpsInfo]: ...

    @abstractmethod
    def health(self) -> AsyncIterator[HealthReport]: ...

    @abstractmethod
    def armed(self) -> AsyncIterator[bool]: ...

    @abstractmethod
    def in_air(self) -> AsyncIterator[bool]: ...

    @abstractmethod
    def flight_mode(self) -> AsyncIterator[str]: ...

    @abstractmethod
    def landed_state(self) -> AsyncIterator[str]: ...

    @abstractmethod
    def mission_progress(self) -> AsyncIterator[MissionProgress]: ...

    @abstractmethod
    def home_position(self) -> AsyncIterator[Position]: ...

    @abstractmethod
    def status_text(self) -> AsyncIterator[StatusText]: ...

    @abstractmethod
    def connection_state(self) -> AsyncIterator[bool]:
        """Stream of link up/down as seen by the transport."""

    # -- one-shot reads ----------------------------------------------------
    @abstractmethod
    async def get_position(self) -> Position | None: ...

    @abstractmethod
    async def get_battery(self) -> Battery | None: ...

    @abstractmethod
    async def get_health(self) -> HealthReport | None: ...

    @abstractmethod
    async def get_flight_mode(self) -> str | None: ...

    @abstractmethod
    async def get_identity(self) -> VehicleIdentity | None: ...

    # -- commands ----------------------------------------------------------
    @abstractmethod
    async def arm(self) -> CommandResult: ...

    @abstractmethod
    async def disarm(self) -> CommandResult: ...

    @abstractmethod
    async def takeoff(self, altitude_m: float | None = None) -> CommandResult: ...

    @abstractmethod
    async def land(self) -> CommandResult: ...

    @abstractmethod
    async def return_to_launch(self) -> CommandResult: ...

    @abstractmethod
    async def hold(self) -> CommandResult: ...

    @abstractmethod
    async def goto_location(
        self, latitude: float, longitude: float, absolute_altitude_m: float, yaw_deg: float
    ) -> CommandResult: ...

    # -- missions ----------------------------------------------------------
    @abstractmethod
    async def upload_mission(self, items: list[MissionItemSpec]) -> CommandResult: ...

    @abstractmethod
    async def start_mission(self) -> CommandResult: ...

    @abstractmethod
    async def pause_mission(self) -> CommandResult: ...

    @abstractmethod
    async def clear_mission(self) -> CommandResult: ...

    @abstractmethod
    async def set_return_to_launch_after_mission(self, enable: bool) -> CommandResult: ...

    # -- geofence ----------------------------------------------------------
    @abstractmethod
    async def upload_geofence(self, polygons: list[GeofencePolygonSpec]) -> CommandResult: ...

    @abstractmethod
    async def clear_geofence(self) -> CommandResult: ...

    # -- diagnostics -------------------------------------------------------
    @abstractmethod
    async def transport_info(self) -> dict[str, Any]:
        """Transport-level diagnostics for the system-health endpoint."""
