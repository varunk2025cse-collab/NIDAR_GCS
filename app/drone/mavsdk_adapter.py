"""The production :class:`DroneAdapter`: real MAVSDK to real PX4.

This is the only code in the backend that imports MAVSDK. Everything above it
sees the neutral dataclasses in :mod:`app.drone.types`.

Two rules are enforced here:

* A connection is not "connected" until MAVSDK reports a discovered system on
  this endpoint. An open socket is not a vehicle.
* A command returns whatever the flight controller actually said. A MAVSDK
  call that raises is reported as a failure, not swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.config import ConnectionConfig, DroneConfig, TelemetryRatesConfig
from app.core.logging import get_logger
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

if TYPE_CHECKING:  # pragma: no cover
    from mavsdk import System

logger = get_logger(__name__)


class MavsdkUnavailableError(RuntimeError):
    """MAVSDK is not installed in this environment.

    Raised at connect time rather than import time so the rest of the backend
    (and its unit tests) can be exercised on a machine without the MAVSDK
    binaries -- but a real connection attempt fails loudly instead of
    degrading into something fake.
    """


def _require_mavsdk() -> Any:
    try:
        import mavsdk
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MavsdkUnavailableError(
            "The mavsdk package is required to talk to a flight controller. "
            "Install it with: pip install mavsdk"
        ) from exc
    return mavsdk


class MavsdkDroneAdapter(DroneAdapter):
    """One instance per physical aircraft.

    Each aircraft gets its own ``mavsdk_server`` on its own gRPC port and its
    own MAVLink endpoint. That one-to-one binding is what guarantees a command
    meant for D3 can never reach D1.
    """

    def __init__(
        self,
        drone_config: DroneConfig,
        connection_config: ConnectionConfig,
        telemetry_rates: TelemetryRatesConfig | None = None,
    ) -> None:
        self._config = drone_config
        self._connection_config = connection_config
        self._rates = telemetry_rates or TelemetryRatesConfig()
        self._system: System | None = None
        self._identity: VehicleIdentity | None = None
        self._connected = False
        self._log = logger.bind(drone_id=drone_config.drone_id)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def system(self) -> System:
        if self._system is None:
            raise RuntimeError(
                f"{self._config.drone_id}: adapter used before connect() succeeded"
            )
        return self._system

    async def connect(self, timeout_s: float) -> VehicleIdentity:
        mavsdk = _require_mavsdk()

        port = self._config.mavsdk_server_port or 50051
        self._system = mavsdk.System(
            mavsdk_server_address=self._config.mavsdk_server_address,
            port=port,
            sysid=self._connection_config.gcs_system_id,
            compid=self._connection_config.gcs_component_id,
        )
        self._log.info(
            "mavsdk_connect_start",
            endpoint=self._config.connection_endpoint,
            grpc_port=port,
            server_address=self._config.mavsdk_server_address,
        )
        await self._system.connect(system_address=self._config.connection_endpoint)

        # Wait for MAVSDK to actually discover a vehicle. Without this the
        # object exists but no aircraft has been heard from.
        try:
            await asyncio.wait_for(self._await_discovery(), timeout=timeout_s)
        except TimeoutError as exc:
            await self.disconnect()
            raise TimeoutError(
                f"{self._config.drone_id}: no vehicle discovered on "
                f"{self._config.connection_endpoint} within {timeout_s}s"
            ) from exc

        self._connected = True
        identity = await self._read_identity()
        self._identity = identity
        await self._apply_telemetry_rates()
        self._log.info(
            "mavsdk_connected",
            hardware_uid=identity.hardware_uid,
            firmware_version=identity.firmware_version,
            product=identity.product_name,
        )
        return identity

    async def _await_discovery(self) -> None:
        async for state in self.system.core.connection_state():
            if state.is_connected:
                return

    async def _read_identity(self) -> VehicleIdentity:
        """Ask the autopilot who it is.

        Each field is optional: an autopilot that does not answer one request
        yields ``None`` for that field rather than a placeholder.
        """
        hardware_uid: str | None = None
        firmware_version: str | None = None
        firmware_vendor: str | None = None
        product_name: str | None = None
        raw: dict[str, Any] = {}

        try:
            ident = await self.system.info.get_identification()
            hardware_uid = getattr(ident, "hardware_uid", None) or None
            raw["legacy_uid"] = getattr(ident, "legacy_uid", None)
        except Exception as exc:
            self._log.warning("identity_hardware_uid_unavailable", error=str(exc))

        try:
            version = await self.system.info.get_version()
            firmware_version = (
                f"{version.flight_sw_major}.{version.flight_sw_minor}.{version.flight_sw_patch}"
            )
            firmware_vendor = (
                f"{version.flight_sw_vendor_major}.{version.flight_sw_vendor_minor}"
                f".{version.flight_sw_vendor_patch}"
            )
            raw["flight_sw_git_hash"] = version.flight_sw_git_hash
            raw["os_sw_version"] = (
                f"{version.os_sw_major}.{version.os_sw_minor}.{version.os_sw_patch}"
            )
        except Exception as exc:
            self._log.warning("identity_version_unavailable", error=str(exc))

        try:
            product = await self.system.info.get_product()
            product_name = product.product_name or None
            raw["vendor_name"] = product.vendor_name
            raw["vendor_id"] = product.vendor_id
            raw["product_id"] = product.product_id
        except Exception as exc:
            self._log.warning("identity_product_unavailable", error=str(exc))

        return VehicleIdentity(
            system_id=self._config.system_id,
            component_id=self._config.component_id,
            hardware_uid=hardware_uid,
            firmware_version=firmware_version,
            firmware_vendor=firmware_vendor,
            product_name=product_name,
            observed_at=datetime.now(UTC),
            raw=raw,
        )

    async def _apply_telemetry_rates(self) -> None:
        """Request the configured stream rates.

        On a bandwidth-limited RF link some of these will be refused. That is
        logged and tolerated; it is not a connection failure.
        """
        requests = [
            ("position", self._rates.position_hz, self.system.telemetry.set_rate_position),
            ("velocity", self._rates.velocity_hz, self.system.telemetry.set_rate_velocity_ned),
            ("attitude", self._rates.attitude_hz, self.system.telemetry.set_rate_attitude_euler),
            ("battery", self._rates.battery_hz, self.system.telemetry.set_rate_battery),
            ("gps_info", self._rates.gps_info_hz, self.system.telemetry.set_rate_gps_info),
            ("health", self._rates.health_hz, self.system.telemetry.set_rate_health),
            (
                "landed_state",
                self._rates.landed_state_hz,
                self.system.telemetry.set_rate_landed_state,
            ),
            ("home", self._rates.home_hz, self.system.telemetry.set_rate_home),
        ]
        for name, hz, setter in requests:
            if hz <= 0:
                continue
            try:
                await setter(hz)
            except Exception as exc:
                if not self._rates.tolerate_rate_failures:
                    raise
                self._log.warning("telemetry_rate_rejected", stream=name, hz=hz, error=str(exc))

    async def disconnect(self) -> None:
        self._connected = False
        system, self._system = self._system, None
        if system is None:
            return
        try:
            # Releases the gRPC channel and stops a locally spawned
            # mavsdk_server, if this instance started one.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(system.__del__)  # type: ignore[misc]
        finally:
            self._log.info("mavsdk_disconnected", endpoint=self._config.connection_endpoint)

    async def is_connected(self) -> bool:
        if self._system is None:
            return False
        try:
            async for state in self._system.core.connection_state():
                return bool(state.is_connected)
        except Exception:
            return False
        return False

    # ------------------------------------------------------------------
    # telemetry streams
    # ------------------------------------------------------------------
    async def position(self) -> AsyncIterator[Position]:
        async for p in self.system.telemetry.position():
            yield Position(
                latitude=p.latitude_deg,
                longitude=p.longitude_deg,
                relative_altitude_m=p.relative_altitude_m,
                absolute_altitude_m=p.absolute_altitude_m,
            )

    async def velocity(self) -> AsyncIterator[Velocity]:
        async for v in self.system.telemetry.velocity_ned():
            yield Velocity(north_mps=v.north_m_s, east_mps=v.east_m_s, down_mps=v.down_m_s)

    async def attitude(self) -> AsyncIterator[Attitude]:
        async for a in self.system.telemetry.attitude_euler():
            yield Attitude(roll_deg=a.roll_deg, pitch_deg=a.pitch_deg, yaw_deg=a.yaw_deg)

    async def heading(self) -> AsyncIterator[float]:
        async for h in self.system.telemetry.heading():
            yield float(h.heading_deg)

    async def battery(self) -> AsyncIterator[Battery]:
        async for b in self.system.telemetry.battery():
            yield _battery_from_mavsdk(b)

    async def gps_info(self) -> AsyncIterator[GpsInfo]:
        async for g in self.system.telemetry.gps_info():
            yield GpsInfo(
                fix_type=int(getattr(g.fix_type, "value", g.fix_type)),
                fix_type_name=str(getattr(g.fix_type, "name", g.fix_type)),
                satellites=int(g.num_satellites),
            )

    async def health(self) -> AsyncIterator[HealthReport]:
        async for h in self.system.telemetry.health():
            yield _health_from_mavsdk(h)

    async def armed(self) -> AsyncIterator[bool]:
        async for value in self.system.telemetry.armed():
            yield bool(value)

    async def in_air(self) -> AsyncIterator[bool]:
        async for value in self.system.telemetry.in_air():
            yield bool(value)

    async def flight_mode(self) -> AsyncIterator[str]:
        async for mode in self.system.telemetry.flight_mode():
            yield str(getattr(mode, "name", mode))

    async def landed_state(self) -> AsyncIterator[str]:
        async for state in self.system.telemetry.landed_state():
            yield str(getattr(state, "name", state))

    async def mission_progress(self) -> AsyncIterator[MissionProgress]:
        async for progress in self.system.mission.mission_progress():
            yield MissionProgress(current=int(progress.current), total=int(progress.total))

    async def home_position(self) -> AsyncIterator[Position]:
        async for p in self.system.telemetry.home():
            yield Position(
                latitude=p.latitude_deg,
                longitude=p.longitude_deg,
                relative_altitude_m=p.relative_altitude_m,
                absolute_altitude_m=p.absolute_altitude_m,
            )

    async def status_text(self) -> AsyncIterator[StatusText]:
        async for s in self.system.telemetry.status_text():
            yield StatusText(severity=str(getattr(s.type, "name", s.type)), text=s.text)

    async def connection_state(self) -> AsyncIterator[bool]:
        async for state in self.system.core.connection_state():
            self._connected = bool(state.is_connected)
            yield self._connected

    # ------------------------------------------------------------------
    # one-shot reads
    # ------------------------------------------------------------------
    async def _first(self, stream: AsyncIterator[Any], timeout_s: float = 3.0) -> Any | None:
        """Take one value from a stream, or ``None`` if nothing arrives.

        ``None`` is a real answer here: it means the aircraft is not currently
        producing that telemetry.
        """
        try:
            return await asyncio.wait_for(anext(stream), timeout=timeout_s)  # type: ignore[arg-type]
        except (TimeoutError, StopAsyncIteration):
            return None
        except Exception as exc:
            self._log.warning("telemetry_read_failed", error=str(exc))
            return None
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()  # type: ignore[attr-defined]

    async def get_position(self) -> Position | None:
        return await self._first(self.position())

    async def get_battery(self) -> Battery | None:
        return await self._first(self.battery())

    async def get_health(self) -> HealthReport | None:
        return await self._first(self.health())

    async def get_flight_mode(self) -> str | None:
        return await self._first(self.flight_mode())

    async def get_identity(self) -> VehicleIdentity | None:
        return self._identity

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def _run_command(self, name: str, coro: Any) -> CommandResult:
        """Execute one MAVSDK call and report exactly what came back.

        A MAVSDK plugin error carries the flight controller result code; that
        code is preserved so the operator sees why PX4 refused, not a generic
        failure.
        """
        try:
            await coro
        except Exception as exc:
            code, detail = _describe_mavsdk_error(exc)
            self._log.warning("command_rejected", command=name, code=code, detail=detail)
            return CommandResult.rejected(code, detail, exception=type(exc).__name__)
        self._log.info("command_acknowledged", command=name)
        return CommandResult.ok(f"{name}_ACCEPTED")

    async def arm(self) -> CommandResult:
        return await self._run_command("ARM", self.system.action.arm())

    async def disarm(self) -> CommandResult:
        return await self._run_command("DISARM", self.system.action.disarm())

    async def takeoff(self, altitude_m: float | None = None) -> CommandResult:
        if altitude_m is not None:
            result = await self._run_command(
                "SET_TAKEOFF_ALTITUDE", self.system.action.set_takeoff_altitude(float(altitude_m))
            )
            if not result.success:
                return result
        return await self._run_command("TAKEOFF", self.system.action.takeoff())

    async def land(self) -> CommandResult:
        return await self._run_command("LAND", self.system.action.land())

    async def return_to_launch(self) -> CommandResult:
        return await self._run_command("RTL", self.system.action.return_to_launch())

    async def hold(self) -> CommandResult:
        return await self._run_command("HOLD", self.system.action.hold())

    async def goto_location(
        self, latitude: float, longitude: float, absolute_altitude_m: float, yaw_deg: float
    ) -> CommandResult:
        return await self._run_command(
            "GOTO",
            self.system.action.goto_location(
                float(latitude), float(longitude), float(absolute_altitude_m), float(yaw_deg)
            ),
        )

    # ------------------------------------------------------------------
    # missions
    # ------------------------------------------------------------------
    async def upload_mission(self, items: list[MissionItemSpec]) -> CommandResult:
        mavsdk = _require_mavsdk()
        from mavsdk.mission import MissionItem, MissionPlan

        if not items:
            return CommandResult.rejected("EMPTY_MISSION", "Refusing to upload an empty plan")

        plan_items = [
            MissionItem(
                latitude_deg=item.latitude,
                longitude_deg=item.longitude,
                relative_altitude_m=float(item.relative_altitude_m),
                speed_m_s=float(item.speed_mps) if item.speed_mps is not None else float("nan"),
                is_fly_through=item.is_fly_through,
                gimbal_pitch_deg=float("nan"),
                gimbal_yaw_deg=float("nan"),
                camera_action=MissionItem.CameraAction.NONE,
                loiter_time_s=(
                    float(item.loiter_time_s) if item.loiter_time_s is not None else float("nan")
                ),
                camera_photo_interval_s=float("nan"),
                acceptance_radius_m=(
                    float(item.acceptance_radius_m)
                    if item.acceptance_radius_m is not None
                    else float("nan")
                ),
                yaw_deg=float(item.yaw_deg) if item.yaw_deg is not None else float("nan"),
                camera_photo_distance_m=float("nan"),
                vehicle_action=MissionItem.VehicleAction.NONE,
            )
            for item in items
        ]
        del mavsdk
        result = await self._run_command(
            "UPLOAD_MISSION", self.system.mission.upload_mission(MissionPlan(plan_items))
        )
        if result.success:
            return CommandResult.ok("UPLOAD_MISSION_ACCEPTED", raw_item_count=len(plan_items))
        return result

    async def start_mission(self) -> CommandResult:
        return await self._run_command("START_MISSION", self.system.mission.start_mission())

    async def pause_mission(self) -> CommandResult:
        return await self._run_command("PAUSE_MISSION", self.system.mission.pause_mission())

    async def clear_mission(self) -> CommandResult:
        return await self._run_command("CLEAR_MISSION", self.system.mission.clear_mission())

    async def set_return_to_launch_after_mission(self, enable: bool) -> CommandResult:
        return await self._run_command(
            "SET_RTL_AFTER_MISSION",
            self.system.mission.set_return_to_launch_after_mission(bool(enable)),
        )

    # ------------------------------------------------------------------
    # geofence
    # ------------------------------------------------------------------
    async def upload_geofence(self, polygons: list[GeofencePolygonSpec]) -> CommandResult:
        from mavsdk.geofence import GeofenceData, Point, Polygon

        if not polygons:
            return CommandResult.rejected("EMPTY_GEOFENCE", "No polygons supplied")

        mav_polygons = [
            Polygon(
                [Point(lat, lon) for lat, lon in spec.points],
                Polygon.FenceType.INCLUSION if spec.inclusion else Polygon.FenceType.EXCLUSION,
            )
            for spec in polygons
        ]
        return await self._run_command(
            "UPLOAD_GEOFENCE",
            self.system.geofence.upload_geofence(GeofenceData(mav_polygons, [])),
        )

    async def clear_geofence(self) -> CommandResult:
        return await self._run_command("CLEAR_GEOFENCE", self.system.geofence.clear_geofence())

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    async def transport_info(self) -> dict[str, Any]:
        return {
            "transport": "mavsdk",
            "endpoint": self._config.connection_endpoint,
            "grpc_port": self._config.mavsdk_server_port or 50051,
            "grpc_address": self._config.mavsdk_server_address,
            "gcs_system_id": self._connection_config.gcs_system_id,
            "expected_system_id": self._config.system_id,
            "attached": self._system is not None,
            "link_up": self._connected,
        }


# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------
def _clean_float(value: Any) -> float | None:
    """Turn a MAVSDK NaN sentinel into an explicit absence of data."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check without importing math


def _battery_from_mavsdk(b: Any) -> Battery:
    # MAVSDK v2 reports remaining_percent in the range 0-100.
    return Battery(
        remaining_percent=_clean_float(getattr(b, "remaining_percent", None)),
        voltage_v=_clean_float(getattr(b, "voltage_v", None)),
        current_a=_clean_float(getattr(b, "current_battery_a", None)),
        battery_id=int(getattr(b, "id", 0) or 0),
        temperature_c=_clean_float(getattr(b, "temperature_degc", None)),
        capacity_consumed_ah=_clean_float(getattr(b, "capacity_consumed_ah", None)),
    )


def _health_from_mavsdk(h: Any) -> HealthReport:
    return HealthReport(
        gyrometer_calibration_ok=bool(h.is_gyrometer_calibration_ok),
        accelerometer_calibration_ok=bool(h.is_accelerometer_calibration_ok),
        magnetometer_calibration_ok=bool(h.is_magnetometer_calibration_ok),
        local_position_ok=bool(h.is_local_position_ok),
        global_position_ok=bool(h.is_global_position_ok),
        home_position_ok=bool(h.is_home_position_ok),
        armable=bool(h.is_armable),
    )


def _describe_mavsdk_error(exc: Exception) -> tuple[str, str]:
    """Extract the flight controller result code from a MAVSDK exception."""
    result = getattr(exc, "_result", None)
    if result is not None:
        code = getattr(result, "result", None)
        detail = getattr(result, "result_str", None)
        return (
            str(getattr(code, "name", code) or type(exc).__name__),
            str(detail or exc) or type(exc).__name__,
        )
    return type(exc).__name__, str(exc) or type(exc).__name__
