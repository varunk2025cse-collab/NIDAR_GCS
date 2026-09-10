"""Preflight validation.

Queries the *real* aircraft and the mission plan, and produces a per-check
report. A mission cannot start while any check is FAIL.

There is deliberately no override parameter on this path. If a check must be
waived, that is a decision to change the configured threshold or fix the
aircraft -- not a flag on the start request. Anything else turns a safety gate
into a dialog box people learn to click through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DroneRole, Settings
from app.core.enums import CheckStatus, ConnectionState, MissionState, TelemetryStatus
from app.core.logging import get_logger
from app.drone.state import DroneState
from app.models.mission import Mission
from app.models.search_sector import SearchSector, Waypoint
from app.services.fleet_manager import FleetManager
from app.services.geofence_service import GeofenceService

logger = get_logger(__name__)


@dataclass(slots=True)
class CheckResult:
    check: str
    status: CheckStatus
    drone: str | None = None
    reason: str | None = None
    observed: Any = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"check": self.check, "status": str(self.status)}
        if self.drone:
            payload["drone"] = self.drone
        if self.reason:
            payload["reason"] = self.reason
        if self.observed is not None:
            payload["observed"] = self.observed
        return payload


@dataclass(slots=True)
class PreflightReport:
    mission_id: uuid.UUID
    checks: list[CheckResult] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.WARN]

    @property
    def ready(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "mission_id": str(self.mission_id),
            "generated_at": self.generated_at.isoformat(),
            "failure_count": len(self.failures),
            "warning_count": len(self.warnings),
            "checks": [c.as_dict() for c in self.checks],
        }


class PreflightService:
    def __init__(
        self,
        fleet: FleetManager,
        geofence: GeofenceService,
        settings: Settings,
        energy_model: Any | None = None,
    ) -> None:
        self._fleet = fleet
        self._geofence = geofence
        self._settings = settings
        self._policy = fleet.policy
        self._energy = energy_model

    async def run(
        self, session: AsyncSession, mission: Mission, drone_ids: list[str]
    ) -> PreflightReport:
        report = PreflightReport(mission_id=mission.id)

        await self._check_mission_configuration(session, mission, report)
        await self._check_conflicting_missions(session, mission, report)
        self._check_geofence_available(mission, report)
        await self._check_plan(session, mission, drone_ids, report)

        for drone_id in drone_ids:
            self._check_drone(mission, drone_id, report)

        self._check_role_coverage(drone_ids, report)

        logger.info(
            "preflight_completed",
            mission_id=str(mission.id),
            ready=report.ready,
            failures=[c.check for c in report.failures],
            warnings=[c.check for c in report.warnings],
        )
        return report

    # ------------------------------------------------------------------
    # mission-level checks
    # ------------------------------------------------------------------
    async def _check_mission_configuration(
        self, session: AsyncSession, mission: Mission, report: PreflightReport
    ) -> None:
        if mission.state not in (MissionState.READY, MissionState.PRECHECK, MissionState.DRAFT):
            report.checks.append(
                CheckResult(
                    "MISSION_STATE",
                    CheckStatus.FAIL,
                    reason=f"Mission is in state {mission.state}; it cannot be started",
                    observed=str(mission.state),
                )
            )
        else:
            report.checks.append(
                CheckResult("MISSION_STATE", CheckStatus.PASS, observed=str(mission.state))
            )

        if mission.launch_point is None:
            report.checks.append(
                CheckResult(
                    "LAUNCH_POINT",
                    CheckStatus.FAIL,
                    reason="No launch point has been surveyed for this mission",
                )
            )
        else:
            report.checks.append(CheckResult("LAUNCH_POINT", CheckStatus.PASS))

        if not mission.search_altitude_m or mission.search_altitude_m <= 0:
            report.checks.append(
                CheckResult(
                    "MISSION_CONFIGURATION",
                    CheckStatus.FAIL,
                    reason="Search altitude is not configured",
                    observed=mission.search_altitude_m,
                )
            )
        else:
            report.checks.append(
                CheckResult(
                    "MISSION_CONFIGURATION",
                    CheckStatus.PASS,
                    observed={
                        "search_altitude_m": mission.search_altitude_m,
                        "delivery_altitude_m": mission.delivery_altitude_m,
                    },
                )
            )

        max_duration = mission.max_duration_s or self._settings.safety.mission_max_duration_s
        if max_duration <= 0:
            report.checks.append(
                CheckResult(
                    "MISSION_DURATION",
                    CheckStatus.FAIL,
                    reason="Mission has no maximum duration",
                )
            )
        else:
            report.checks.append(
                CheckResult("MISSION_DURATION", CheckStatus.PASS, observed=max_duration)
            )

    async def _check_conflicting_missions(
        self, session: AsyncSession, mission: Mission, report: PreflightReport
    ) -> None:
        from app.core.enums import MISSION_ACTIVE_STATES

        result = await session.execute(
            select(Mission.id, Mission.name, Mission.state).where(
                Mission.id != mission.id,
                Mission.state.in_(list(MISSION_ACTIVE_STATES)),
            )
        )
        conflicts = [
            {"mission_id": str(row.id), "name": row.name, "state": str(row.state)}
            for row in result.all()
        ]
        if conflicts:
            report.checks.append(
                CheckResult(
                    "NO_CONFLICTING_MISSION",
                    CheckStatus.FAIL,
                    reason="Another mission is already active",
                    observed=conflicts,
                )
            )
        else:
            report.checks.append(CheckResult("NO_CONFLICTING_MISSION", CheckStatus.PASS))

    def _check_geofence_available(self, mission: Mission, report: PreflightReport) -> None:
        if self._geofence.has_fences(mission.id):
            report.checks.append(
                CheckResult(
                    "GEOFENCE",
                    CheckStatus.PASS,
                    observed=len(self._geofence.fences_for(mission.id)),
                )
            )
        else:
            report.checks.append(
                CheckResult(
                    "GEOFENCE",
                    CheckStatus.FAIL,
                    reason="No geofence is attached to this mission",
                )
            )

    async def _check_plan(
        self,
        session: AsyncSession,
        mission: Mission,
        drone_ids: list[str],
        report: PreflightReport,
    ) -> None:
        sector_count = await session.scalar(
            select(func.count(SearchSector.id)).where(SearchSector.mission_id == mission.id)
        )
        if not sector_count:
            report.checks.append(
                CheckResult(
                    "SEARCH_PLAN",
                    CheckStatus.FAIL,
                    reason="Mission has no search sectors",
                )
            )
            return

        assigned = await session.scalar(
            select(func.count(SearchSector.id)).where(
                SearchSector.mission_id == mission.id,
                SearchSector.assigned_drone_uuid.isnot(None),
            )
        )
        if not assigned:
            report.checks.append(
                CheckResult(
                    "SEARCH_PLAN",
                    CheckStatus.FAIL,
                    reason=f"None of the {sector_count} sectors is assigned to an aircraft",
                    observed={"sectors": sector_count, "assigned": 0},
                )
            )
            return

        waypoint_count = await session.scalar(
            select(func.count(Waypoint.id))
            .join(SearchSector, SearchSector.id == Waypoint.sector_id)
            .where(SearchSector.mission_id == mission.id)
        )
        if not waypoint_count:
            report.checks.append(
                CheckResult(
                    "SEARCH_PLAN",
                    CheckStatus.FAIL,
                    reason="Assigned sectors have no waypoints to upload",
                    observed={"sectors": sector_count, "assigned": assigned},
                )
            )
            return

        report.checks.append(
            CheckResult(
                "SEARCH_PLAN",
                CheckStatus.PASS if assigned == sector_count else CheckStatus.WARN,
                reason=(
                    None
                    if assigned == sector_count
                    else f"{sector_count - assigned} sector(s) are still unassigned"
                ),
                observed={
                    "sectors": sector_count,
                    "assigned": assigned,
                    "waypoints": waypoint_count,
                },
            )
        )

    def _check_role_coverage(self, drone_ids: list[str], report: PreflightReport) -> None:
        roles = [
            self._fleet.state(d).role
            for d in drone_ids
            if self._fleet.connection_manager.has(d)
        ]
        if not any(r is DroneRole.SCOUT for r in roles):
            report.checks.append(
                CheckResult(
                    "ROLE_COVERAGE",
                    CheckStatus.FAIL,
                    reason="No scout aircraft is assigned to this mission",
                )
            )
        elif not any(r is DroneRole.DELIVERY for r in roles):
            report.checks.append(
                CheckResult(
                    "ROLE_COVERAGE",
                    CheckStatus.WARN,
                    reason=(
                        "No delivery aircraft is assigned; survivors can be found "
                        "but nothing can be delivered"
                    ),
                )
            )
        else:
            report.checks.append(
                CheckResult(
                    "ROLE_COVERAGE",
                    CheckStatus.PASS,
                    observed={str(r): roles.count(r) for r in set(roles)},
                )
            )

    # ------------------------------------------------------------------
    # per-aircraft checks, against real telemetry
    # ------------------------------------------------------------------
    def _check_drone(
        self, mission: Mission, drone_id: str, report: PreflightReport
    ) -> None:
        if not self._fleet.connection_manager.has(drone_id):
            report.checks.append(
                CheckResult(
                    "CONNECTION",
                    CheckStatus.FAIL,
                    drone=drone_id,
                    reason="Drone is not part of the configured fleet",
                )
            )
            return

        state = self._fleet.state(drone_id)

        # -- link ----------------------------------------------------------
        if state.connection_state is ConnectionState.CONNECTED:
            report.checks.append(
                CheckResult("CONNECTION", CheckStatus.PASS, drone=drone_id,
                            observed=str(state.connection_state))
            )
        else:
            report.checks.append(
                CheckResult(
                    "CONNECTION",
                    CheckStatus.FAIL,
                    drone=drone_id,
                    reason=f"Link state is {state.connection_state}",
                    observed=str(state.connection_state),
                )
            )
            # No live link means nothing below can be evaluated honestly.
            for check in ("GPS", "BATTERY", "HEALTH", "HOME_POSITION", "TELEMETRY"):
                report.checks.append(
                    CheckResult(
                        check,
                        CheckStatus.UNKNOWN,
                        drone=drone_id,
                        reason="No telemetry: aircraft is not connected",
                    )
                )
            return

        # -- identity ------------------------------------------------------
        report.checks.append(
            CheckResult(
                "IDENTITY",
                CheckStatus.PASS if state.identity_verified else CheckStatus.FAIL,
                drone=drone_id,
                reason=None if state.identity_verified else "MAVLink identity is unverified",
                observed=(
                    {
                        "system_id": state.identity.system_id,
                        "hardware_uid": state.identity.hardware_uid,
                    }
                    if state.identity
                    else None
                ),
            )
        )

        # -- telemetry freshness --------------------------------------------
        age = state.contact_age_s()
        limit = self._settings.safety.command_requires_telemetry_age_s
        report.checks.append(
            CheckResult(
                "TELEMETRY",
                CheckStatus.PASS if age is not None and age <= limit else CheckStatus.FAIL,
                drone=drone_id,
                reason=(
                    None
                    if age is not None and age <= limit
                    else f"Telemetry age {age}s exceeds the {limit}s limit"
                ),
                observed=round(age, 2) if age is not None else None,
            )
        )

        # -- GPS -------------------------------------------------------------
        gps = state.gps.value
        thresholds = self._settings.safety
        if gps is None:
            report.checks.append(
                CheckResult("GPS", CheckStatus.FAIL, drone=drone_id,
                            reason="No GPS telemetry")
            )
        elif gps.fix_type < thresholds.min_gps_fix_type:
            report.checks.append(
                CheckResult(
                    "GPS", CheckStatus.FAIL, drone=drone_id,
                    reason=f"Fix is {gps.fix_type_name}, a 3D fix is required",
                    observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
                )
            )
        elif gps.satellites < thresholds.min_satellites:
            report.checks.append(
                CheckResult(
                    "GPS", CheckStatus.FAIL, drone=drone_id,
                    reason=(
                        f"{gps.satellites} satellites, minimum is {thresholds.min_satellites}"
                    ),
                    observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
                )
            )
        else:
            report.checks.append(
                CheckResult(
                    "GPS", CheckStatus.PASS, drone=drone_id,
                    observed={"fix": gps.fix_type_name, "satellites": gps.satellites},
                )
            )

        # -- battery ---------------------------------------------------------
        report.checks.append(self._battery_check(state, drone_id))

        # -- health / flight controller --------------------------------------
        health = state.health.value
        if health is None:
            report.checks.append(
                CheckResult("HEALTH", CheckStatus.FAIL, drone=drone_id,
                            reason="No health telemetry from the flight controller")
            )
            report.checks.append(
                CheckResult("HOME_POSITION", CheckStatus.UNKNOWN, drone=drone_id,
                            reason="No health telemetry")
            )
        else:
            report.checks.append(
                CheckResult(
                    "HOME_POSITION",
                    CheckStatus.PASS if health.home_position_ok else CheckStatus.FAIL,
                    drone=drone_id,
                    reason=None if health.home_position_ok else "Home position is not set",
                )
            )
            failing = [k for k, v in health.as_dict().items() if v is False and k != "all_ok"]
            if health.armable and not failing:
                report.checks.append(
                    CheckResult("HEALTH", CheckStatus.PASS, drone=drone_id,
                                observed=health.as_dict())
                )
            elif not health.armable:
                report.checks.append(
                    CheckResult(
                        "HEALTH", CheckStatus.FAIL, drone=drone_id,
                        reason=f"Flight controller reports not armable ({', '.join(failing)})",
                        observed=health.as_dict(),
                    )
                )
            else:
                report.checks.append(
                    CheckResult(
                        "HEALTH", CheckStatus.WARN, drone=drone_id,
                        reason=f"Non-critical health items failing: {', '.join(failing)}",
                        observed=health.as_dict(),
                    )
                )

        # -- flight controller status ----------------------------------------
        if state.armed.value is True:
            report.checks.append(
                CheckResult(
                    "FLIGHT_CONTROLLER",
                    CheckStatus.FAIL,
                    drone=drone_id,
                    reason="Aircraft is already armed before mission start",
                    observed={"armed": True, "in_air": state.in_air.value},
                )
            )
        else:
            report.checks.append(
                CheckResult(
                    "FLIGHT_CONTROLLER",
                    CheckStatus.PASS,
                    drone=drone_id,
                    observed={
                        "armed": state.armed.value,
                        "flight_mode": state.flight_mode.value,
                        "firmware": (
                            state.identity.firmware_version if state.identity else None
                        ),
                    },
                )
            )

        # -- payload and energy model (delivery aircraft only) -------------------
        if state.role is DroneRole.DELIVERY:
            report.checks.append(self._payload_check(state, drone_id))
            report.checks.append(self._energy_model_check(drone_id))

        # -- position inside the geofence ---------------------------------------
        position = state.position.value
        if position is not None and self._geofence.has_fences(mission.id):
            evaluation = self._geofence.evaluate(
                mission.id, position.latitude, position.longitude
            )
            from app.core.enums import GeofenceStatus

            report.checks.append(
                CheckResult(
                    "INSIDE_GEOFENCE",
                    CheckStatus.FAIL
                    if evaluation.status is GeofenceStatus.BREACHED
                    else CheckStatus.PASS,
                    drone=drone_id,
                    reason=(
                        f"Aircraft is outside {', '.join(evaluation.breached_fences)}"
                        if evaluation.status is GeofenceStatus.BREACHED
                        else None
                    ),
                    observed=str(evaluation.status),
                )
            )

    def _battery_check(self, state: DroneState, drone_id: str) -> CheckResult:
        minimum = self._settings.safety.battery_min_for_takeoff_pct
        status = self._policy.status("battery", state.battery)
        battery = state.battery.value
        if battery is None or status is TelemetryStatus.NO_DATA:
            return CheckResult(
                "BATTERY", CheckStatus.FAIL, drone=drone_id,
                reason="No battery telemetry available",
            )
        if battery.remaining_percent is None:
            return CheckResult(
                "BATTERY", CheckStatus.FAIL, drone=drone_id,
                reason="Autopilot is not reporting a battery percentage",
                observed={"voltage_v": battery.voltage_v},
            )
        if battery.remaining_percent < minimum:
            return CheckResult(
                "BATTERY", CheckStatus.FAIL, drone=drone_id,
                reason=(
                    f"Battery {battery.remaining_percent:.0f}% is below the "
                    f"{minimum:.0f}% required mission reserve"
                ),
                observed=battery.remaining_percent,
            )
        return CheckResult(
            "BATTERY",
            CheckStatus.WARN if status is TelemetryStatus.STALE else CheckStatus.PASS,
            drone=drone_id,
            reason="Battery telemetry is stale" if status is TelemetryStatus.STALE else None,
            observed=battery.remaining_percent,
        )

    def _energy_model_check(self, drone_id: str) -> CheckResult:
        """Whether this aircraft can be dispatched on a delivery at all.

        Reported at preflight rather than only at dispatch, so an operator
        learns the delivery aircraft is unusable before takeoff instead of
        after a survivor has been found.
        """
        if self._energy is None:
            return CheckResult(
                "DELIVERY_ENERGY_MODEL",
                CheckStatus.UNKNOWN,
                drone=drone_id,
                reason="No energy model was supplied to the preflight service",
            )
        if not self._energy.is_calibrated:
            return CheckResult(
                "DELIVERY_ENERGY_MODEL",
                CheckStatus.WARN,
                drone=drone_id,
                reason=(
                    "Energy model is UNCALIBRATED. The mission can fly and search, "
                    "but delivery dispatch will be refused until the battery cost "
                    "of a delivery is measured on this airframe."
                ),
                observed=self._energy.describe(),
            )
        return CheckResult(
            "DELIVERY_ENERGY_MODEL",
            CheckStatus.PASS,
            drone=drone_id,
            observed={
                "calibrated_on": (
                    self._energy.calibration.calibrated_on.isoformat()
                    if self._energy.calibration.calibrated_on
                    else None
                ),
                "airframe": self._energy.calibration.airframe,
                "sample_count": self._energy.calibration.sample_count,
            },
        )

    def _payload_check(self, state: DroneState, drone_id: str) -> CheckResult:
        """Payload readiness for a delivery aircraft.

        Readiness is asserted by the payload subsystem through the delivery
        confirmation provider. Until a real payload sensor reports in, this is
        reported as UNKNOWN rather than assumed ready -- an assumed-loaded
        aircraft that flies empty is a wasted sortie over a survivor.
        """
        config = self._fleet.drone_config(drone_id)
        if config is None or config.payload_capacity_g is None:
            return CheckResult(
                "PAYLOAD",
                CheckStatus.WARN,
                drone=drone_id,
                reason="No payload capacity is configured for this aircraft",
            )
        return CheckResult(
            "PAYLOAD",
            CheckStatus.UNKNOWN,
            drone=drone_id,
            reason=(
                "Payload load state must be confirmed by the operator or the payload "
                "sensor before dispatch"
            ),
            observed={"capacity_g": config.payload_capacity_g},
        )
