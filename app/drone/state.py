"""Live in-memory state of one physical aircraft.

This is the single authority for "what is D1 doing right now". Every field
carries the instant it arrived, so the API and the WebSocket can never present
a value as live when it is not.

The object is mutated only by the owning :class:`~app.drone.connection.DroneConnection`
task; readers take consistent snapshots via :meth:`snapshot`.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import DroneConfig, DroneRole
from app.core.enums import ConnectionState, GeofenceStatus, TelemetryStatus
from app.core.freshness import FreshnessPolicy, TimedValue
from app.drone.types import (
    Attitude,
    Battery,
    GpsInfo,
    HealthReport,
    MissionProgress,
    Position,
    StatusText,
    VehicleIdentity,
    Velocity,
)

#: Positions retained for the map trail. At 4 Hz this is ~5 minutes of flight.
TRAIL_MAXLEN = 1200
STATUS_TEXT_MAXLEN = 50


@dataclass(slots=True)
class TrailPoint:
    latitude: float
    longitude: float
    relative_altitude_m: float | None
    at: datetime


@dataclass(slots=True)
class DroneState:
    """Consolidated live state for one airframe."""

    drone_id: str
    role: DroneRole
    system_id: int
    component_id: int
    endpoint: str
    name: str | None = None

    # --- link -------------------------------------------------------------
    connection_state: ConnectionState = ConnectionState.DISCOVERING
    connection_state_since: datetime = field(default_factory=lambda: datetime.now(UTC))
    connected_since: datetime | None = None
    #: Monotonic instant of the most recent evidence the aircraft is alive.
    last_contact_monotonic: float | None = None
    last_contact_at: datetime | None = None
    #: Preserved across a disconnect so the operator can see where contact
    #: was lost. Never presented as the current position.
    last_known_position: TrailPoint | None = None
    disconnected_at: datetime | None = None
    last_error: str | None = None
    reconnect_attempts: int = 0
    identity: VehicleIdentity | None = None
    identity_verified: bool = False
    connection_uuid: str | None = None

    # --- telemetry --------------------------------------------------------
    position: TimedValue[Position] = field(default_factory=TimedValue)
    velocity: TimedValue[Velocity] = field(default_factory=TimedValue)
    attitude: TimedValue[Attitude] = field(default_factory=TimedValue)
    heading: TimedValue[float] = field(default_factory=TimedValue)
    battery: TimedValue[Battery] = field(default_factory=TimedValue)
    gps: TimedValue[GpsInfo] = field(default_factory=TimedValue)
    health: TimedValue[HealthReport] = field(default_factory=TimedValue)
    armed: TimedValue[bool] = field(default_factory=TimedValue)
    in_air: TimedValue[bool] = field(default_factory=TimedValue)
    flight_mode: TimedValue[str] = field(default_factory=TimedValue)
    landed_state: TimedValue[str] = field(default_factory=TimedValue)
    mission_progress: TimedValue[MissionProgress] = field(default_factory=TimedValue)
    home: TimedValue[Position] = field(default_factory=TimedValue)

    status_texts: deque[tuple[datetime, StatusText]] = field(
        default_factory=lambda: deque(maxlen=STATUS_TEXT_MAXLEN)
    )
    trail: deque[TrailPoint] = field(default_factory=lambda: deque(maxlen=TRAIL_MAXLEN))

    # --- supervision ------------------------------------------------------
    #: Set by the geofence service from real positions.
    geofence_status: GeofenceStatus = GeofenceStatus.UNKNOWN
    geofence_margin_m: float | None = None

    # --- assignment -------------------------------------------------------
    mission_id: str | None = None
    sector_id: str | None = None
    sector_code: str | None = None
    delivery_task_id: str | None = None
    #: Set while a command is in flight, so a second command is refused
    #: rather than queued behind it.
    busy_with_command: str | None = None

    # ------------------------------------------------------------------
    # mutation helpers (called only by the owning connection task)
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: DroneConfig) -> DroneState:
        return cls(
            drone_id=config.drone_id,
            role=config.role,
            system_id=config.system_id,
            component_id=config.component_id,
            endpoint=config.connection_endpoint,
            name=config.name,
        )

    def mark_contact(self) -> None:
        """Record that real traffic arrived from the aircraft."""
        self.last_contact_monotonic = time.monotonic()
        self.last_contact_at = datetime.now(UTC)

    def set_connection_state(self, state: ConnectionState, error: str | None = None) -> bool:
        """Returns True when the state actually changed."""
        if state is self.connection_state:
            if error:
                self.last_error = error
            return False
        previous = self.connection_state
        self.connection_state = state
        self.connection_state_since = datetime.now(UTC)
        if error:
            self.last_error = error
        if state is ConnectionState.CONNECTED and previous is not ConnectionState.CONNECTED:
            self.connected_since = datetime.now(UTC)
            self.disconnected_at = None
            self.reconnect_attempts = 0
        if state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            self.connected_since = None
            self.disconnected_at = datetime.now(UTC)
            self.on_link_lost()
        return True

    def on_link_lost(self) -> None:
        """Drop live telemetry values on link loss.

        The last position is kept separately as ``last_known_position`` and is
        always labelled as such. Nothing keeps reporting a value the aircraft
        is no longer sending.
        """
        pos = self.position.value
        if pos is not None and self.position.wall_at is not None:
            self.last_known_position = TrailPoint(
                latitude=pos.latitude,
                longitude=pos.longitude,
                relative_altitude_m=pos.relative_altitude_m,
                at=self.position.wall_at,
            )
        for value in (
            self.position,
            self.velocity,
            self.attitude,
            self.heading,
            self.battery,
            self.gps,
            self.health,
            self.armed,
            self.in_air,
            self.flight_mode,
            self.landed_state,
            self.mission_progress,
        ):
            value.clear()
        self.geofence_status = GeofenceStatus.UNKNOWN
        self.geofence_margin_m = None
        self.identity_verified = False

    def record_position(self, position: Position) -> None:
        self.position.set(position)
        self.mark_contact()
        point = TrailPoint(
            latitude=position.latitude,
            longitude=position.longitude,
            relative_altitude_m=position.relative_altitude_m,
            at=datetime.now(UTC),
        )
        self.trail.append(point)
        self.last_known_position = point

    def record_status_text(self, status: StatusText) -> None:
        self.status_texts.append((datetime.now(UTC), status))
        self.mark_contact()

    # ------------------------------------------------------------------
    # derived views
    # ------------------------------------------------------------------
    def contact_age_s(self, now: float | None = None) -> float | None:
        if self.last_contact_monotonic is None:
            return None
        reference = now if now is not None else time.monotonic()
        return max(0.0, reference - self.last_contact_monotonic)

    @property
    def is_connected(self) -> bool:
        return self.connection_state in (ConnectionState.CONNECTED, ConnectionState.DEGRADED)

    @property
    def is_commandable(self) -> bool:
        """Only a fully CONNECTED aircraft accepts commands.

        DEGRADED means telemetry has gone quiet; sending a flight command over
        a link we cannot confirm is exactly the situation to refuse.
        """
        return self.connection_state is ConnectionState.CONNECTED

    def battery_percent(self) -> float | None:
        b = self.battery.value
        return b.remaining_percent if b else None

    def altitude_relative_m(self) -> float | None:
        p = self.position.value
        return p.relative_altitude_m if p else None

    def ground_speed_mps(self) -> float | None:
        v = self.velocity.value
        return v.ground_speed_mps if v else None

    def snapshot(self, policy: FreshnessPolicy, now: float | None = None) -> dict[str, Any]:
        """Full wire representation, freshness included on every field."""
        now = now if now is not None else time.monotonic()
        pos = self.position.value
        vel = self.velocity.value
        att = self.attitude.value
        bat = self.battery.value
        gps = self.gps.value
        hlt = self.health.value
        prog = self.mission_progress.value
        home = self.home.value

        return {
            "drone_id": self.drone_id,
            "name": self.name,
            "role": str(self.role),
            "system_id": self.system_id,
            "component_id": self.component_id,
            "endpoint": self.endpoint,
            "connection": {
                "state": str(self.connection_state),
                "since": self.connection_state_since.isoformat(),
                "connected_since": (
                    self.connected_since.isoformat() if self.connected_since else None
                ),
                "disconnected_at": (
                    self.disconnected_at.isoformat() if self.disconnected_at else None
                ),
                "last_contact_at": (
                    self.last_contact_at.isoformat() if self.last_contact_at else None
                ),
                "last_contact_age_s": (
                    round(age, 2) if (age := self.contact_age_s(now)) is not None else None
                ),
                "identity_verified": self.identity_verified,
                "reconnect_attempts": self.reconnect_attempts,
                "last_error": self.last_error,
            },
            "identity": (
                {
                    "system_id": self.identity.system_id,
                    "component_id": self.identity.component_id,
                    "autopilot": self.identity.autopilot,
                    "vehicle_type": self.identity.vehicle_type,
                    "hardware_uid": self.identity.hardware_uid,
                    "firmware_version": self.identity.firmware_version,
                    "firmware_vendor": self.identity.firmware_vendor,
                    "product_name": self.identity.product_name,
                }
                if self.identity
                else None
            ),
            "telemetry": {
                "position": _render_struct(
                    policy,
                    "position",
                    self.position,
                    now,
                    lambda: {
                        "latitude": pos.latitude,
                        "longitude": pos.longitude,
                        "relative_altitude_m": pos.relative_altitude_m,
                        "absolute_altitude_m": pos.absolute_altitude_m,
                    }
                    if pos
                    else None,
                ),
                "velocity": _render_struct(
                    policy,
                    "velocity",
                    self.velocity,
                    now,
                    lambda: {
                        "north_mps": vel.north_mps,
                        "east_mps": vel.east_mps,
                        "down_mps": vel.down_mps,
                        "ground_speed_mps": round(vel.ground_speed_mps, 3),
                        "vertical_speed_mps": round(vel.vertical_speed_mps, 3),
                    }
                    if vel
                    else None,
                ),
                "attitude": _render_struct(
                    policy,
                    "attitude",
                    self.attitude,
                    now,
                    lambda: {
                        "roll_deg": att.roll_deg,
                        "pitch_deg": att.pitch_deg,
                        "yaw_deg": att.yaw_deg,
                    }
                    if att
                    else None,
                ),
                "heading": policy.render("attitude", self.heading, now),
                "battery": _render_struct(
                    policy,
                    "battery",
                    self.battery,
                    now,
                    lambda: {
                        "remaining_percent": bat.remaining_percent,
                        "voltage_v": bat.voltage_v,
                        "current_a": bat.current_a,
                        "temperature_c": bat.temperature_c,
                        "battery_id": bat.battery_id,
                    }
                    if bat
                    else None,
                ),
                "gps": _render_struct(
                    policy,
                    "gps",
                    self.gps,
                    now,
                    lambda: {
                        "fix_type": gps.fix_type,
                        "fix_type_name": gps.fix_type_name,
                        "satellites": gps.satellites,
                        "horizontal_accuracy_m": gps.horizontal_accuracy_m,
                        "vertical_accuracy_m": gps.vertical_accuracy_m,
                    }
                    if gps
                    else None,
                ),
                "health": _render_struct(
                    policy,
                    "health",
                    self.health,
                    now,
                    lambda: hlt.as_dict() if hlt else None,
                ),
                "armed": policy.render("flight_mode", self.armed, now),
                "in_air": policy.render("flight_mode", self.in_air, now),
                "flight_mode": policy.render("flight_mode", self.flight_mode, now),
                "landed_state": policy.render("flight_mode", self.landed_state, now),
                "mission_progress": _render_struct(
                    policy,
                    "flight_mode",
                    self.mission_progress,
                    now,
                    lambda: {
                        "current": prog.current,
                        "total": prog.total,
                        "fraction": round(prog.fraction, 4),
                    }
                    if prog
                    else None,
                ),
                "home": _render_struct(
                    policy,
                    "position",
                    self.home,
                    now,
                    lambda: {
                        "latitude": home.latitude,
                        "longitude": home.longitude,
                        "absolute_altitude_m": home.absolute_altitude_m,
                    }
                    if home
                    else None,
                ),
            },
            "last_known_position": (
                {
                    "latitude": self.last_known_position.latitude,
                    "longitude": self.last_known_position.longitude,
                    "relative_altitude_m": self.last_known_position.relative_altitude_m,
                    "at": self.last_known_position.at.isoformat(),
                    "is_live": self.position.status(*policy.limits("position"), now)
                    is TelemetryStatus.FRESH,
                }
                if self.last_known_position
                else None
            ),
            "geofence": {
                "status": str(self.geofence_status),
                "margin_m": self.geofence_margin_m,
            },
            "assignment": {
                "mission_id": self.mission_id,
                "sector_id": self.sector_id,
                "sector_code": self.sector_code,
                "delivery_task_id": self.delivery_task_id,
                "busy_with_command": self.busy_with_command,
            },
            "status_texts": [
                {"at": at.isoformat(), "severity": s.severity, "text": s.text}
                for at, s in list(self.status_texts)[-10:]
            ],
        }

    def card(self, policy: FreshnessPolicy, now: float | None = None) -> dict[str, Any]:
        """Compact projection for the fleet summary and the dashboard cards.

        Values are omitted (``None``) rather than shown stale; the matching
        ``*_status`` field tells the UI why.
        """
        now = now if now is not None else time.monotonic()

        def value_or_none(stream: str, holder: TimedValue[Any]) -> tuple[Any, str]:
            status = policy.status(stream, holder, now)
            return (holder.value if status is not TelemetryStatus.NO_DATA else None, str(status))

        pos, pos_status = value_or_none("position", self.position)
        bat, bat_status = value_or_none("battery", self.battery)
        vel, vel_status = value_or_none("velocity", self.velocity)
        hdg, hdg_status = value_or_none("attitude", self.heading)
        mode, mode_status = value_or_none("flight_mode", self.flight_mode)
        gps, gps_status = value_or_none("gps", self.gps)
        armed, _ = value_or_none("flight_mode", self.armed)

        return {
            "drone_id": self.drone_id,
            "role": str(self.role),
            "connection_state": str(self.connection_state),
            "online": self.is_connected,
            "identity_verified": self.identity_verified,
            "battery_percent": bat.remaining_percent if bat else None,
            "battery_status": bat_status,
            "battery_voltage_v": bat.voltage_v if bat else None,
            "latitude": pos.latitude if pos else None,
            "longitude": pos.longitude if pos else None,
            "altitude_relative_m": pos.relative_altitude_m if pos else None,
            "position_status": pos_status,
            "ground_speed_mps": round(vel.ground_speed_mps, 2) if vel else None,
            "vertical_speed_mps": round(vel.vertical_speed_mps, 2) if vel else None,
            "speed_status": vel_status,
            "heading_deg": hdg,
            "heading_status": hdg_status,
            "flight_mode": mode,
            "flight_mode_status": mode_status,
            "armed": armed,
            "gps_fix": gps.fix_type_name if gps else None,
            "satellites": gps.satellites if gps else None,
            "gps_status": gps_status,
            "geofence_status": str(self.geofence_status),
            "mission_id": self.mission_id,
            "sector_code": self.sector_code,
            "delivery_task_id": self.delivery_task_id,
            "last_contact_age_s": (
                round(age, 2) if (age := self.contact_age_s(now)) is not None else None
            ),
        }

    def trail_geojson(self, limit: int = TRAIL_MAXLEN) -> dict[str, Any]:
        points = list(self.trail)[-limit:]
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[p.longitude, p.latitude] for p in points],
            },
            "properties": {
                "drone_id": self.drone_id,
                "role": str(self.role),
                "point_count": len(points),
                "from": points[0].at.isoformat() if points else None,
                "to": points[-1].at.isoformat() if points else None,
            },
        }


def _render_struct(
    policy: FreshnessPolicy,
    stream: str,
    holder: TimedValue[Any],
    now: float,
    builder: Any,
) -> dict[str, Any]:
    """Render a composite telemetry value with its freshness.

    The payload is built only when the value is not NO_DATA, so an expired
    struct can never leak into the response.
    """
    rendered = policy.render(stream, holder, now)
    rendered["value"] = builder() if rendered["value"] is not None else None
    return rendered
