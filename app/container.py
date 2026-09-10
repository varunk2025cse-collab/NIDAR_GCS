"""Application container.

Builds every service once, in dependency order, and owns their lifecycle.
Constructed by the FastAPI lifespan handler and reached from request handlers
through ``request.app.state.container``.

Startup order is deliberate:

1. Database engine and the drone registry, so aircraft have database ids.
2. Event bus, before anything that publishes.
3. Fleet, telemetry, safety and mission services.
4. Real drone links last -- once everything that consumes telemetry is
   listening, so no aircraft data arrives before there is somewhere to put it.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.ai.detection_validator import DetectionValidator
from app.ai.survivor_event_ingestion import SurvivorEventIngestion
from app.core.config import FleetConfig, Settings, get_fleet_config, get_settings
from app.core.energy import EnergyModelConfig
from app.core.enums import DeliveryConfirmationSource, OperatorRole
from app.core.logging import get_logger
from app.core.security import SlidingWindowRateLimiter, TokenPrincipal
from app.database.session import dispose_engine, init_engine, session_scope
from app.drone.connection import AdapterFactory, default_adapter_factory
from app.drone.manager import DroneConnectionManager
from app.realtime.broadcaster import Broadcaster
from app.realtime.event_bus import EventBus, get_event_bus
from app.realtime.websocket_manager import WebSocketManager
from app.services.command_service import CommandService
from app.services.delivery_confirmation import ConfirmationRegistry
from app.services.delivery_manager import DeliveryManager
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService
from app.services.mission_manager import MissionManager
from app.services.preflight import PreflightService
from app.services.safety_engine import SafetyEngine
from app.services.search_manager import SearchSectorManager
from app.services.survivor_manager import SurvivorManager
from app.services.system_health import SystemHealthService
from app.services.task_dispatcher import TaskDispatcher
from app.services.telemetry_service import TelemetryService
from app.services.video_service import VideoService

logger = get_logger(__name__)

#: Identity attributed to automatic actions (auto-dispatch), so the audit log
#: never attributes a machine decision to a human operator.
SYSTEM_OPERATOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Container:
    def __init__(
        self,
        settings: Settings | None = None,
        fleet_config: FleetConfig | None = None,
        event_bus: EventBus | None = None,
        adapter_factory: AdapterFactory = default_adapter_factory,
        energy_model: EnergyModelConfig | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fleet_config = fleet_config or get_fleet_config()
        # Loaded from config/energy_model.yaml. Absent means UNCALIBRATED,
        # which blocks delivery dispatch rather than guessing.
        self.energy_model = energy_model or self.settings.load_energy_model()
        self.bus = event_bus or get_event_bus()
        self._adapter_factory = adapter_factory
        self._started = False

        # --- transport ---------------------------------------------------
        self.connection_manager = DroneConnectionManager.from_settings(
            self.settings, self.fleet_config, self.bus, adapter_factory
        )

        # --- core services ------------------------------------------------
        self.fleet = FleetManager(self.connection_manager, self.fleet_config, self.settings)
        self.events = EventService(self.bus)
        self.geofence = GeofenceService(self.settings)
        self.telemetry = TelemetryService(self.fleet, self.bus, self.settings)
        self.commands = CommandService(self.fleet, self.events, self.bus, self.settings)
        self.safety = SafetyEngine(self.fleet, self.geofence, self.bus, self.settings)
        self.preflight = PreflightService(
            self.fleet, self.geofence, self.settings, energy_model=self.energy_model
        )

        self.missions = MissionManager(
            self.fleet, self.commands, self.preflight, self.geofence,
            self.safety, self.events, self.bus, self.settings,
        )
        self.search = SearchSectorManager(
            self.fleet, self.commands, self.geofence, self.events, self.bus, self.settings
        )
        self.survivors = SurvivorManager(self.fleet, self.events, self.bus, self.settings)
        self.detection_validator = DetectionValidator(self.settings, self.geofence)
        self.ingestion = SurvivorEventIngestion(
            self.fleet, self.survivors, self.detection_validator, self.settings
        )

        self.confirmations = ConfirmationRegistry(
            DeliveryConfirmationSource(self.settings.delivery_confirmation_source)
        )
        self.deliveries = DeliveryManager(
            self.fleet, self.commands, self.survivors, self.geofence,
            self.events, self.confirmations, self.bus, self.settings,
            energy_model=self.energy_model,
        )
        self.dispatcher = TaskDispatcher(
            self.fleet, self.deliveries, self.survivors, self.bus, self.settings
        )
        self.dispatcher.enable_auto_dispatch(self.settings.auto_dispatch_deliveries)

        # --- realtime -------------------------------------------------------
        self.websockets = WebSocketManager()
        self.broadcaster = Broadcaster(
            self.bus, self.websockets, self.fleet, self.settings
        )

        # --- ancillary --------------------------------------------------------
        self.video = VideoService(self.settings.camera_config_file)
        self.health = SystemHealthService(
            self.fleet, self.telemetry, self.bus, self.settings
        )
        self.command_rate_limiter = SlidingWindowRateLimiter(
            limit=self.settings.rate_limit_commands_per_minute
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        if self._started:
            return
        logger.info(
            "container_starting",
            environment=self.settings.environment,
            drones=[d.drone_id for d in self.fleet_config.drones],
        )

        init_engine()

        # 1. Reconcile the configured fleet with the database.
        try:
            async with session_scope() as session:
                await self.fleet.sync_registry(session)
        except Exception as exc:
            # The GCS still starts: losing the database must not stop an
            # operator from seeing live aircraft. Persistence is degraded and
            # the health endpoint says so.
            logger.error(
                "drone_registry_sync_failed", error=str(exc), error_type=type(exc).__name__
            )

        # 2. Event bus, before any publisher exists.
        await self.bus.start()

        # 3. Wire subscribers.
        self.events.register(
            resolve_drone_uuid=self.fleet.drone_uuid,
            resolve_mission_uuid=_to_uuid,
        )
        self.broadcaster.register()
        self.dispatcher.register()
        self.dispatcher.set_system_principal(
            TokenPrincipal(
                operator_id=SYSTEM_OPERATOR_ID,
                username="system",
                role=OperatorRole.OPERATOR,
                token_id="system",
                expires_at=_far_future(),
            )
        )
        self.health.bind(websocket_manager=self.websockets, safety_engine=self.safety)

        # 4. Restore standing alerts from before a restart.
        await self.safety.load_active_from_db()

        # 5. Background loops.
        await self.broadcaster.start()
        await self.video.start()
        if self.settings.enable_safety_engine:
            await self.safety.start()
        await self.health.start()

        # 6. Real aircraft last, once every consumer is listening.
        if self.settings.enable_drone_connections:
            await self.connection_manager.start()
            self.telemetry.install()
        else:
            logger.warning(
                "drone_connections_disabled",
                detail="No link to any aircraft will be attempted in this process",
            )

        if not self.energy_model.is_calibrated:
            logger.warning(
                "delivery_energy_model_uncalibrated",
                detail=(
                    "Delivery dispatch is blocked: the battery cost of a delivery "
                    "has never been measured on this airframe. Run "
                    "scripts/calibrate_energy.py after the calibration flights."
                ),
                config_file=self.settings.energy_model_file,
            )

        self._started = True
        logger.info("container_started")

    async def shutdown(self) -> None:
        if not self._started:
            return
        logger.info("container_stopping")

        # Reverse order: stop talking to aircraft first, then the consumers.
        await self.connection_manager.stop()
        await self.deliveries.shutdown()
        await self.health.stop()
        await self.safety.stop()
        await self.video.stop()
        await self.broadcaster.stop()
        await self.websockets.disconnect_all()
        await self.bus.stop()
        await dispose_engine()

        self._started = False
        logger.info("container_stopped")

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "environment": self.settings.environment,
            "event_bus": self.bus.stats(),
            "websockets": self.websockets.stats(),
            "telemetry": self.telemetry.stats(),
            "safety": self.safety.status(),
            "dispatcher": self.dispatcher.status(),
            "video": self.video.summary(),
            "confirmations": self.confirmations.describe(),
            "energy_model": self.energy_model.describe(),
            "links": self.connection_manager.diagnostics(),
        }


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


def _far_future() -> Any:
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=3650)
