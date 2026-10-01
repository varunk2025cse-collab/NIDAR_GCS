"""Configuration for the NIDAR RescueSwarm GCS backend.

Nothing about the physical fleet is hard-coded here. Drone identity,
connection endpoints and every safety threshold are supplied through a fleet
configuration file and/or environment variables so the same binary can be
pointed at a different set of physical aircraft without a code change.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.energy import EnergyModelConfig, load_energy_model


class DroneRole(StrEnum):
    """Operational role of a physical airframe."""

    SCOUT = "SCOUT"
    DELIVERY = "DELIVERY"
    RELAY = "RELAY"
    UNKNOWN = "UNKNOWN"


class DroneConfig(BaseModel):
    """Configuration for one *physical* aircraft.

    ``system_id`` is the MAVLink system id the airframe is expected to
    transmit. It is used to *verify* identity against the heartbeat actually
    observed on the wire -- it is never assumed.
    """

    model_config = {"extra": "forbid"}

    drone_id: str = Field(..., min_length=1, max_length=32)
    name: str | None = None
    role: DroneRole = DroneRole.UNKNOWN

    # MAVLink identity that this endpoint is expected to carry.
    system_id: int = Field(..., ge=1, le=255)
    component_id: int = Field(default=1, ge=1, le=255)

    # e.g. "udpin://0.0.0.0:14541", "serial:///dev/ttyUSB0:57600", "tcpin://:5760"
    connection_endpoint: str = Field(..., min_length=3)

    # Optional dedicated endpoint used only by the passive identity probe.
    # When None the probe listens on ``connection_endpoint`` and releases the
    # socket before MAVSDK binds it.
    identity_endpoint: str | None = None

    # Hardware UID (PX4 SYS_UID / autopilot uid2) that this drone_id must
    # match. When set, a mismatch is a hard identity failure.
    expected_hardware_uid: str | None = None

    # Where mavsdk_server for this drone should listen (gRPC). Distinct per
    # drone; one server instance per airframe keeps routing unambiguous.
    mavsdk_server_port: int = Field(default=0, ge=0, le=65535)
    mavsdk_server_address: str | None = None

    # Payload capability, used by the delivery feasibility checks.
    payload_capacity_g: int | None = Field(default=None, ge=0)

    enabled: bool = True

    @field_validator("drone_id")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class FleetConfig(BaseModel):
    model_config = {"extra": "forbid"}

    drones: list[DroneConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> FleetConfig:
        ids = [d.drone_id for d in self.drones]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate drone_id in fleet configuration")
        sysids = [(d.system_id, d.component_id) for d in self.drones]
        if len(sysids) != len(set(sysids)):
            raise ValueError("duplicate (system_id, component_id) in fleet configuration")
        endpoints = [d.connection_endpoint for d in self.drones]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("duplicate connection_endpoint in fleet configuration")
        ports = [d.mavsdk_server_port for d in self.drones if d.mavsdk_server_port]
        if len(ports) != len(set(ports)):
            raise ValueError("duplicate mavsdk_server_port in fleet configuration")
        return self

    def by_id(self, drone_id: str) -> DroneConfig | None:
        for d in self.drones:
            if d.drone_id == drone_id.upper():
                return d
        return None


class TelemetryFreshnessConfig(BaseModel):
    """Per-stream age limits, in seconds.

    A value older than ``*_fresh_s`` is reported STALE; older than
    ``*_stale_s`` the field drops back to NO_DATA. These limits are the only
    thing standing between the operator and a value that looks live but is not.
    """

    model_config = {"extra": "forbid"}

    position_fresh_s: float = 2.0
    position_stale_s: float = 10.0
    battery_fresh_s: float = 5.0
    battery_stale_s: float = 30.0
    gps_fresh_s: float = 5.0
    gps_stale_s: float = 30.0
    attitude_fresh_s: float = 2.0
    attitude_stale_s: float = 10.0
    velocity_fresh_s: float = 2.0
    velocity_stale_s: float = 10.0
    flight_mode_fresh_s: float = 5.0
    flight_mode_stale_s: float = 30.0
    health_fresh_s: float = 10.0
    health_stale_s: float = 60.0
    default_fresh_s: float = 5.0
    default_stale_s: float = 30.0


class ConnectionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # A drone is DEGRADED after this long without a heartbeat, DISCONNECTED
    # after ``heartbeat_lost_s``.
    heartbeat_degraded_s: float = 3.0
    heartbeat_lost_s: float = 10.0

    connect_timeout_s: float = 30.0
    identity_probe_timeout_s: float = 15.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 30.0
    reconnect_backoff_factor: float = 2.0

    # Require the passive pymavlink probe to confirm system_id before MAVSDK
    # is allowed to attach. Strongly recommended for multi-drone operation.
    require_identity_probe: bool = True
    # If the probe cannot run (e.g. endpoint type unsupported), refuse to
    # connect rather than guessing which airframe is on the other end.
    fail_closed_on_identity: bool = True

    # Path to the mavsdk_server binary. When None the mavsdk python package
    # bundled binary is used.
    mavsdk_server_bin: str | None = None

    # MAVLink identity this GCS presents on the network.
    gcs_system_id: int = Field(default=245, ge=1, le=255)
    gcs_component_id: int = Field(default=190, ge=1, le=255)


class TelemetryRatesConfig(BaseModel):
    """Requested telemetry stream rates, in Hz.

    PX4 grants these on a best-effort basis and the available bandwidth on a
    telemetry radio is limited, so keep them modest for a real RF link.
    A rate of 0 means "leave the autopilot default alone".
    """

    model_config = {"extra": "forbid"}

    position_hz: float = 4.0
    velocity_hz: float = 2.0
    attitude_hz: float = 2.0
    battery_hz: float = 1.0
    gps_info_hz: float = 1.0
    health_hz: float = 1.0
    landed_state_hz: float = 1.0
    home_hz: float = 0.2
    # Setting rates can fail on links that do not support the command; that is
    # logged and tolerated rather than treated as a connection failure.
    tolerate_rate_failures: bool = True


class SafetyThresholds(BaseModel):
    """Mission-supervisor thresholds.

    These are advisory: PX4 failsafes remain authoritative for the aircraft.
    These drive GCS alerts and mission gating only.
    """

    model_config = {"extra": "forbid"}

    battery_warning_pct: float = 40.0
    battery_critical_pct: float = 25.0
    battery_emergency_pct: float = 15.0
    battery_min_for_takeoff_pct: float = 50.0
    battery_min_for_delivery_pct: float = 45.0
    # Reserve that must remain after a delivery round trip.
    battery_reserve_pct: float = 25.0
    # Conservative consumption estimate used by delivery feasibility.
    battery_pct_per_km: float = 4.0
    battery_pct_per_minute_hover: float = 1.2

    min_satellites: int = 8
    min_gps_fix_type: int = 3  # 3 = 3D fix

    geofence_warning_margin_m: float = 25.0

    mission_max_duration_s: int = 1800
    mission_warning_remaining_s: int = 300

    max_delivery_distance_m: float = 3000.0
    delivery_cruise_speed_mps: float = 8.0
    delivery_hover_time_s: float = 45.0
    # Delivery dispatch will not proceed on an uncalibrated energy model.
    # Set true ONLY for bench work where no aircraft can actually launch;
    # it converts the blocking calibration check into a warning.
    allow_uncalibrated_delivery: bool = False

    # Telemetry considered too old for any command to be authorised.
    command_requires_telemetry_age_s: float = 5.0


class SurvivorConfig(BaseModel):
    model_config = {"extra": "forbid"}

    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    auto_confirm_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    # Two detections within this radius are duplicate *candidates*.
    duplicate_radius_m: float = 25.0
    # ...and within this time window.
    duplicate_window_s: float = 900.0
    # Corroborating observations needed for automatic confirmation when the
    # single-shot confidence is below ``auto_confirm_confidence``.
    corroborations_for_confirm: int = 2
    # Detections whose reported position accuracy is worse than this are kept
    # but never auto-confirmed.
    max_auto_confirm_accuracy_m: float = 30.0
    max_detection_age_s: float = 120.0


class CommandConfig(BaseModel):
    model_config = {"extra": "forbid"}

    default_timeout_s: float = 15.0
    arm_timeout_s: float = 15.0
    takeoff_timeout_s: float = 60.0
    land_timeout_s: float = 180.0
    rtl_timeout_s: float = 30.0
    mission_upload_timeout_s: float = 120.0
    # How long a completed idempotency key stays reserved.
    idempotency_ttl_s: float = 600.0
    # Poll interval when verifying that the aircraft actually changed state.
    verification_poll_s: float = 0.5
    # How long to wait for observable state change after an acknowledgement.
    verification_timeout_s: float = 20.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "NIDAR RescueSwarm GCS"
    environment: Literal["development", "bench", "field", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # --- persistence -----------------------------------------------------
    database_url: str = "postgresql+asyncpg://gcs:gcs@localhost:5432/nidar_gcs"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- security --------------------------------------------------------
    secret_key: str = Field(default="CHANGE-ME-IN-PRODUCTION", min_length=8)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 720
    # Shared secrets presented by companion computers when posting detections,
    # keyed by drone_id.
    companion_api_keys: dict[str, str] = Field(default_factory=dict)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    rate_limit_commands_per_minute: int = 60

    # --- fleet -----------------------------------------------------------
    fleet_config_file: str = "config/fleet.yaml"
    camera_config_file: str = "config/cameras.yaml"
    # Measured energy characteristics of the delivery airframe. A missing
    # file means UNCALIBRATED, which blocks delivery dispatch.
    energy_model_file: str = "config/energy_model.yaml"

    # --- operator console (served locally; never from a CDN) --------------
    # Pre-downloaded basemap tiles, laid out as {z}/{x}/{y}.jpg. Mounted at
    # /tiles only when the directory exists, so a station with no cached
    # imagery behaves exactly as before. The console degrades to a coordinate
    # grid rather than failing.
    tile_dir: str = "tiles"
    # Built frontend (`npm run build` in frontend/). Served at /console when
    # present so the ground station needs no second web server.
    console_dist_dir: str = "frontend/dist"

    # Which physical mechanism attests that a payload was actually released.
    # OPERATOR is the safe default: it requires a human to say so rather than
    # assuming a release that no sensor observed. Change it to
    # PAYLOAD_MECHANISM or SENSOR once that hardware reports in.
    delivery_confirmation_source: Literal[
        "PAYLOAD_MECHANISM", "COMPANION_COMPUTER", "OPERATOR", "SENSOR"
    ] = "OPERATOR"
    delivery_confirmation_timeout_s: float = 120.0
    # Automatic dispatch of a delivery aircraft on survivor confirmation.
    # Off by default: launching an aircraft is an operator decision.
    auto_dispatch_deliveries: bool = False

    # --- runtime tunables ------------------------------------------------
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    telemetry_rates: TelemetryRatesConfig = Field(default_factory=TelemetryRatesConfig)
    freshness: TelemetryFreshnessConfig = Field(default_factory=TelemetryFreshnessConfig)
    safety: SafetyThresholds = Field(default_factory=SafetyThresholds)
    survivor: SurvivorConfig = Field(default_factory=SurvivorConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)

    # --- engines ---------------------------------------------------------
    safety_engine_interval_s: float = 1.0
    fleet_broadcast_interval_s: float = 1.0
    telemetry_persist_interval_s: float = 1.0
    telemetry_persist_min_distance_m: float = 2.0
    system_health_interval_s: float = 5.0

    # Start the drone connection manager on application startup. Disabled in
    # unit tests; never disabled in the field.
    enable_drone_connections: bool = True
    enable_safety_engine: bool = True

    log_level: str = "INFO"
    log_json: bool = True
    log_dir: str = "logs"

    def load_energy_model(self) -> EnergyModelConfig:
        """Load the delivery energy model.

        Defaults to an explicitly uncalibrated model when the file is absent,
        which is the honest state of an airframe nobody has measured yet.
        """
        return load_energy_model(self.energy_model_file)

    def load_fleet(self) -> FleetConfig:
        """Load the fleet definition from file, then apply env overrides.

        ``DRONE_<ID>_CONNECTION`` overrides the endpoint for a drone already
        declared in the fleet file. The file remains the single place where
        identity (role, system id, hardware uid) is declared, so an env var
        can never silently re-point a *role* at the wrong airframe.
        """
        path = Path(self.fleet_config_file)
        raw: dict[str, Any] = {"drones": []}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{path} must contain a mapping at the top level")
            raw = loaded
        fleet = FleetConfig.model_validate(raw)
        for drone in fleet.drones:
            override = os.environ.get(f"DRONE_{drone.drone_id}_CONNECTION")
            if override:
                drone.connection_endpoint = override
            uid = os.environ.get(f"DRONE_{drone.drone_id}_HARDWARE_UID")
            if uid:
                drone.expected_hardware_uid = uid
        return fleet


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_fleet_config() -> FleetConfig:
    return get_settings().load_fleet()


@lru_cache(maxsize=1)
def get_energy_model() -> EnergyModelConfig:
    return get_settings().load_energy_model()


def reset_settings_cache() -> None:
    """Test hook -- never called from request handling."""
    get_settings.cache_clear()
    get_fleet_config.cache_clear()
    get_energy_model.cache_clear()
