"""Delivery feasibility.

The question this answers is whether the aircraft can get there, hover,
release and come home with the configured reserve intact. Getting it wrong in
the optimistic direction strands an aircraft; these tests pin the pessimistic
behaviour in place.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.core.energy import CalibrationRecord, EnergyModelConfig
from app.core.enums import CheckStatus, DeliveryConfirmationSource
from app.core.geo import destination_point
from app.drone.types import Battery, GpsInfo, HealthReport
from app.services.delivery_confirmation import ConfirmationRegistry


def _target_at(state, metres: float, bearing: float = 90.0) -> tuple[float, float]:
    position = state.position.value
    point = destination_point(position.latitude, position.longitude, bearing, metres)
    return point.latitude, point.longitude


async def test_nearby_target_with_full_battery_is_safe(
    delivery_manager, live_fleet, d3_state
) -> None:
    latitude, longitude = _target_at(d3_state, 300)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is True
    assert report.distance_m == pytest.approx(300, abs=5)
    assert report.battery_after_pct is not None
    assert report.battery_after_pct > 25


async def test_target_beyond_the_configured_range_is_refused(
    delivery_manager, live_fleet, d3_state, settings
) -> None:
    latitude, longitude = _target_at(d3_state, settings.safety.max_delivery_distance_m + 500)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is False
    assert "DISTANCE" in {c.name for c in report.failures}


async def test_round_trip_must_leave_the_configured_reserve(
    delivery_manager, live_fleet, d3_state, adapters, settings
) -> None:
    """The case that matters: enough battery to reach the survivor, not enough
    to come back. Sending the aircraft would strand it."""
    # 46% is above the 45% dispatch minimum, so this isolates the return-trip
    # check rather than tripping the simpler battery floor.
    adapters.get("D3").current_battery = Battery(46.0, 21.8, 6.0)
    await _settle(live_fleet, "D3", lambda s: s.battery.value.remaining_percent == 46.0)

    # 2.9 km each way: 5.8 km round trip at 4%/km is ~23%, plus ~1% hover,
    # leaving ~22% against a required 25% reserve.
    latitude, longitude = _target_at(d3_state, 2900)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is False
    failures = {c.name for c in report.failures}
    assert "RETURN_CAPABILITY" in failures
    detail = next(c for c in report.checks if c.name == "RETURN_CAPABILITY")
    assert detail.observed["required_reserve_pct"] == settings.safety.battery_reserve_pct
    assert detail.observed["remaining_after_pct"] < settings.safety.battery_reserve_pct


async def test_battery_below_dispatch_minimum_is_refused(
    delivery_manager, live_fleet, d3_state, adapters
) -> None:
    adapters.get("D3").current_battery = Battery(30.0, 21.0, 6.0)
    await _settle(live_fleet, "D3", lambda s: s.battery.value.remaining_percent == 30.0)

    latitude, longitude = _target_at(d3_state, 200)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is False
    assert "BATTERY" in {c.name for c in report.failures}


async def test_unknown_battery_is_treated_as_unsafe(
    delivery_manager, live_fleet, d3_state, adapters
) -> None:
    """An unknown battery must never be assumed to be a good one."""
    adapters.get("D3").current_battery = Battery(None, 22.0, None)
    await _settle(
        live_fleet, "D3", lambda s: s.battery.value.remaining_percent is None
    )

    latitude, longitude = _target_at(d3_state, 200)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is False
    failures = {c.name for c in report.failures}
    assert "BATTERY" in failures
    assert "RETURN_CAPABILITY" in failures


async def test_no_position_telemetry_is_unsafe(
    delivery_manager, live_fleet, d3_state, adapters
) -> None:
    adapters.make_unreachable("D3")
    await asyncio.sleep(1.3)

    report = delivery_manager.evaluate_safety(d3_state, 11.24, 77.13)
    assert report.safe is False
    assert "CURRENT_LOCATION" in {c.name for c in report.failures}
    assert report.distance_m is None


async def test_poor_gps_blocks_dispatch(
    delivery_manager, live_fleet, d3_state, adapters
) -> None:
    adapters.get("D3").current_gps = GpsInfo(2, "FIX_2D", 5)
    await _settle(live_fleet, "D3", lambda s: s.gps.value.fix_type == 2)

    latitude, longitude = _target_at(d3_state, 200)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)
    assert report.safe is False
    assert "GPS" in {c.name for c in report.failures}


async def test_unhealthy_position_estimate_blocks_dispatch(
    delivery_manager, live_fleet, d3_state, adapters
) -> None:
    adapters.get("D3").current_health = HealthReport(
        gyrometer_calibration_ok=True,
        accelerometer_calibration_ok=True,
        magnetometer_calibration_ok=True,
        local_position_ok=True,
        global_position_ok=False,
        home_position_ok=True,
        armable=True,
    )
    await _settle(live_fleet, "D3", lambda s: s.health.value.global_position_ok is False)

    latitude, longitude = _target_at(d3_state, 200)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)
    assert report.safe is False
    assert "HEALTH" in {c.name for c in report.failures}


async def test_aircraft_already_on_a_task_is_unavailable(
    delivery_manager, live_fleet, d3_state
) -> None:
    d3_state.delivery_task_id = "existing-task"
    latitude, longitude = _target_at(d3_state, 200)

    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)
    assert report.safe is False
    assert "AIRCRAFT_AVAILABLE" in {c.name for c in report.failures}


async def test_report_shows_its_working(delivery_manager, live_fleet, d3_state) -> None:
    """An operator refused a delivery needs the numbers, not just a refusal."""
    latitude, longitude = _target_at(d3_state, 1000)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)
    payload = report.as_dict()

    assert payload["distance_m"] == pytest.approx(1000, abs=10)
    assert payload["estimated_duration_s"] is not None
    assert payload["estimated_battery_cost_pct"] is not None
    assert payload["battery_after_pct"] is not None
    assert {c["check"] for c in payload["checks"]} >= {
        "CONNECTION",
        "TELEMETRY",
        "AIRCRAFT_AVAILABLE",
        "CURRENT_LOCATION",
        "DISTANCE",
        "GPS",
        "HEALTH",
        "BATTERY",
        "RETURN_CAPABILITY",
        "GEOFENCE",
        "PAYLOAD",
    }


async def test_dispatcher_prefers_the_closest_safe_aircraft(
    dispatcher, live_fleet, adapters, fleet_config
) -> None:
    """Ranking is by live telemetry, and only among delivery-role aircraft."""
    d3 = live_fleet.state("D3")
    latitude, longitude = _target_at(d3, 500)

    candidates = dispatcher._rank_candidates(  # noqa: SLF001
        __import__("uuid").uuid4(), latitude, longitude
    )
    assert [c.drone_id for c in candidates] == ["D3"], (
        "scouts must never be considered for a delivery"
    )
    assert candidates[0].report.safe is True


async def test_no_delivery_aircraft_available_when_d3_is_busy(
    dispatcher, live_fleet
) -> None:
    live_fleet.state("D3").delivery_task_id = "task-1"
    assert live_fleet.available_delivery_drones() == []

    candidates = dispatcher._rank_candidates(  # noqa: SLF001
        __import__("uuid").uuid4(), 11.24, 77.13
    )
    assert candidates == []


async def _settle(fleet, drone_id: str, predicate, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    state = fleet.state(drone_id)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if predicate(state):
                return
        except AttributeError:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError(f"telemetry for {drone_id} did not settle")


# ---------------------------------------------------------------------------
# energy model calibration (Phase 10)
# ---------------------------------------------------------------------------
async def test_uncalibrated_energy_model_blocks_dispatch(
    live_fleet,
    command_service,
    survivor_manager,
    geofence_service,
    events_service,
    bus,
    settings,
    uncalibrated_energy_model,
    d3_state,
) -> None:
    """An unmeasured energy model is not evidence an aircraft can get home.

    This is the default state of a new deployment. A perfectly healthy D3 with
    a full battery and a target 300 m away is still refused, because nobody has
    measured what a delivery actually costs this airframe.
    """
    from app.services.delivery_manager import DeliveryManager

    manager = DeliveryManager(
        live_fleet, command_service, survivor_manager, geofence_service,
        events_service, ConfirmationRegistry(DeliveryConfirmationSource.OPERATOR),
        bus, settings, energy_model=uncalibrated_energy_model,
    )
    latitude, longitude = _target_at(d3_state, 300)
    report = manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is False
    failures = {c.name for c in report.failures}
    assert "ENERGY_MODEL_CALIBRATION" in failures
    assert report.as_dict()["energy_model_calibrated"] is False

    # The battery itself is fine -- the refusal is specifically about the model.
    battery = next(c for c in report.checks if c.name == "BATTERY")
    assert battery.status is CheckStatus.PASS


async def test_uncalibrated_return_capability_is_unknown_not_pass(
    live_fleet,
    command_service,
    survivor_manager,
    geofence_service,
    events_service,
    bus,
    settings,
    uncalibrated_energy_model,
    d3_state,
) -> None:
    """The estimate is still computed and shown, but labelled UNKNOWN.

    Reporting PASS from unmeasured coefficients would be presenting a guess as
    a validated safety limit.
    """
    from app.services.delivery_manager import DeliveryManager

    manager = DeliveryManager(
        live_fleet, command_service, survivor_manager, geofence_service,
        events_service, ConfirmationRegistry(DeliveryConfirmationSource.OPERATOR),
        bus, settings, energy_model=uncalibrated_energy_model,
    )
    latitude, longitude = _target_at(d3_state, 300)
    report = manager.evaluate_safety(d3_state, latitude, longitude)

    check = next(c for c in report.checks if c.name == "RETURN_CAPABILITY")
    assert check.status is CheckStatus.UNKNOWN
    assert "uncalibrated" in (check.reason or "").lower()
    # The numbers are still there for the operator to look at.
    assert check.observed["estimated_battery_cost_pct"] > 0


async def test_calibrated_model_allows_a_safe_dispatch(
    delivery_manager, d3_state
) -> None:
    latitude, longitude = _target_at(d3_state, 300)
    report = delivery_manager.evaluate_safety(d3_state, latitude, longitude)

    assert report.safe is True
    assert report.as_dict()["energy_model_calibrated"] is True
    assert "ENERGY_MODEL_CALIBRATION" not in {c.name for c in report.checks}
    check = next(c for c in report.checks if c.name == "RETURN_CAPABILITY")
    assert check.status is CheckStatus.PASS


def test_calibration_cannot_be_claimed_without_provenance() -> None:
    """Flipping a boolean must not be enough to mark an airframe measured."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError, match="provenance"):
        CalibrationRecord(calibrated=True)

    with pytest.raises(PydanticValidationError, match="measurement flight"):
        CalibrationRecord(
            calibrated=True,
            calibrated_on=date(2026, 9, 1),
            calibrated_by="someone",
            airframe="D3",
            sample_count=0,
        )


def test_energy_estimate_is_conservative() -> None:
    """Every term should push the estimate up, not down.

    The failure mode being guarded against is an optimistic estimate that
    strands a loaded aircraft short of a survivor.
    """
    model = EnergyModelConfig(
        calibration=CalibrationRecord(
            calibrated=True,
            calibrated_on=date(2026, 9, 1),
            calibrated_by="test",
            airframe="TEST",
            sample_count=3,
        )
    )
    estimate = model.estimate(1000.0, payload_g=1200)

    naive_round_trip = 2 * 1.0 * model.battery_pct_per_km
    assert estimate.total_pct > naive_round_trip, (
        "the estimate must exceed a naive there-and-back figure"
    )
    # Outbound is loaded, so it costs more than the empty return leg.
    assert estimate.outbound_transit_pct > estimate.return_transit_pct
    assert estimate.takeoff_landing_pct > 0
    assert estimate.total_pct > estimate.raw_total_pct


def test_uncalibrated_estimate_never_reports_it_can_return() -> None:
    estimate = EnergyModelConfig().estimate(100.0)
    assert estimate.calibrated is False
    # Even with a full battery and a 100 m hop.
    assert estimate.can_return(100.0) is False


def test_calibration_state_is_reported_honestly() -> None:
    described = EnergyModelConfig().describe()
    assert described["status"] == "UNCALIBRATED"
    assert described["calibrated"] is False
    assert "placeholders" in described["warning"]
