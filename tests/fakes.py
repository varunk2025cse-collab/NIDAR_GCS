"""Test doubles.

These live under ``tests/`` and are never importable from ``app/``. There is a
test in ``test_architecture.py`` that enforces that: a fake must never be able
to reach production code, because a mock that can stand in for an aircraft is
a mock that can be mistaken for one.

The fake here is a *transport* double. It lets the state machines, safety
rules and command lifecycle be exercised deterministically. It is not a flight
simulator and it is not a substitute for bench testing against real PX4
hardware -- see docs/hardware-validation.md for the staged validation that is.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from app.core.config import ConnectionConfig, DroneConfig, TelemetryRatesConfig
from app.drone.adapter import DroneAdapter
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


def healthy_health() -> HealthReport:
    return HealthReport(
        gyrometer_calibration_ok=True,
        accelerometer_calibration_ok=True,
        magnetometer_calibration_ok=True,
        local_position_ok=True,
        global_position_ok=True,
        home_position_ok=True,
        armable=True,
    )


def good_gps(satellites: int = 14) -> GpsInfo:
    return GpsInfo(fix_type=3, fix_type_name="FIX_3D", satellites=satellites)


class FakeDroneAdapter(DroneAdapter):
    """Scriptable stand-in for one aircraft link."""

    def __init__(
        self,
        drone_config: DroneConfig,
        connection_config: ConnectionConfig | None = None,
        telemetry_rates: TelemetryRatesConfig | None = None,
    ) -> None:
        self.config = drone_config
        self.connected = False
        self.connect_should_timeout = False
        self.connect_delay_s = 0.0

        # Scripted vehicle state.
        self.current_position = Position(11.2345, 77.1234, 100.0, 350.0)
        self.current_velocity = Velocity(0.0, 0.0, 0.0)
        self.current_attitude = Attitude(0.0, 0.0, 90.0)
        self.current_heading = 90.0
        self.current_battery = Battery(85.0, 22.2, 5.0, battery_id=0)
        self.current_gps = good_gps()
        self.current_health = healthy_health()
        self.is_armed = False
        self.airborne = False
        self.current_flight_mode = "HOLD"
        self.current_landed_state = "ON_GROUND"
        self.current_mission_progress = MissionProgress(0, 0)
        self.home = Position(11.2345, 77.1234, 0.0, 350.0)

        # Command scripting.
        self.command_log: list[tuple[str, Any]] = []
        self.reject_commands: set[str] = set()
        self.hang_commands: set[str] = set()
        #: When False, a command is acknowledged but the vehicle state is not
        #: updated -- the case that must never be reported as success.
        self.commands_change_state = True

        self.uploaded_mission: list[MissionItemSpec] = []
        self.uploaded_geofence: list[GeofencePolygonSpec] = []

        self._stream_interval_s = 0.01
        self._streaming = True

    # -- lifecycle --------------------------------------------------------
    async def connect(self, timeout_s: float) -> VehicleIdentity:
        if self.connect_delay_s:
            await asyncio.sleep(self.connect_delay_s)
        if self.connect_should_timeout:
            raise TimeoutError(f"{self.config.drone_id}: no vehicle discovered")
        self.connected = True
        return VehicleIdentity(
            system_id=self.config.system_id,
            component_id=self.config.component_id,
            autopilot="MAV_AUTOPILOT_PX4",
            vehicle_type="MAV_TYPE_QUADROTOR",
            hardware_uid=f"FAKE-UID-{self.config.drone_id}",
            firmware_version="1.15.0",
            product_name="Pixhawk 6C",
            observed_at=datetime.now(UTC),
        )

    async def disconnect(self) -> None:
        self.connected = False
        self._streaming = False

    async def is_connected(self) -> bool:
        return self.connected

    def stop_streaming(self) -> None:
        """Simulate telemetry going quiet without the socket closing."""
        self._streaming = False

    # -- streams -----------------------------------------------------------
    async def _stream(self, getter: Any) -> AsyncIterator[Any]:
        while self._streaming and self.connected:
            yield getter()
            await asyncio.sleep(self._stream_interval_s)

    async def position(self) -> AsyncIterator[Position]:
        async for value in self._stream(lambda: self.current_position):
            yield value

    async def velocity(self) -> AsyncIterator[Velocity]:
        async for value in self._stream(lambda: self.current_velocity):
            yield value

    async def attitude(self) -> AsyncIterator[Attitude]:
        async for value in self._stream(lambda: self.current_attitude):
            yield value

    async def heading(self) -> AsyncIterator[float]:
        async for value in self._stream(lambda: self.current_heading):
            yield value

    async def battery(self) -> AsyncIterator[Battery]:
        async for value in self._stream(lambda: self.current_battery):
            yield value

    async def gps_info(self) -> AsyncIterator[GpsInfo]:
        async for value in self._stream(lambda: self.current_gps):
            yield value

    async def health(self) -> AsyncIterator[HealthReport]:
        async for value in self._stream(lambda: self.current_health):
            yield value

    async def armed(self) -> AsyncIterator[bool]:
        async for value in self._stream(lambda: self.is_armed):
            yield value

    async def in_air(self) -> AsyncIterator[bool]:
        async for value in self._stream(lambda: self.airborne):
            yield value

    async def flight_mode(self) -> AsyncIterator[str]:
        async for value in self._stream(lambda: self.current_flight_mode):
            yield value

    async def landed_state(self) -> AsyncIterator[str]:
        async for value in self._stream(lambda: self.current_landed_state):
            yield value

    async def mission_progress(self) -> AsyncIterator[MissionProgress]:
        async for value in self._stream(lambda: self.current_mission_progress):
            yield value

    async def home_position(self) -> AsyncIterator[Position]:
        async for value in self._stream(lambda: self.home):
            yield value

    async def status_text(self) -> AsyncIterator[StatusText]:
        # Quiet by default; tests push text explicitly where they need it.
        while self._streaming and self.connected:
            await asyncio.sleep(3600)
        return
        yield  # pragma: no cover

    async def connection_state(self) -> AsyncIterator[bool]:
        while self._streaming:
            yield self.connected
            await asyncio.sleep(self._stream_interval_s)

    # -- one-shot reads -----------------------------------------------------
    async def get_position(self) -> Position | None:
        return self.current_position if self.connected else None

    async def get_battery(self) -> Battery | None:
        return self.current_battery if self.connected else None

    async def get_health(self) -> HealthReport | None:
        return self.current_health if self.connected else None

    async def get_flight_mode(self) -> str | None:
        return self.current_flight_mode if self.connected else None

    async def get_identity(self) -> VehicleIdentity | None:
        return await self.connect(1.0) if self.connected else None

    # -- commands ------------------------------------------------------------
    async def _command(self, name: str, effect: Any = None) -> CommandResult:
        self.command_log.append((name, datetime.now(UTC)))
        if name in self.hang_commands:
            await asyncio.sleep(3600)
        if name in self.reject_commands:
            return CommandResult.rejected(f"{name}_DENIED", f"{name} refused by vehicle")
        if effect is not None and self.commands_change_state:
            effect()
        return CommandResult.ok(f"{name}_ACCEPTED")

    def _apply_arm(self) -> None:
        self.is_armed = True

    def _apply_disarm(self) -> None:
        self.is_armed = False

    def _apply_takeoff(self) -> None:
        self.is_armed = True
        self.airborne = True
        self.current_flight_mode = "TAKEOFF"
        self.current_landed_state = "IN_AIR"

    def _apply_land(self) -> None:
        self.airborne = False
        self.current_flight_mode = "LAND"
        self.current_landed_state = "ON_GROUND"

    async def arm(self) -> CommandResult:
        return await self._command("ARM", self._apply_arm)

    async def disarm(self) -> CommandResult:
        return await self._command("DISARM", self._apply_disarm)

    async def takeoff(self, altitude_m: float | None = None) -> CommandResult:
        return await self._command("TAKEOFF", self._apply_takeoff)

    async def land(self) -> CommandResult:
        return await self._command("LAND", self._apply_land)

    async def return_to_launch(self) -> CommandResult:
        def effect() -> None:
            self.current_flight_mode = "RETURN_TO_LAUNCH"

        return await self._command("RTL", effect)

    async def hold(self) -> CommandResult:
        def effect() -> None:
            self.current_flight_mode = "HOLD"

        return await self._command("HOLD", effect)

    async def goto_location(
        self, latitude: float, longitude: float, absolute_altitude_m: float, yaw_deg: float
    ) -> CommandResult:
        def effect() -> None:
            self.current_flight_mode = "HOLD"

        self.command_log.append(("GOTO_TARGET", (latitude, longitude, absolute_altitude_m)))
        return await self._command("GOTO", effect)

    async def upload_mission(self, items: list[MissionItemSpec]) -> CommandResult:
        self.uploaded_mission = list(items)

        def effect() -> None:
            self.current_mission_progress = MissionProgress(0, len(items))

        return await self._command("UPLOAD_MISSION", effect)

    async def start_mission(self) -> CommandResult:
        def effect() -> None:
            self.current_flight_mode = "MISSION"

        return await self._command("START_MISSION", effect)

    async def pause_mission(self) -> CommandResult:
        def effect() -> None:
            self.current_flight_mode = "HOLD"

        return await self._command("PAUSE_MISSION", effect)

    async def clear_mission(self) -> CommandResult:
        return await self._command("CLEAR_MISSION")

    async def set_return_to_launch_after_mission(self, enable: bool) -> CommandResult:
        return await self._command("SET_RTL_AFTER_MISSION")

    async def upload_geofence(self, polygons: list[GeofencePolygonSpec]) -> CommandResult:
        self.uploaded_geofence = list(polygons)
        return await self._command("UPLOAD_GEOFENCE")

    async def clear_geofence(self) -> CommandResult:
        self.uploaded_geofence = []
        return await self._command("CLEAR_GEOFENCE")

    async def transport_info(self) -> dict[str, Any]:
        return {
            "transport": "fake",
            "endpoint": self.config.connection_endpoint,
            "link_up": self.connected,
            "warning": "TEST DOUBLE -- not a physical aircraft",
        }


class FakeAdapterRegistry:
    """Factory that hands out (and remembers) one fake per aircraft."""

    def __init__(self) -> None:
        self.adapters: dict[str, FakeDroneAdapter] = {}
        #: Aircraft that are powered off or out of radio range. Every adapter
        #: subsequently built for them fails to connect, so a reconnect loop
        #: cannot quietly bring the link back during a link-loss test.
        self.unreachable: set[str] = set()

    def __call__(
        self,
        drone_config: DroneConfig,
        connection_config: ConnectionConfig,
        telemetry_rates: TelemetryRatesConfig,
    ) -> FakeDroneAdapter:
        adapter = FakeDroneAdapter(drone_config, connection_config, telemetry_rates)
        adapter.connect_should_timeout = drone_config.drone_id in self.unreachable
        self.adapters[drone_config.drone_id] = adapter
        return adapter

    def make_unreachable(self, drone_id: str) -> None:
        """Model the aircraft losing power or flying out of radio range."""
        drone_id = drone_id.upper()
        self.unreachable.add(drone_id)
        if drone_id in self.adapters:
            self.adapters[drone_id].stop_streaming()

    def get(self, drone_id: str) -> FakeDroneAdapter:
        return self.adapters[drone_id.upper()]
