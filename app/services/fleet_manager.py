"""FleetManager -- the operator-facing view of the physical fleet.

Sits directly on top of :class:`DroneConnectionManager` and adds the things
the connection layer has no business knowing about: which mission an aircraft
is flying, which sector it is searching, and the aggregate numbers on the
dashboard header.

It holds no telemetry of its own. Every value it returns is read from the live
:class:`DroneState` at call time, with its freshness attached.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DroneRole, FleetConfig, Settings
from app.core.enums import ConnectionState, TelemetryStatus
from app.core.exceptions import DroneNotFoundError
from app.core.freshness import FreshnessPolicy
from app.core.logging import get_logger
from app.drone.manager import DroneConnectionManager
from app.drone.state import DroneState
from app.models.drone import Drone

logger = get_logger(__name__)


class FleetManager:
    def __init__(
        self,
        connection_manager: DroneConnectionManager,
        fleet_config: FleetConfig,
        settings: Settings,
    ) -> None:
        self._connections = connection_manager
        self._fleet_config = fleet_config
        self._settings = settings
        self._policy = FreshnessPolicy(settings.freshness)
        #: drone_id -> database uuid, resolved once at startup.
        self._drone_uuids: dict[str, uuid.UUID] = {}

    @property
    def policy(self) -> FreshnessPolicy:
        return self._policy

    @property
    def connection_manager(self) -> DroneConnectionManager:
        return self._connections

    # ------------------------------------------------------------------
    # registry synchronisation
    # ------------------------------------------------------------------
    async def sync_registry(self, session: AsyncSession) -> dict[str, uuid.UUID]:
        """Reconcile the configured fleet with the ``drones`` table.

        Identity columns observed from the aircraft (hardware uid, firmware)
        are written back as they are learned, so the database records what was
        actually flown rather than what was configured.
        """
        result = await session.execute(select(Drone))
        existing = {d.drone_id: d for d in result.scalars().all()}

        for config in self._fleet_config.drones:
            row = existing.get(config.drone_id)
            if row is None:
                row = Drone(
                    drone_id=config.drone_id,
                    name=config.name,
                    role=config.role,
                    system_id=config.system_id,
                    component_id=config.component_id,
                    connection_endpoint=config.connection_endpoint,
                    payload_capacity_g=config.payload_capacity_g,
                    enabled=config.enabled,
                )
                session.add(row)
                await session.flush()
                logger.info("drone_registered", drone_id=config.drone_id, id=str(row.id))
            else:
                row.name = config.name
                row.role = config.role
                row.system_id = config.system_id
                row.component_id = config.component_id
                row.connection_endpoint = config.connection_endpoint
                row.payload_capacity_g = config.payload_capacity_g
                row.enabled = config.enabled
            self._drone_uuids[config.drone_id] = row.id

        await session.flush()
        return dict(self._drone_uuids)

    async def record_observed_identity(self, session: AsyncSession, drone_id: str) -> None:
        """Persist the identity the aircraft actually reported."""
        state = self._connections.state(drone_id)
        if state.identity is None:
            return
        drone_uuid = self._drone_uuids.get(drone_id)
        if drone_uuid is None:
            return
        row = await session.get(Drone, drone_uuid)
        if row is None:
            return
        identity = state.identity
        row.hardware_uid = identity.hardware_uid or row.hardware_uid
        row.autopilot_type = identity.autopilot or row.autopilot_type
        row.vehicle_type = identity.vehicle_type or row.vehicle_type
        row.firmware_version = identity.firmware_version or row.firmware_version
        row.firmware_vendor = identity.firmware_vendor or row.firmware_vendor
        row.product_name = identity.product_name or row.product_name

    def drone_config(self, drone_id: str) -> Any | None:
        """Configured definition of an airframe (role, endpoint, payload)."""
        return self._fleet_config.by_id(drone_id)

    def drone_uuid(self, drone_id: str) -> uuid.UUID | None:
        return self._drone_uuids.get(drone_id.upper())

    def drone_id_for_uuid(self, drone_uuid: uuid.UUID) -> str | None:
        for drone_id, value in self._drone_uuids.items():
            if value == drone_uuid:
                return drone_id
        return None

    def require_drone_uuid(self, drone_id: str) -> uuid.UUID:
        found = self.drone_uuid(drone_id)
        if found is None:
            raise DroneNotFoundError(
                f"Drone {drone_id} has no database record",
                details={"drone_id": drone_id},
            )
        return found

    # ------------------------------------------------------------------
    # live state access
    # ------------------------------------------------------------------
    def state(self, drone_id: str) -> DroneState:
        return self._connections.state(drone_id)

    @property
    def states(self) -> dict[str, DroneState]:
        return self._connections.states

    def snapshot(self, drone_id: str) -> dict[str, Any]:
        return self.state(drone_id).snapshot(self._policy)

    def card(self, drone_id: str) -> dict[str, Any]:
        return self.state(drone_id).card(self._policy)

    def cards(self) -> list[dict[str, Any]]:
        return [s.card(self._policy) for s in self.states.values()]

    def snapshots(self) -> list[dict[str, Any]]:
        return [s.snapshot(self._policy) for s in self.states.values()]

    # ------------------------------------------------------------------
    # role / availability queries
    # ------------------------------------------------------------------
    def scouts(self) -> list[DroneState]:
        return [s for s in self.states.values() if s.role is DroneRole.SCOUT]

    def delivery_drones(self) -> list[DroneState]:
        return [s for s in self.states.values() if s.role is DroneRole.DELIVERY]

    def available_delivery_drones(self) -> list[DroneState]:
        """Delivery aircraft that are connected and not already on a task.

        Returned in battery order (highest first) so the dispatcher naturally
        picks the aircraft with the most margin. Aircraft with no battery
        telemetry sort last -- an unknown battery is never treated as a good one.
        """

        def sort_key(state: DroneState) -> tuple[int, float]:
            percent = state.battery_percent()
            return (0, -percent) if percent is not None else (1, 0.0)

        candidates = [
            s
            for s in self.delivery_drones()
            if s.is_commandable and s.delivery_task_id is None
        ]
        return sorted(candidates, key=sort_key)

    # ------------------------------------------------------------------
    # assignment bookkeeping
    # ------------------------------------------------------------------
    def assign_mission(self, drone_id: str, mission_id: str | None) -> None:
        self.state(drone_id).mission_id = mission_id

    def assign_sector(
        self, drone_id: str, sector_id: str | None, sector_code: str | None = None
    ) -> None:
        state = self.state(drone_id)
        state.sector_id = sector_id
        state.sector_code = sector_code

    def assign_delivery(self, drone_id: str, task_id: str | None) -> None:
        self.state(drone_id).delivery_task_id = task_id

    def release_mission(self, mission_id: str) -> None:
        for state in self.states.values():
            if state.mission_id == mission_id:
                state.mission_id = None
                state.sector_id = None
                state.sector_code = None
                state.delivery_task_id = None

    def drones_for_mission(self, mission_id: str) -> list[DroneState]:
        return [s for s in self.states.values() if s.mission_id == mission_id]

    # ------------------------------------------------------------------
    # dashboard aggregates
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The numbers behind the dashboard header and fleet panel."""
        states = list(self.states.values())
        by_state: dict[str, int] = {}
        for state in states:
            key = str(state.connection_state)
            by_state[key] = by_state.get(key, 0) + 1

        fresh_telemetry = sum(
            1
            for s in states
            if self._policy.status("position", s.position) is TelemetryStatus.FRESH
        )
        return {
            "total": len(states),
            "online": sum(
                1 for s in states if s.connection_state is ConnectionState.CONNECTED
            ),
            "degraded": sum(
                1 for s in states if s.connection_state is ConnectionState.DEGRADED
            ),
            "offline": sum(
                1
                for s in states
                if s.connection_state
                in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
            ),
            "identity_verified": sum(1 for s in states if s.identity_verified),
            "armed": sum(1 for s in states if s.armed.value is True),
            "in_air": sum(1 for s in states if s.in_air.value is True),
            "with_fresh_position": fresh_telemetry,
            "by_connection_state": by_state,
            "by_role": {
                str(role): sum(1 for s in states if s.role is role) for role in DroneRole
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def fleet_payload(self) -> dict[str, Any]:
        """Full fleet frame, as broadcast on ``/ws/fleet``."""
        return {
            "summary": self.summary(),
            "drones": self.cards(),
        }
