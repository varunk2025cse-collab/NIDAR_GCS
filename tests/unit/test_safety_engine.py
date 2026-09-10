"""Safety engine.

The engine is advisory: it raises alerts, it never commands an aircraft.
These tests check that it notices the right things, deduplicates standing
conditions, and stays quiet about telemetry it is no longer receiving rather
than alerting on remembered values.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.enums import AlertCategory, AlertSeverity
from app.drone.types import Battery, GpsInfo, HealthReport
from app.realtime.event_bus import EventType


async def test_healthy_fleet_raises_nothing(safety_engine) -> None:
    conditions = await safety_engine.evaluate_once()
    assert conditions == [], f"unexpected alerts: {[c.code for c in conditions]}"


async def test_battery_thresholds_escalate(
    safety_engine, adapters, live_fleet, settings
) -> None:
    adapter = adapters.get("D1")

    for percent, expected_code, expected_severity in (
        (38.0, "BATTERY_LOW", AlertSeverity.WARNING),
        (22.0, "BATTERY_CRITICAL", AlertSeverity.CRITICAL),
        (10.0, "BATTERY_EMERGENCY", AlertSeverity.EMERGENCY),
    ):
        adapter.current_battery = Battery(percent, 21.0, 5.0)
        await _settle(live_fleet, "D1", lambda s, p=percent: (
            s.battery.value is not None and s.battery.value.remaining_percent == p
        ))
        conditions = await safety_engine.evaluate_once()
        battery = [c for c in conditions if c.category is AlertCategory.BATTERY]
        assert len(battery) == 1
        assert battery[0].code == expected_code
        assert battery[0].severity is expected_severity


async def test_missing_battery_data_is_itself_an_alert(
    safety_engine, adapters, live_fleet
) -> None:
    """Not knowing the battery state is a condition worth telling the operator
    about; it is not the same as a healthy battery."""
    adapters.get("D1").current_battery = Battery(None, None, None)
    await _settle(live_fleet, "D1", lambda s: (
        s.battery.value is not None and s.battery.value.remaining_percent is None
    ))

    conditions = await safety_engine.evaluate_once()
    codes = {c.code for c in conditions}
    assert "BATTERY_NO_PERCENT" in codes


async def test_gps_degradation_is_reported(safety_engine, adapters, live_fleet) -> None:
    adapters.get("D2").current_gps = GpsInfo(
        fix_type=1, fix_type_name="NO_FIX", satellites=2
    )
    await _settle(live_fleet, "D2", lambda s: s.gps.value.fix_type == 1)

    conditions = await safety_engine.evaluate_once()
    gps = [c for c in conditions if c.category is AlertCategory.GPS]
    assert gps and gps[0].code == "GPS_FIX_LOST"
    assert gps[0].severity is AlertSeverity.CRITICAL
    assert gps[0].drone_id == "D2"


async def test_low_satellite_count_warns(safety_engine, adapters, live_fleet) -> None:
    adapters.get("D2").current_gps = GpsInfo(
        fix_type=3, fix_type_name="FIX_3D", satellites=4
    )
    await _settle(live_fleet, "D2", lambda s: s.gps.value.satellites == 4)

    conditions = await safety_engine.evaluate_once()
    codes = {c.code for c in conditions}
    assert "GPS_LOW_SATELLITES" in codes


async def test_link_loss_raises_a_critical_alert(
    safety_engine, adapters, live_fleet
) -> None:
    adapters.make_unreachable("D1")
    await asyncio.sleep(1.3)

    conditions = await safety_engine.evaluate_once()
    connection = [c for c in conditions if c.category is AlertCategory.CONNECTION]
    assert connection, "a lost link must raise an alert"
    assert connection[0].severity is AlertSeverity.CRITICAL
    assert connection[0].evidence["last_seen"] is not None


async def test_no_battery_alerts_are_invented_for_a_disconnected_aircraft(
    safety_engine, adapters, live_fleet
) -> None:
    """Once the link is down the GCS has no battery data. Alerting on the last
    remembered value would be reporting a measurement it is not making."""
    adapters.make_unreachable("D1")
    await asyncio.sleep(1.3)

    conditions = await safety_engine.evaluate_once()
    d1 = [c for c in conditions if c.drone_id == "D1"]
    categories = {c.category for c in d1}
    assert AlertCategory.CONNECTION in categories
    assert AlertCategory.BATTERY not in categories
    assert AlertCategory.GPS not in categories


async def test_standing_condition_is_deduplicated(
    safety_engine, adapters, live_fleet, collected_events, bus
) -> None:
    """A drone sitting at 22% must not produce one alert per cycle."""
    adapters.get("D1").current_battery = Battery(22.0, 21.0, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 22.0)

    for _ in range(5):
        await safety_engine.evaluate_once()
    await bus.drain()

    created = [
        e
        for e in collected_events
        if e.type == EventType.ALERT_CREATED and e.payload["code"] == "BATTERY_CRITICAL"
    ]
    assert len(created) == 1, f"expected one standing alert, got {len(created)}"
    assert len(safety_engine.active_alerts()) >= 1


async def test_alert_clears_when_the_condition_resolves(
    safety_engine, adapters, live_fleet, collected_events, bus
) -> None:
    adapter = adapters.get("D1")
    adapter.current_battery = Battery(22.0, 21.0, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 22.0)
    await safety_engine.evaluate_once()
    assert any(a["code"] == "BATTERY_CRITICAL" for a in safety_engine.active_alerts())

    adapter.current_battery = Battery(85.0, 22.2, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 85.0)
    await safety_engine.evaluate_once()
    await bus.drain()

    assert not any(a["code"] == "BATTERY_CRITICAL" for a in safety_engine.active_alerts())
    cleared = [e for e in collected_events if e.type == EventType.ALERT_CLEARED]
    assert cleared


async def test_severity_change_supersedes_the_previous_alert(
    safety_engine, adapters, live_fleet
) -> None:
    adapter = adapters.get("D1")
    adapter.current_battery = Battery(38.0, 21.5, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 38.0)
    await safety_engine.evaluate_once()
    assert any(a["code"] == "BATTERY_LOW" for a in safety_engine.active_alerts())

    adapter.current_battery = Battery(12.0, 20.0, 5.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 12.0)
    await safety_engine.evaluate_once()

    active = safety_engine.active_alerts()
    codes = {a["code"] for a in active}
    assert "BATTERY_EMERGENCY" in codes
    assert "BATTERY_LOW" not in codes


async def test_mission_timeout_warning_then_expiry(safety_engine) -> None:
    started = datetime.now(UTC) - timedelta(seconds=1700)
    safety_engine.track_mission("mission-1", started, 1800)

    conditions = await safety_engine.evaluate_once()
    timeouts = [c for c in conditions if c.category is AlertCategory.MISSION_TIMEOUT]
    assert timeouts and timeouts[0].code == "MISSION_TIME_LOW"

    safety_engine.track_mission(
        "mission-1", datetime.now(UTC) - timedelta(seconds=1900), 1800
    )
    conditions = await safety_engine.evaluate_once()
    timeouts = [c for c in conditions if c.category is AlertCategory.MISSION_TIMEOUT]
    assert timeouts and timeouts[0].code == "MISSION_TIME_EXPIRED"
    assert timeouts[0].severity is AlertSeverity.CRITICAL


async def test_untracking_a_mission_clears_its_timeout_alert(safety_engine) -> None:
    safety_engine.track_mission(
        "mission-2", datetime.now(UTC) - timedelta(seconds=1900), 1800
    )
    await safety_engine.evaluate_once()
    assert any(a["code"] == "MISSION_TIME_EXPIRED" for a in safety_engine.active_alerts())

    safety_engine.untrack_mission("mission-2")
    await safety_engine.evaluate_once()
    assert not any(
        a["code"] == "MISSION_TIME_EXPIRED" for a in safety_engine.active_alerts()
    )


async def test_autopilot_initiated_rtl_is_surfaced(
    safety_engine, adapters, live_fleet
) -> None:
    """PX4 entering RTL by itself is a failsafe firing. The operator has to
    know, and the GCS must not interfere with it."""
    adapters.get("D1").current_flight_mode = "RETURN_TO_LAUNCH"
    await _settle(live_fleet, "D1", lambda s: s.flight_mode.value == "RETURN_TO_LAUNCH")

    conditions = await safety_engine.evaluate_once()
    modes = [c for c in conditions if c.category is AlertCategory.FLIGHT_MODE]
    assert modes and modes[0].code == "AUTOPILOT_FAILSAFE_MODE"


async def test_unhealthy_vehicle_in_flight_warns(
    safety_engine, adapters, live_fleet
) -> None:
    adapter = adapters.get("D1")
    adapter.current_health = HealthReport(
        gyrometer_calibration_ok=True,
        accelerometer_calibration_ok=True,
        magnetometer_calibration_ok=False,
        local_position_ok=True,
        global_position_ok=True,
        home_position_ok=True,
        armable=True,
    )
    await _settle(
        live_fleet, "D1", lambda s: s.health.value.magnetometer_calibration_ok is False
    )

    conditions = await safety_engine.evaluate_once()
    health = [c for c in conditions if c.category is AlertCategory.HEALTH]
    assert health and health[0].code == "VEHICLE_HEALTH_DEGRADED"


async def test_grounded_unarmable_aircraft_is_not_an_alert(
    safety_engine, adapters, live_fleet
) -> None:
    """An unarmed aircraft on the ground legitimately reports not-armable;
    alerting on it would train operators to ignore health alerts."""
    adapters.get("D1").current_health = HealthReport(
        gyrometer_calibration_ok=True,
        accelerometer_calibration_ok=True,
        magnetometer_calibration_ok=True,
        local_position_ok=True,
        global_position_ok=True,
        home_position_ok=True,
        armable=False,
    )
    await _settle(live_fleet, "D1", lambda s: s.health.value.armable is False)

    conditions = await safety_engine.evaluate_once()
    assert not [c for c in conditions if c.category is AlertCategory.HEALTH]


async def test_engine_never_sends_a_command(safety_engine, adapters, live_fleet) -> None:
    """PX4 owns the aircraft. The safety engine raises alerts and nothing else."""
    adapters.get("D1").current_battery = Battery(5.0, 19.0, 8.0)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 5.0)

    await safety_engine.evaluate_once()

    for drone_id in ("D1", "D2", "D3"):
        assert adapters.get(drone_id).command_log == [], (
            "the safety engine must never command an aircraft"
        )


async def test_highest_severity_reports_the_worst_standing_condition(
    safety_engine, adapters, live_fleet
) -> None:
    adapters.get("D1").current_battery = Battery(10.0, 19.5, 6.0)
    adapters.get("D2").current_gps = GpsInfo(3, "FIX_3D", 4)
    await _settle(live_fleet, "D1", lambda s: s.battery.value.remaining_percent == 10.0)
    await _settle(live_fleet, "D2", lambda s: s.gps.value.satellites == 4)

    await safety_engine.evaluate_once()
    assert safety_engine.highest_severity() is AlertSeverity.EMERGENCY


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
