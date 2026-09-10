"""Telemetry freshness.

The rule under test: a value is only ever presented as live while it is
actually fresh, and an expired value is replaced by None with an explicit
status -- never by a number.
"""

from __future__ import annotations

import time

from app.core.config import TelemetryFreshnessConfig
from app.core.enums import TelemetryStatus
from app.core.freshness import FreshnessPolicy, TimedValue


def test_unset_value_is_no_data() -> None:
    value: TimedValue[float] = TimedValue()
    assert value.status(2.0, 10.0) is TelemetryStatus.NO_DATA
    assert value.age_s() is None

    rendered = value.render(2.0, 10.0)
    assert rendered["value"] is None
    assert rendered["timestamp"] is None
    assert rendered["status"] == "NO_DATA"


def test_fresh_then_stale_then_no_data() -> None:
    value: TimedValue[float] = TimedValue()
    value.set(78.0)
    now = time.monotonic()

    assert value.status(2.0, 10.0, now) is TelemetryStatus.FRESH
    assert value.status(2.0, 10.0, now + 5) is TelemetryStatus.STALE
    assert value.status(2.0, 10.0, now + 30) is TelemetryStatus.NO_DATA


def test_expired_value_is_not_rendered() -> None:
    """The specific failure this guards against: a battery reading of 78
    still being shown long after the aircraft stopped reporting it."""
    battery: TimedValue[float] = TimedValue()
    battery.set(78.0)
    now = time.monotonic()

    fresh = battery.render(5.0, 30.0, now)
    assert fresh["value"] == 78.0
    assert fresh["status"] == "FRESH"

    stale = battery.render(5.0, 30.0, now + 10)
    assert stale["value"] == 78.0, "a stale value is still shown, but labelled stale"
    assert stale["status"] == "STALE"

    expired = battery.render(5.0, 30.0, now + 60)
    assert expired["value"] is None, "an expired value must never be rendered"
    assert expired["status"] == "NO_DATA"
    # The timestamp survives so the UI can say when contact was lost.
    assert expired["timestamp"] is not None


def test_clear_drops_value_but_keeps_timestamp() -> None:
    value: TimedValue[float] = TimedValue()
    value.set(42.0)
    value.clear()

    assert value.value is None
    assert value.wall_at is not None
    assert value.status(2.0, 10.0) is TelemetryStatus.NO_DATA


def test_zero_is_a_real_value_not_absence() -> None:
    """A battery at 0% and a battery that is not reporting are different."""
    value: TimedValue[float] = TimedValue()
    value.set(0.0)
    assert value.status(5.0, 30.0) is TelemetryStatus.FRESH
    assert value.render(5.0, 30.0)["value"] == 0.0


def test_policy_uses_per_stream_limits() -> None:
    policy = FreshnessPolicy(
        TelemetryFreshnessConfig(
            position_fresh_s=2.0,
            position_stale_s=10.0,
            battery_fresh_s=5.0,
            battery_stale_s=30.0,
        )
    )
    assert policy.limits("position") == (2.0, 10.0)
    assert policy.limits("battery") == (5.0, 30.0)
    # An unknown stream falls back to the defaults rather than being treated
    # as always-fresh.
    assert policy.limits("something_new") == (5.0, 30.0)


def test_policy_status_differs_per_stream_at_same_age() -> None:
    policy = FreshnessPolicy(TelemetryFreshnessConfig())
    position: TimedValue[str] = TimedValue()
    battery: TimedValue[str] = TimedValue()
    position.set("p")
    battery.set("b")
    now = time.monotonic()

    # 3 seconds old: position is stale (2s limit), battery is still fresh (5s).
    assert policy.status("position", position, now + 3) is TelemetryStatus.STALE
    assert policy.status("battery", battery, now + 3) is TelemetryStatus.FRESH


def test_age_uses_monotonic_clock() -> None:
    """A wall-clock jump must not make stale telemetry look fresh."""
    value: TimedValue[float] = TimedValue()
    value.set(1.0)
    assert value.monotonic_at is not None
    # Age is derived from the monotonic reading, independent of wall_at.
    assert value.age_s(value.monotonic_at + 7.0) == 7.0
