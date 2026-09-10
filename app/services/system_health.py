"""System health.

Feeds the dashboard health panel. Every component reports its *measured*
state: the database status comes from an actual query, the PX4 status from
actual link states, the MAVLink status from actual telemetry arrival times.

There is no path in this module that returns OK without having checked.
An unmeasurable component reports UNKNOWN.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.core.config import Settings
from app.core.enums import ComponentStatus, ConnectionState, TelemetryStatus
from app.core.logging import get_logger
from app.database.session import session_scope_optional
from app.models.event import SystemHealthSample
from app.realtime.event_bus import EventBus, EventType
from app.services.fleet_manager import FleetManager
from app.services.telemetry_service import TelemetryService

logger = get_logger(__name__)


@dataclass(slots=True)
class ComponentHealth:
    component: str
    status: ComponentStatus
    detail: str | None = None
    latency_ms: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": str(self.status),
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
            "metrics": self.metrics,
        }


class SystemHealthService:
    def __init__(
        self,
        fleet: FleetManager,
        telemetry: TelemetryService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._fleet = fleet
        self._telemetry = telemetry
        self._bus = bus
        self._settings = settings
        self._websocket_manager: Any = None
        self._safety: Any = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last: dict[str, ComponentHealth] = {}
        self._started_at = datetime.now(UTC)

    def bind(self, websocket_manager: Any = None, safety_engine: Any = None) -> None:
        """Late-bind components that are constructed after this service."""
        if websocket_manager is not None:
            self._websocket_manager = websocket_manager
        if safety_engine is not None:
            self._safety = safety_engine

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="system-health")
        logger.info("system_health_started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                report = await self.check_all()
                self._bus.emit(
                    EventType.SYSTEM_HEALTH_UPDATED, payload=report
                )
                await self._persist(report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("system_health_cycle_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(self._settings.system_health_interval_s)

    # ------------------------------------------------------------------
    # checks
    # ------------------------------------------------------------------
    async def check_all(self) -> dict[str, Any]:
        components = [
            await self._check_database(),
            self._check_px4(),
            self._check_mavlink(),
            self._check_drone_connections(),
            self._check_websocket(),
            self._check_network(),
            self._check_storage(),
            self._check_event_bus(),
            self._check_safety_engine(),
        ]
        self._last = {c.component: c for c in components}

        overall = ComponentStatus.OK
        if any(c.status is ComponentStatus.FAILED for c in components):
            overall = ComponentStatus.FAILED
        elif any(c.status is ComponentStatus.DEGRADED for c in components):
            overall = ComponentStatus.DEGRADED
        elif any(c.status is ComponentStatus.UNKNOWN for c in components):
            overall = ComponentStatus.DEGRADED

        return {
            "status": str(overall),
            "checked_at": datetime.now(UTC).isoformat(),
            "uptime_s": round((datetime.now(UTC) - self._started_at).total_seconds(), 1),
            "environment": self._settings.environment,
            "components": [c.as_dict() for c in components],
        }

    async def _check_database(self) -> ComponentHealth:
        start = time.perf_counter()
        async with session_scope_optional() as session:
            if session is None:
                return ComponentHealth(
                    "database", ComponentStatus.FAILED,
                    "Database is unreachable; mission recording is degraded",
                )
            try:
                await session.execute(text("SELECT 1"))
                postgis = await session.scalar(text("SELECT PostGIS_Version()"))
            except Exception as exc:
                return ComponentHealth(
                    "database", ComponentStatus.FAILED, f"Query failed: {exc}"
                )
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            "database",
            ComponentStatus.OK if latency < 500 else ComponentStatus.DEGRADED,
            None if latency < 500 else f"Query latency {latency:.0f} ms",
            latency_ms=latency,
            metrics={"postgis": str(postgis)},
        )

    def _check_px4(self) -> ComponentHealth:
        """PX4 across all aircraft: are the flight controllers healthy?"""
        states = list(self._fleet.states.values())
        if not states:
            return ComponentHealth(
                "px4", ComponentStatus.UNKNOWN, "No aircraft configured"
            )
        connected = [s for s in states if s.is_connected]
        if not connected:
            return ComponentHealth(
                "px4", ComponentStatus.FAILED,
                f"None of {len(states)} aircraft is connected",
                metrics={"connected": 0, "total": len(states)},
            )

        unhealthy = [
            s.drone_id
            for s in connected
            if s.health.value is not None and not s.health.value.all_ok
        ]
        no_health = [s.drone_id for s in connected if s.health.value is None]
        metrics = {
            "connected": len(connected),
            "total": len(states),
            "unhealthy": unhealthy,
            "no_health_telemetry": no_health,
        }
        if no_health:
            return ComponentHealth(
                "px4", ComponentStatus.DEGRADED,
                f"No health telemetry from {', '.join(no_health)}",
                metrics=metrics,
            )
        if unhealthy:
            return ComponentHealth(
                "px4", ComponentStatus.DEGRADED,
                f"Health checks failing on {', '.join(unhealthy)}",
                metrics=metrics,
            )
        if len(connected) < len(states):
            offline = [s.drone_id for s in states if not s.is_connected]
            return ComponentHealth(
                "px4", ComponentStatus.DEGRADED,
                f"Offline: {', '.join(offline)}",
                metrics=metrics,
            )
        return ComponentHealth("px4", ComponentStatus.OK, metrics=metrics)

    def _check_mavlink(self) -> ComponentHealth:
        """The MAVLink link itself: is telemetry actually arriving?"""
        states = list(self._fleet.states.values())
        connected = [s for s in states if s.is_connected]
        if not connected:
            return ComponentHealth(
                "mavlink", ComponentStatus.FAILED, "No live MAVLink link"
            )

        policy = self._fleet.policy
        stale = [
            s.drone_id
            for s in connected
            if policy.status("position", s.position) is not TelemetryStatus.FRESH
        ]
        ages = {
            s.drone_id: round(age, 2)
            for s in connected
            if (age := s.contact_age_s()) is not None
        }
        metrics = {"links": len(connected), "telemetry_age_s": ages, "stale": stale}

        if len(stale) == len(connected):
            return ComponentHealth(
                "mavlink", ComponentStatus.FAILED,
                "Telemetry is stale on every link", metrics=metrics,
            )
        if stale:
            return ComponentHealth(
                "mavlink", ComponentStatus.DEGRADED,
                f"Stale telemetry from {', '.join(stale)}", metrics=metrics,
            )
        return ComponentHealth("mavlink", ComponentStatus.OK, metrics=metrics)

    def _check_drone_connections(self) -> ComponentHealth:
        diagnostics = self._fleet.connection_manager.diagnostics()
        if not diagnostics:
            return ComponentHealth(
                "drone_connections", ComponentStatus.UNKNOWN,
                "Connection manager is not running",
            )
        by_state: dict[str, list[str]] = {}
        for entry in diagnostics:
            by_state.setdefault(entry["state"], []).append(entry["drone_id"])

        errored = by_state.get(str(ConnectionState.ERROR), [])
        disconnected = by_state.get(str(ConnectionState.DISCONNECTED), [])
        connected = by_state.get(str(ConnectionState.CONNECTED), [])
        metrics = {"by_state": by_state, "links": diagnostics}

        if errored:
            return ComponentHealth(
                "drone_connections", ComponentStatus.FAILED,
                f"Link error on {', '.join(errored)}", metrics=metrics,
            )
        if disconnected:
            return ComponentHealth(
                "drone_connections", ComponentStatus.DEGRADED,
                f"Disconnected: {', '.join(disconnected)}", metrics=metrics,
            )
        if not connected:
            return ComponentHealth(
                "drone_connections", ComponentStatus.FAILED,
                "No aircraft is connected", metrics=metrics,
            )
        return ComponentHealth("drone_connections", ComponentStatus.OK, metrics=metrics)

    def _check_websocket(self) -> ComponentHealth:
        if self._websocket_manager is None:
            return ComponentHealth(
                "websocket", ComponentStatus.UNKNOWN, "WebSocket manager not bound"
            )
        stats = self._websocket_manager.stats()
        status = ComponentStatus.OK
        detail = None
        if stats.get("send_failures", 0) > 0 and stats.get("clients", 0) == 0:
            status = ComponentStatus.DEGRADED
            detail = "Recent send failures and no connected clients"
        return ComponentHealth("websocket", status, detail, metrics=stats)

    def _check_network(self) -> ComponentHealth:
        """Local network health, inferred from the links we depend on.

        Deliberately does not probe the Internet: mission operation must not
        depend on it, so an Internet outage is not a GCS fault.
        """
        states = list(self._fleet.states.values())
        reconnects = sum(s.reconnect_attempts for s in states)
        connected = sum(1 for s in states if s.is_connected)
        metrics = {
            "local_links_up": connected,
            "configured_links": len(states),
            "total_reconnect_attempts": reconnects,
            "internet_required": False,
        }
        if states and connected == 0:
            return ComponentHealth(
                "network_local", ComponentStatus.FAILED,
                "No local radio link is up", metrics=metrics,
            )
        if reconnects > 0 and connected < len(states):
            return ComponentHealth(
                "network_local", ComponentStatus.DEGRADED,
                "Some links are reconnecting", metrics=metrics,
            )
        return ComponentHealth("network_local", ComponentStatus.OK, metrics=metrics)

    def _check_storage(self) -> ComponentHealth:
        try:
            usage = shutil.disk_usage(".")
        except OSError as exc:
            return ComponentHealth(
                "storage", ComponentStatus.UNKNOWN, f"Cannot read disk usage: {exc}"
            )
        free_pct = usage.free / usage.total * 100 if usage.total else 0.0
        metrics = {
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "free_percent": round(free_pct, 1),
        }
        if free_pct < 5:
            return ComponentHealth(
                "storage", ComponentStatus.FAILED,
                f"Only {free_pct:.1f}% disk free; logging will fail",
                metrics=metrics,
            )
        if free_pct < 15:
            return ComponentHealth(
                "storage", ComponentStatus.DEGRADED,
                f"{free_pct:.1f}% disk free", metrics=metrics,
            )
        return ComponentHealth("storage", ComponentStatus.OK, metrics=metrics)

    def _check_event_bus(self) -> ComponentHealth:
        stats = self._bus.stats()
        if not stats["running"]:
            return ComponentHealth(
                "event_bus", ComponentStatus.FAILED, "Event bus is not running",
                metrics=stats,
            )
        if stats["dropped"] > 0:
            return ComponentHealth(
                "event_bus", ComponentStatus.DEGRADED,
                f"{stats['dropped']} events dropped due to backpressure",
                metrics=stats,
            )
        return ComponentHealth("event_bus", ComponentStatus.OK, metrics=stats)

    def _check_safety_engine(self) -> ComponentHealth:
        if self._safety is None:
            return ComponentHealth(
                "safety_engine", ComponentStatus.UNKNOWN, "Safety engine not bound"
            )
        status = self._safety.status()
        if not status["running"]:
            return ComponentHealth(
                "safety_engine", ComponentStatus.FAILED,
                "Safety engine is not running", metrics=status,
            )
        return ComponentHealth("safety_engine", ComponentStatus.OK, metrics=status)

    # ------------------------------------------------------------------
    # persistence / access
    # ------------------------------------------------------------------
    async def _persist(self, report: dict[str, Any]) -> None:
        async with session_scope_optional() as session:
            if session is None:
                return
            now = datetime.now(UTC)
            for component in report["components"]:
                session.add(
                    SystemHealthSample(
                        sampled_at=now,
                        component=component["component"],
                        status=ComponentStatus(component["status"]),
                        latency_ms=component["latency_ms"],
                        detail=component["detail"],
                        metrics=component["metrics"],
                    )
                )

    def last_report(self) -> dict[str, Any] | None:
        if not self._last:
            return None
        return {
            "components": [c.as_dict() for c in self._last.values()],
            "cached": True,
        }
