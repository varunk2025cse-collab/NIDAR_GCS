"""One supervised link to one physical aircraft.

Owns the whole lifecycle:

    DISCOVERING -> CONNECTING -> IDENTIFYING -> CONNECTED
                                                  |
                                     (telemetry goes quiet)
                                                  v
                                              DEGRADED
                                                  |
                                                  v
                                            DISCONNECTED -> (reconnect)

Identity is verified *before* the aircraft is admitted to the fleet. If the
airframe answering on D3's endpoint reports a different system id, the link is
refused and an alert is raised -- the alternative is a delivery command
reaching a scout.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import ConnectionConfig, DroneConfig, TelemetryRatesConfig
from app.core.enums import ConnectionState
from app.core.logging import get_logger
from app.drone.adapter import DroneAdapter
from app.drone.identity import IdentityProbeUnsupported, probe_identity
from app.drone.mavsdk_adapter import MavsdkDroneAdapter
from app.drone.state import DroneState
from app.drone.types import VehicleIdentity
from app.realtime.event_bus import EventBus, EventType

logger = get_logger(__name__)

#: Factory so tests can inject a double. Production always builds the MAVSDK
#: adapter; nothing in ``app/`` ever supplies anything else.
AdapterFactory = Callable[[DroneConfig, ConnectionConfig, TelemetryRatesConfig], DroneAdapter]


def default_adapter_factory(
    drone: DroneConfig, connection: ConnectionConfig, rates: TelemetryRatesConfig
) -> DroneAdapter:
    return MavsdkDroneAdapter(drone, connection, rates)


@dataclass(slots=True)
class IdentityCheck:
    ok: bool
    reason: str | None = None
    observed: VehicleIdentity | None = None


class DroneConnection:
    """Supervises the link to one airframe and keeps its :class:`DroneState`."""

    def __init__(
        self,
        config: DroneConfig,
        connection_config: ConnectionConfig,
        telemetry_rates: TelemetryRatesConfig,
        event_bus: EventBus,
        adapter_factory: AdapterFactory = default_adapter_factory,
    ) -> None:
        self.config = config
        self.state = DroneState.from_config(config)
        self._connection_config = connection_config
        self._telemetry_rates = telemetry_rates
        self._bus = event_bus
        self._adapter_factory = adapter_factory

        self._adapter: DroneAdapter | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._watchdog_task: asyncio.Task[None] | None = None
        self._running = False
        #: Serialises commands so two operator actions cannot interleave on
        #: the same aircraft.
        self._command_lock = asyncio.Lock()
        #: Set the instant the link is judged lost, so the supervisor reacts
        #: immediately rather than on the next poll tick.
        self._link_lost = asyncio.Event()
        self._log = logger.bind(drone_id=config.drone_id)
        self._telemetry_hooks: list[Callable[[DroneState, str], Awaitable[None]]] = []

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------
    @property
    def drone_id(self) -> str:
        return self.config.drone_id

    @property
    def adapter(self) -> DroneAdapter | None:
        return self._adapter

    @property
    def command_lock(self) -> asyncio.Lock:
        return self._command_lock

    def add_telemetry_hook(self, hook: Callable[[DroneState, str], Awaitable[None]]) -> None:
        """Register a coroutine invoked after each telemetry update.

        Used by the telemetry service for persistence and by the geofence
        service for per-sample containment checks.
        """
        self._telemetry_hooks.append(hook)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name=f"drone-{self.drone_id}-supervisor"
        )
        self._log.info("connection_supervisor_started", endpoint=self.config.connection_endpoint)

    async def stop(self) -> None:
        self._running = False
        self._link_lost.set()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None
        await self._teardown_link("shutdown")
        # An orderly shutdown must not erase why the link was in trouble. If
        # this aircraft was refused on identity, or dropped out, that reason is
        # what an operator needs after the fact -- not "GCS shutdown".
        self.state.set_connection_state(
            ConnectionState.DISCONNECTED,
            None if self.state.last_error else "GCS shutdown",
        )
        self._log.info("connection_supervisor_stopped", last_error=self.state.last_error)

    # ------------------------------------------------------------------
    # supervision loop
    # ------------------------------------------------------------------
    async def _supervise(self) -> None:
        delay = self._connection_config.reconnect_initial_delay_s
        while self._running:
            try:
                self._link_lost.clear()
                connected = await self._establish()
                if connected:
                    delay = self._connection_config.reconnect_initial_delay_s
                    # Block here until the link is judged lost.
                    await self._await_link_loss()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.set_connection_state(ConnectionState.ERROR, str(exc))
                self._link_lost.set()
                self._log.error(
                    "connection_attempt_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                self._publish_disconnected(str(exc))

            await self._teardown_link("link lost")

            if not self._running:
                return

            self.state.reconnect_attempts += 1
            self._log.info("reconnect_scheduled", delay_s=round(delay, 1),
                           attempt=self.state.reconnect_attempts)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(
                delay * self._connection_config.reconnect_backoff_factor,
                self._connection_config.reconnect_max_delay_s,
            )

    async def _establish(self) -> bool:
        """Bring the link up, verify identity, and start telemetry."""
        self.state.set_connection_state(ConnectionState.CONNECTING)
        self._log.info("connecting", endpoint=self.config.connection_endpoint)

        # 1. Passive identity probe -- who is actually on this endpoint?
        self.state.set_connection_state(ConnectionState.IDENTIFYING)
        check = await self._verify_identity()
        if not check.ok:
            self.state.identity_verified = False
            self.state.set_connection_state(ConnectionState.ERROR, check.reason)
            self._bus.emit(
                EventType.DRONE_IDENTITY_MISMATCH,
                drone_id=self.drone_id,
                payload={
                    "endpoint": self.config.connection_endpoint,
                    "expected_system_id": self.config.system_id,
                    "observed_system_id": (
                        check.observed.system_id if check.observed else None
                    ),
                    "reason": check.reason,
                },
            )
            self._log.error("identity_check_failed", reason=check.reason)
            return False

        # 2. Attach the real transport.
        adapter = self._adapter_factory(
            self.config, self._connection_config, self._telemetry_rates
        )
        self._adapter = adapter
        identity = await adapter.connect(self._connection_config.connect_timeout_s)

        # Merge what the wire told us with what the autopilot told us.
        if check.observed is not None:
            identity = VehicleIdentity(
                system_id=check.observed.system_id,
                component_id=check.observed.component_id,
                autopilot=check.observed.autopilot or identity.autopilot,
                vehicle_type=check.observed.vehicle_type or identity.vehicle_type,
                hardware_uid=identity.hardware_uid,
                firmware_version=identity.firmware_version,
                firmware_vendor=identity.firmware_vendor,
                product_name=identity.product_name,
                observed_at=datetime.now(UTC),
                raw={**check.observed.raw, **identity.raw},
            )

        # 3. Hardware UID gate, when one is configured.
        if self.config.expected_hardware_uid and identity.hardware_uid:
            if identity.hardware_uid != self.config.expected_hardware_uid:
                reason = (
                    f"hardware uid mismatch: expected "
                    f"{self.config.expected_hardware_uid}, observed {identity.hardware_uid}"
                )
                self.state.set_connection_state(ConnectionState.ERROR, reason)
                self._bus.emit(
                    EventType.DRONE_IDENTITY_MISMATCH,
                    drone_id=self.drone_id,
                    payload={"reason": reason, "observed_uid": identity.hardware_uid},
                )
                self._log.error("hardware_uid_mismatch", reason=reason)
                await self._teardown_link(reason)
                return False

        self.state.identity = identity
        self.state.identity_verified = True
        self.state.mark_contact()
        self._link_lost.clear()
        self.state.set_connection_state(ConnectionState.CONNECTED)

        # 4. Start telemetry streams and the staleness watchdog.
        self._start_streams()
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name=f"drone-{self.drone_id}-watchdog"
        )

        self._bus.emit(
            EventType.DRONE_CONNECTED,
            drone_id=self.drone_id,
            payload={
                "endpoint": self.config.connection_endpoint,
                "role": str(self.config.role),
                "system_id": identity.system_id,
                "component_id": identity.component_id,
                "hardware_uid": identity.hardware_uid,
                "firmware_version": identity.firmware_version,
                "product_name": identity.product_name,
            },
        )
        self._bus.emit(
            EventType.DRONE_IDENTITY_VERIFIED,
            drone_id=self.drone_id,
            payload={"system_id": identity.system_id, "hardware_uid": identity.hardware_uid},
        )
        self._log.info("connected", system_id=identity.system_id,
                       hardware_uid=identity.hardware_uid)
        return True

    async def _verify_identity(self) -> IdentityCheck:
        """Confirm the endpoint carries the expected airframe."""
        if not self._connection_config.require_identity_probe:
            return IdentityCheck(ok=True, reason="probe disabled by configuration")

        endpoint = self.config.identity_endpoint or self.config.connection_endpoint
        try:
            result = await probe_identity(
                endpoint,
                self._connection_config.identity_probe_timeout_s,
                expected_system_id=self.config.system_id,
            )
        except IdentityProbeUnsupported as exc:
            if self._connection_config.fail_closed_on_identity:
                return IdentityCheck(ok=False, reason=f"identity probe unsupported: {exc}")
            self._log.warning("identity_probe_unsupported_allowed", error=str(exc))
            return IdentityCheck(ok=True, reason=str(exc))

        if result is None:
            return IdentityCheck(
                ok=False,
                reason=(
                    f"no vehicle heartbeat on {endpoint} within "
                    f"{self._connection_config.identity_probe_timeout_s}s"
                ),
            )

        observed = result.identity
        if observed.system_id != self.config.system_id:
            return IdentityCheck(
                ok=False,
                observed=observed,
                reason=(
                    f"system id mismatch on {endpoint}: expected "
                    f"{self.config.system_id}, observed {observed.system_id}"
                ),
            )
        return IdentityCheck(ok=True, observed=observed)

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------
    def _start_streams(self) -> None:
        adapter = self._adapter
        if adapter is None:
            return

        specs: list[tuple[str, Callable[[], AsyncIterator[Any]], Callable[[Any], None]]] = [
            ("position", adapter.position, self._on_position),
            ("velocity", adapter.velocity, self._on_velocity),
            ("attitude", adapter.attitude, self._on_attitude),
            ("heading", adapter.heading, self._on_heading),
            ("battery", adapter.battery, self._on_battery),
            ("gps", adapter.gps_info, self._on_gps),
            ("health", adapter.health, self._on_health),
            ("armed", adapter.armed, self._on_armed),
            ("in_air", adapter.in_air, self._on_in_air),
            ("flight_mode", adapter.flight_mode, self._on_flight_mode),
            ("landed_state", adapter.landed_state, self._on_landed_state),
            ("mission_progress", adapter.mission_progress, self._on_mission_progress),
            ("home", adapter.home_position, self._on_home),
            ("status_text", adapter.status_text, self._on_status_text),
        ]
        for name, factory, handler in specs:
            task = asyncio.create_task(
                self._consume_stream(name, factory, handler),
                name=f"drone-{self.drone_id}-{name}",
            )
            self._stream_tasks.append(task)

    async def _consume_stream(
        self,
        name: str,
        factory: Callable[[], AsyncIterator[Any]],
        handler: Callable[[Any], None],
    ) -> None:
        """Drain one telemetry stream for the life of the link.

        A stream that ends or errors is logged and left ended: it is real
        information that the aircraft stopped sending that data. The watchdog
        decides what that means for the link as a whole.
        """
        try:
            async for value in factory():
                handler(value)
                await self._run_hooks(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "telemetry_stream_failed", stream=name, error=str(exc),
                error_type=type(exc).__name__,
            )
        else:
            self._log.info("telemetry_stream_ended", stream=name)

    async def _run_hooks(self, stream: str) -> None:
        for hook in self._telemetry_hooks:
            try:
                await hook(self.state, stream)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(
                    "telemetry_hook_failed", stream=stream, error=str(exc), exc_info=True
                )

    def _on_position(self, value: Any) -> None:
        self.state.record_position(value)

    def _on_velocity(self, value: Any) -> None:
        self.state.velocity.set(value)
        self.state.mark_contact()

    def _on_attitude(self, value: Any) -> None:
        self.state.attitude.set(value)
        self.state.mark_contact()

    def _on_heading(self, value: Any) -> None:
        self.state.heading.set(value)
        self.state.mark_contact()

    def _on_battery(self, value: Any) -> None:
        self.state.battery.set(value)
        self.state.mark_contact()

    def _on_gps(self, value: Any) -> None:
        self.state.gps.set(value)
        self.state.mark_contact()

    def _on_health(self, value: Any) -> None:
        self.state.health.set(value)
        self.state.mark_contact()

    def _on_armed(self, value: Any) -> None:
        self.state.armed.set(bool(value))
        self.state.mark_contact()

    def _on_in_air(self, value: Any) -> None:
        self.state.in_air.set(bool(value))
        self.state.mark_contact()

    def _on_flight_mode(self, value: Any) -> None:
        self.state.flight_mode.set(str(value))
        self.state.mark_contact()

    def _on_landed_state(self, value: Any) -> None:
        self.state.landed_state.set(str(value))
        self.state.mark_contact()

    def _on_mission_progress(self, value: Any) -> None:
        self.state.mission_progress.set(value)
        self.state.mark_contact()

    def _on_home(self, value: Any) -> None:
        self.state.home.set(value)
        self.state.mark_contact()

    def _on_status_text(self, value: Any) -> None:
        self.state.record_status_text(value)
        self._bus.emit(
            EventType.MISSION_EVENT,
            drone_id=self.drone_id,
            mission_id=self.state.mission_id,
            payload={
                "event_type": "AUTOPILOT_STATUS_TEXT",
                "severity": value.severity,
                "text": value.text,
            },
        )

    # ------------------------------------------------------------------
    # watchdog
    # ------------------------------------------------------------------
    async def _watchdog(self) -> None:
        """Detect a link going quiet.

        Telemetry arriving is the only evidence the aircraft is still there.
        Silence for ``heartbeat_degraded_s`` marks the link DEGRADED; silence
        for ``heartbeat_lost_s`` declares it DISCONNECTED and triggers a
        reconnect. PX4 handles the aircraft side of a link loss through its
        own failsafe -- the GCS only reports what it can see.
        """
        degraded_after = self._connection_config.heartbeat_degraded_s
        lost_after = self._connection_config.heartbeat_lost_s
        poll = min(0.5, max(0.1, degraded_after / 4))

        while self._running:
            await asyncio.sleep(poll)
            age = self.state.contact_age_s()
            if age is None:
                continue
            if age >= lost_after:
                self._log.warning("link_lost_no_telemetry", age_s=round(age, 2))
                if self.state.set_connection_state(
                    ConnectionState.DISCONNECTED,
                    f"no telemetry for {age:.1f}s",
                ):
                    self._publish_disconnected(f"no telemetry for {age:.1f}s")
                self._link_lost.set()
                return
            if age >= degraded_after:
                if self.state.set_connection_state(
                    ConnectionState.DEGRADED, f"telemetry stale for {age:.1f}s"
                ):
                    self._log.warning("link_degraded", age_s=round(age, 2))
                    self._publish_state_changed()
            elif self.state.connection_state is ConnectionState.DEGRADED:
                if self.state.set_connection_state(ConnectionState.CONNECTED):
                    self._log.info("link_recovered", age_s=round(age, 2))
                    self._publish_state_changed()

    async def _await_link_loss(self) -> None:
        """Block until the link is judged lost.

        The watchdog sets the event, so the reconnect starts the moment loss
        is declared rather than up to a poll interval later.
        """
        await self._link_lost.wait()

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------
    async def _teardown_link(self, reason: str) -> None:
        for task in self._stream_tasks:
            task.cancel()
        for task in self._stream_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._stream_tasks.clear()

        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None

        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.disconnect()
            self._log.info("link_torn_down", reason=reason)

    def _publish_disconnected(self, reason: str) -> None:
        last = self.state.last_known_position
        self._bus.emit(
            EventType.DRONE_DISCONNECTED,
            drone_id=self.drone_id,
            mission_id=self.state.mission_id,
            payload={
                "reason": reason,
                "last_seen": (
                    self.state.last_contact_at.isoformat()
                    if self.state.last_contact_at
                    else None
                ),
                "last_heartbeat": (
                    self.state.last_contact_at.isoformat()
                    if self.state.last_contact_at
                    else None
                ),
                "loss_time": datetime.now(UTC).isoformat(),
                "last_position": (
                    {
                        "latitude": last.latitude,
                        "longitude": last.longitude,
                        "relative_altitude_m": last.relative_altitude_m,
                        "at": last.at.isoformat(),
                    }
                    if last
                    else None
                ),
            },
        )

    def _publish_state_changed(self) -> None:
        self._bus.emit(
            EventType.DRONE_STATE_UPDATED,
            drone_id=self.drone_id,
            mission_id=self.state.mission_id,
            payload={"connection_state": str(self.state.connection_state)},
        )

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def link_diagnostics(self) -> dict[str, Any]:
        return {
            "drone_id": self.drone_id,
            "endpoint": self.config.connection_endpoint,
            "state": str(self.state.connection_state),
            "identity_verified": self.state.identity_verified,
            "reconnect_attempts": self.state.reconnect_attempts,
            "last_contact_age_s": self.state.contact_age_s(),
            "active_streams": sum(1 for t in self._stream_tasks if not t.done()),
            "expected_streams": len(self._stream_tasks),
            "last_error": self.state.last_error,
            "supervisor_running": self._supervisor_task is not None
            and not self._supervisor_task.done(),
            "uptime_s": (
                round(time.time() - self.state.connected_since.timestamp(), 1)
                if self.state.connected_since
                else None
            ),
        }
