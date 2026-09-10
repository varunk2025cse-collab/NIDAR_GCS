"""DroneConnectionManager -- owns every link in the fleet.

One :class:`~app.drone.connection.DroneConnection` per configured airframe,
each with its own endpoint, its own mavsdk_server port and its own verified
MAVLink identity. Lookups are by ``drone_id``, and the mapping from drone_id
to a live adapter is the only route by which a command can reach an aircraft.
That is what makes it structurally impossible for a command addressed to D3 to
be delivered to D1.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import ConnectionConfig, FleetConfig, Settings, TelemetryRatesConfig
from app.core.enums import ConnectionState
from app.core.exceptions import DroneNotConnectedError, DroneNotFoundError
from app.core.logging import get_logger
from app.drone.adapter import DroneAdapter
from app.drone.connection import AdapterFactory, DroneConnection, default_adapter_factory
from app.drone.state import DroneState
from app.realtime.event_bus import EventBus

logger = get_logger(__name__)


class DroneConnectionManager:
    def __init__(
        self,
        fleet_config: FleetConfig,
        connection_config: ConnectionConfig,
        telemetry_rates: TelemetryRatesConfig,
        event_bus: EventBus,
        adapter_factory: AdapterFactory = default_adapter_factory,
    ) -> None:
        self._fleet_config = fleet_config
        self._connection_config = connection_config
        self._telemetry_rates = telemetry_rates
        self._bus = event_bus
        self._adapter_factory = adapter_factory
        self._started = False
        # Connections are built up front, not on start(). The configured fleet
        # exists whether or not any radio is on, so the dashboard shows three
        # aircraft as DISCONNECTED rather than the API reporting them unknown.
        self._connections: dict[str, DroneConnection] = {
            drone_config.drone_id: DroneConnection(
                config=drone_config,
                connection_config=connection_config,
                telemetry_rates=telemetry_rates,
                event_bus=event_bus,
                adapter_factory=adapter_factory,
            )
            for drone_config in fleet_config.drones
            if drone_config.enabled
        }

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        fleet_config: FleetConfig,
        event_bus: EventBus,
        adapter_factory: AdapterFactory = default_adapter_factory,
    ) -> DroneConnectionManager:
        return cls(
            fleet_config=fleet_config,
            connection_config=settings.connection,
            telemetry_rates=settings.telemetry_rates,
            event_bus=event_bus,
            adapter_factory=adapter_factory,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        if not self._connections:
            logger.warning("no_drones_configured", config_hint="config/fleet.yaml")

        # Links are brought up concurrently: a scout that is not yet powered
        # on must not delay the aircraft that are.
        await asyncio.gather(*(c.start() for c in self._connections.values()))
        self._started = True
        logger.info("connection_manager_started", drones=list(self._connections))

    async def stop(self) -> None:
        # Runs even if start() never did, so a failed startup still tears down
        # cleanly.
        await asyncio.gather(
            *(c.stop() for c in self._connections.values()), return_exceptions=True
        )
        self._started = False
        logger.info("connection_manager_stopped")

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def has(self, drone_id: str) -> bool:
        return drone_id.upper() in self._connections

    def get(self, drone_id: str) -> DroneConnection:
        connection = self._connections.get(drone_id.upper())
        if connection is None:
            raise DroneNotFoundError(
                f"Drone {drone_id} is not part of the configured fleet",
                details={"drone_id": drone_id, "known": sorted(self._connections)},
            )
        return connection

    def state(self, drone_id: str) -> DroneState:
        return self.get(drone_id).state

    def require_adapter(self, drone_id: str) -> DroneAdapter:
        """Return the live adapter, or refuse.

        Refusing here -- rather than queueing or retrying -- is deliberate: a
        command issued over a link the GCS cannot confirm is a command with an
        unknown outcome.
        """
        connection = self.get(drone_id)
        adapter = connection.adapter
        if adapter is None or not connection.state.is_commandable:
            raise DroneNotConnectedError(
                f"{drone_id} is not connected",
                details={
                    "drone_id": drone_id,
                    "connection_state": str(connection.state.connection_state),
                    "last_contact_age_s": connection.state.contact_age_s(),
                    "identity_verified": connection.state.identity_verified,
                },
            )
        return adapter

    @property
    def connections(self) -> dict[str, DroneConnection]:
        return dict(self._connections)

    @property
    def states(self) -> dict[str, DroneState]:
        return {drone_id: c.state for drone_id, c in self._connections.items()}

    def states_by_role(self, role: str) -> list[DroneState]:
        return [s for s in self.states.values() if str(s.role) == str(role)]

    def connected_count(self) -> int:
        return sum(
            1
            for c in self._connections.values()
            if c.state.connection_state is ConnectionState.CONNECTED
        )

    def add_telemetry_hook(self, hook: Any) -> None:
        """Register a telemetry hook on every link, current and future."""
        for connection in self._connections.values():
            connection.add_telemetry_hook(hook)

    def diagnostics(self) -> list[dict[str, Any]]:
        return [c.link_diagnostics() for c in self._connections.values()]
