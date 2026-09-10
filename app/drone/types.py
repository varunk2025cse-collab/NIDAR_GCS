"""Transport-neutral value types that cross the adapter boundary.

Nothing above :mod:`app.drone` ever imports MAVSDK. The adapter converts
MAVSDK/MAVLink structures into these dataclasses, which means the service
layer, the API and the tests all speak one vocabulary and a change of
transport library stays contained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TelemetryStream(StrEnum):
    """Named telemetry streams a drone adapter can expose."""

    POSITION = "position"
    VELOCITY = "velocity"
    ATTITUDE = "attitude"
    HEADING = "heading"
    BATTERY = "battery"
    GPS = "gps"
    HEALTH = "health"
    ARMED = "armed"
    IN_AIR = "in_air"
    FLIGHT_MODE = "flight_mode"
    LANDED_STATE = "landed_state"
    MISSION_PROGRESS = "mission_progress"
    HOME = "home"
    STATUS_TEXT = "status_text"


@dataclass(frozen=True, slots=True)
class Position:
    latitude: float
    longitude: float
    relative_altitude_m: float | None
    absolute_altitude_m: float | None


@dataclass(frozen=True, slots=True)
class Velocity:
    north_mps: float
    east_mps: float
    down_mps: float

    @property
    def ground_speed_mps(self) -> float:
        return (self.north_mps**2 + self.east_mps**2) ** 0.5

    @property
    def vertical_speed_mps(self) -> float:
        """Positive up, which is what an operator expects to read."""
        return -self.down_mps


@dataclass(frozen=True, slots=True)
class Attitude:
    roll_deg: float
    pitch_deg: float
    yaw_deg: float


@dataclass(frozen=True, slots=True)
class Battery:
    """Battery telemetry exactly as reported.

    ``remaining_percent`` is 0-100. ``None`` means the autopilot did not
    report it -- it is never defaulted to a number.
    """

    remaining_percent: float | None
    voltage_v: float | None
    current_a: float | None
    battery_id: int | None = None
    temperature_c: float | None = None
    capacity_consumed_ah: float | None = None


@dataclass(frozen=True, slots=True)
class GpsInfo:
    fix_type: int
    fix_type_name: str
    satellites: int
    horizontal_accuracy_m: float | None = None
    vertical_accuracy_m: float | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    gyrometer_calibration_ok: bool
    accelerometer_calibration_ok: bool
    magnetometer_calibration_ok: bool
    local_position_ok: bool
    global_position_ok: bool
    home_position_ok: bool
    armable: bool

    @property
    def all_ok(self) -> bool:
        return all(
            (
                self.gyrometer_calibration_ok,
                self.accelerometer_calibration_ok,
                self.magnetometer_calibration_ok,
                self.local_position_ok,
                self.global_position_ok,
                self.home_position_ok,
                self.armable,
            )
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "gyrometer_calibration_ok": self.gyrometer_calibration_ok,
            "accelerometer_calibration_ok": self.accelerometer_calibration_ok,
            "magnetometer_calibration_ok": self.magnetometer_calibration_ok,
            "local_position_ok": self.local_position_ok,
            "global_position_ok": self.global_position_ok,
            "home_position_ok": self.home_position_ok,
            "armable": self.armable,
            "all_ok": self.all_ok,
        }


@dataclass(frozen=True, slots=True)
class MissionProgress:
    current: int
    total: int

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current / self.total))


@dataclass(frozen=True, slots=True)
class StatusText:
    severity: str
    text: str


@dataclass(frozen=True, slots=True)
class VehicleIdentity:
    """Identity as observed on the wire, not as configured.

    ``system_id``/``component_id`` come from an actual MAVLink heartbeat;
    ``hardware_uid`` from the autopilot itself.
    """

    system_id: int
    component_id: int
    autopilot: str | None = None
    vehicle_type: str | None = None
    hardware_uid: str | None = None
    firmware_version: str | None = None
    firmware_vendor: str | None = None
    product_name: str | None = None
    observed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MissionItemSpec:
    """One planned mission item, in adapter-neutral terms."""

    latitude: float
    longitude: float
    relative_altitude_m: float
    speed_mps: float | None = None
    is_fly_through: bool = True
    acceptance_radius_m: float | None = None
    yaw_deg: float | None = None
    loiter_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class GeofencePolygonSpec:
    #: [(lat, lon), ...] in order; the ring is closed implicitly.
    points: list[tuple[float, float]]
    inclusion: bool = True


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one adapter call.

    ``acknowledged`` means the flight controller returned a result for the
    command. It does not mean the aircraft has done anything yet -- the
    caller verifies that separately by watching telemetry.
    """

    acknowledged: bool
    success: bool
    result_code: str
    detail: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, code: str = "OK", detail: str | None = None, **raw: Any) -> CommandResult:
        return cls(acknowledged=True, success=True, result_code=code, detail=detail, raw=raw)

    @classmethod
    def rejected(cls, code: str, detail: str | None = None, **raw: Any) -> CommandResult:
        return cls(acknowledged=True, success=False, result_code=code, detail=detail, raw=raw)

    @classmethod
    def no_ack(cls, code: str = "NO_ACKNOWLEDGEMENT", detail: str | None = None) -> CommandResult:
        return cls(acknowledged=False, success=False, result_code=code, detail=detail)
