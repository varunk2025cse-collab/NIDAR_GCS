"""Shared test fixtures.

Unit tests run without a database and without any aircraft. Where a service
would persist something, ``session_scope_optional`` yields ``None`` and the
service degrades to log-only -- which is the same path the real backend takes
during a database outage, so these tests exercise it too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest

from app.core.config import (
    ConnectionConfig,
    DroneConfig,
    DroneRole,
    FleetConfig,
    Settings,
    TelemetryRatesConfig,
)
from app.core.energy import CalibrationRecord, EnergyModelConfig
from app.core.freshness import FreshnessPolicy
from app.drone.connection import DroneConnection
from app.drone.manager import DroneConnectionManager
from app.drone.state import DroneState
from app.realtime.event_bus import Event, EventBus
from app.services.command_service import CommandService
from app.services.delivery_confirmation import ConfirmationRegistry
from app.services.delivery_manager import DeliveryManager
from app.services.event_service import EventService
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService
from app.services.safety_engine import SafetyEngine
from app.services.survivor_manager import SurvivorManager
from app.services.task_dispatcher import TaskDispatcher
from tests.fakes import FakeAdapterRegistry


@pytest.fixture(autouse=True)
async def _isolate_database(settings: Settings) -> AsyncIterator[None]:
    """Point the global session factory at an unreachable database.

    Unit tests must never touch a real one. This also means every service runs
    through the same "database unavailable" path the field backend takes during
    an outage, so that degradation is exercised on every test rather than only
    when something breaks.
    """
    import app.core.config as config_module
    import app.database.session as session_module

    def no_database() -> None:
        raise RuntimeError("unit tests run without a database by design")

    config_module.get_settings.cache_clear()
    original_settings = config_module.get_settings
    original_factory = session_module.get_sessionmaker

    config_module.get_settings = lambda: settings  # type: ignore[assignment]
    session_module.get_settings = lambda: settings  # type: ignore[assignment]
    session_module.get_sessionmaker = no_database  # type: ignore[assignment]
    try:
        yield
    finally:
        session_module.get_sessionmaker = original_factory  # type: ignore[assignment]
        config_module.get_settings = original_settings  # type: ignore[assignment]
        session_module.get_settings = original_settings  # type: ignore[assignment]
        config_module.get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Settings tuned for fast, deterministic tests.

    Only timings are shortened; every threshold that governs a safety decision
    keeps its production default so the tests exercise the real rules.
    """
    return Settings(
        environment="development",
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/none",
        secret_key="test-secret-key-not-for-flight",
        enable_drone_connections=False,
        enable_safety_engine=False,
        safety_engine_interval_s=0.05,
        fleet_broadcast_interval_s=0.05,
        telemetry_persist_interval_s=0.05,
        connection=ConnectionConfig(
            heartbeat_degraded_s=0.3,
            heartbeat_lost_s=0.8,
            connect_timeout_s=2.0,
            identity_probe_timeout_s=1.0,
            reconnect_initial_delay_s=0.05,
            reconnect_max_delay_s=0.2,
            # The passive pymavlink probe needs a real socket; unit tests
            # exercise the identity logic directly instead.
            require_identity_probe=False,
        ),
        telemetry_rates=TelemetryRatesConfig(),
    )


@pytest.fixture
def fleet_config() -> FleetConfig:
    return FleetConfig(
        drones=[
            DroneConfig(
                drone_id="D1",
                name="Scout One",
                role=DroneRole.SCOUT,
                system_id=1,
                connection_endpoint="udpin://0.0.0.0:14541",
                mavsdk_server_port=50051,
            ),
            DroneConfig(
                drone_id="D2",
                name="Scout Two",
                role=DroneRole.SCOUT,
                system_id=2,
                connection_endpoint="udpin://0.0.0.0:14542",
                mavsdk_server_port=50052,
            ),
            DroneConfig(
                drone_id="D3",
                name="Delivery One",
                role=DroneRole.DELIVERY,
                system_id=3,
                connection_endpoint="udpin://0.0.0.0:14543",
                mavsdk_server_port=50053,
                payload_capacity_g=1500,
            ),
        ]
    )


@pytest.fixture
def energy_model() -> EnergyModelConfig:
    """A *calibrated* energy model, so feasibility maths can be tested.

    Real deployments start uncalibrated and delivery dispatch is blocked; that
    behaviour has its own tests. This fixture stands in for an airframe that
    has actually been measured.
    """
    return EnergyModelConfig(
        calibration=CalibrationRecord(
            calibrated=True,
            calibrated_on=date(2026, 9, 1),
            calibrated_by="test-fixture",
            airframe="TEST-AIRFRAME",
            method="synthetic coefficients for deterministic tests",
            sample_count=6,
        )
    )


@pytest.fixture
def uncalibrated_energy_model() -> EnergyModelConfig:
    """The default state of a system nobody has measured yet."""
    return EnergyModelConfig()


@pytest.fixture
def adapters() -> FakeAdapterRegistry:
    return FakeAdapterRegistry()


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    event_bus = EventBus()
    await event_bus.start()
    try:
        yield event_bus
    finally:
        await event_bus.stop()


@pytest.fixture
def collected_events(bus: EventBus) -> list[Event]:
    """Every event published during a test, in order."""
    captured: list[Event] = []

    async def collect(event: Event) -> None:
        captured.append(event)

    bus.on_any(collect)
    return captured


@pytest.fixture
async def connection_manager(
    settings: Settings,
    fleet_config: FleetConfig,
    bus: EventBus,
    adapters: FakeAdapterRegistry,
) -> AsyncIterator[DroneConnectionManager]:
    manager = DroneConnectionManager(
        fleet_config=fleet_config,
        connection_config=settings.connection,
        telemetry_rates=settings.telemetry_rates,
        event_bus=bus,
        adapter_factory=adapters,
    )
    try:
        yield manager
    finally:
        await manager.stop()


@pytest.fixture
async def live_fleet(
    connection_manager: DroneConnectionManager,
    settings: Settings,
    fleet_config: FleetConfig,
    adapters: FakeAdapterRegistry,
) -> AsyncIterator[FleetManager]:
    """A fleet with all three links up and telemetry flowing."""
    await connection_manager.start()
    fleet = FleetManager(connection_manager, fleet_config, settings)
    await _wait_for_telemetry(connection_manager)
    yield fleet


async def _wait_for_telemetry(
    manager: DroneConnectionManager, timeout_s: float = 3.0
) -> None:
    """Block until every configured link has produced real telemetry."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        states = manager.states.values()
        if states and all(
            s.position.value is not None and s.battery.value is not None for s in states
        ):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Fake links did not produce telemetry within the timeout")


@pytest.fixture
def offline_fleet(
    connection_manager: DroneConnectionManager,
    settings: Settings,
    fleet_config: FleetConfig,
) -> FleetManager:
    """A fleet whose links were never started -- nothing is connected."""
    return FleetManager(connection_manager, fleet_config, settings)


@pytest.fixture
def policy(settings: Settings) -> FreshnessPolicy:
    return FreshnessPolicy(settings.freshness)


@pytest.fixture
def events_service(bus: EventBus) -> EventService:
    return EventService(bus)


@pytest.fixture
def geofence_service(settings: Settings) -> GeofenceService:
    return GeofenceService(settings)


@pytest.fixture
def command_service(
    live_fleet: FleetManager,
    events_service: EventService,
    bus: EventBus,
    settings: Settings,
) -> CommandService:
    return CommandService(live_fleet, events_service, bus, settings)


@pytest.fixture
def safety_engine(
    live_fleet: FleetManager,
    geofence_service: GeofenceService,
    bus: EventBus,
    settings: Settings,
) -> SafetyEngine:
    return SafetyEngine(live_fleet, geofence_service, bus, settings)


@pytest.fixture
def survivor_manager(
    live_fleet: FleetManager,
    events_service: EventService,
    bus: EventBus,
    settings: Settings,
) -> SurvivorManager:
    return SurvivorManager(live_fleet, events_service, bus, settings)


@pytest.fixture
def delivery_manager(
    live_fleet: FleetManager,
    command_service: CommandService,
    survivor_manager: SurvivorManager,
    geofence_service: GeofenceService,
    events_service: EventService,
    bus: EventBus,
    settings: Settings,
    energy_model: EnergyModelConfig,
) -> DeliveryManager:
    return DeliveryManager(
        live_fleet,
        command_service,
        survivor_manager,
        geofence_service,
        events_service,
        ConfirmationRegistry(
            __import__(
                "app.core.enums", fromlist=["DeliveryConfirmationSource"]
            ).DeliveryConfirmationSource.OPERATOR
        ),
        bus,
        settings,
        energy_model=energy_model,
    )


@pytest.fixture
def dispatcher(
    live_fleet: FleetManager,
    delivery_manager: DeliveryManager,
    survivor_manager: SurvivorManager,
    bus: EventBus,
    settings: Settings,
) -> TaskDispatcher:
    return TaskDispatcher(live_fleet, delivery_manager, survivor_manager, bus, settings)


@pytest.fixture
def d1_connection(connection_manager: DroneConnectionManager) -> DroneConnection:
    return connection_manager.get("D1")


@pytest.fixture
def d1_state(live_fleet: FleetManager) -> DroneState:
    return live_fleet.state("D1")


@pytest.fixture
def d3_state(live_fleet: FleetManager) -> DroneState:
    return live_fleet.state("D3")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a live PostgreSQL+PostGIS database"
    )
    config.addinivalue_line(
        "markers", "hardware: requires real PX4 hardware; never run in CI"
    )


@pytest.fixture(autouse=True)
def _fail_on_hardware_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    """Hardware tests are opt-in only.

    Running them by accident would send commands to whatever is on the
    configured endpoints.
    """
    if request.node.get_closest_marker("hardware") and not request.config.getoption(
        "--run-hardware", default=False
    ):
        pytest.skip("hardware test: pass --run-hardware to enable")
    yield


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hardware", "real PX4 hardware validation")
    group.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help="Run tests that talk to real PX4 hardware",
    )
    group.addoption(
        "--stage3",
        action="store_true",
        default=False,
        help="Stage 3: send acknowledgement-only commands. Propellers removed.",
    )
    group.addoption(
        "--stage4",
        action="store_true",
        default=False,
        help=(
            "Stage 4: arm and disarm a real aircraft. Airframe secured and "
            "propellers removed."
        ),
    )
    group.addoption(
        "--interactive",
        action="store_true",
        default=False,
        help="Run hardware tests that need an operator to act (e.g. power an aircraft down)",
    )
