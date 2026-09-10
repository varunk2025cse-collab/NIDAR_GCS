"""Telemetry freshness.

Every value the backend holds about a physical aircraft carries the instant it
was received. Nothing is ever presented as live because it once was.

Ages are computed from a monotonic clock so that an NTP step or a manual
system clock change on the GCS laptop cannot make stale telemetry look fresh.
The wall-clock timestamp is carried alongside purely for display and storage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from app.core.config import TelemetryFreshnessConfig
from app.core.enums import TelemetryStatus

T = TypeVar("T")


@dataclass(slots=True)
class TimedValue(Generic[T]):
    """A telemetry value with the instant it arrived from the aircraft.

    ``value is None`` means the stream has produced nothing yet -- the field
    reports NO_DATA. It never means zero.
    """

    value: T | None = None
    monotonic_at: float | None = None
    wall_at: datetime | None = None
    #: Free-form provenance, e.g. {"stream": "battery"}.
    meta: dict[str, Any] = field(default_factory=dict)

    def set(self, value: T, *, meta: dict[str, Any] | None = None) -> None:
        self.value = value
        self.monotonic_at = time.monotonic()
        self.wall_at = datetime.now(UTC)
        if meta:
            self.meta.update(meta)

    def clear(self) -> None:
        """Drop the value entirely (used when a link is torn down).

        The last-known timestamp is preserved so the UI can still say *when*
        contact was lost, but the value itself becomes NO_DATA rather than a
        number that is no longer true.
        """
        self.value = None

    def age_s(self, now: float | None = None) -> float | None:
        if self.monotonic_at is None:
            return None
        return max(0.0, (now if now is not None else time.monotonic()) - self.monotonic_at)

    def status(
        self, fresh_s: float, stale_s: float, now: float | None = None
    ) -> TelemetryStatus:
        age = self.age_s(now)
        if self.value is None or age is None:
            return TelemetryStatus.NO_DATA
        if age <= fresh_s:
            return TelemetryStatus.FRESH
        if age <= stale_s:
            return TelemetryStatus.STALE
        return TelemetryStatus.NO_DATA

    def render(
        self, fresh_s: float, stale_s: float, now: float | None = None
    ) -> dict[str, Any]:
        """Wire representation: value + timestamp + freshness, always together.

        When the status is NO_DATA the value is forced to ``None`` so a caller
        cannot accidentally render an expired number.
        """
        status = self.status(fresh_s, stale_s, now)
        age = self.age_s(now)
        return {
            "value": self.value if status is not TelemetryStatus.NO_DATA else None,
            "timestamp": self.wall_at.isoformat() if self.wall_at else None,
            "age_s": round(age, 3) if age is not None else None,
            "status": str(status),
        }


class FreshnessPolicy:
    """Maps a telemetry stream name onto its configured age limits."""

    _LIMITS = {
        "position": ("position_fresh_s", "position_stale_s"),
        "battery": ("battery_fresh_s", "battery_stale_s"),
        "gps": ("gps_fresh_s", "gps_stale_s"),
        "attitude": ("attitude_fresh_s", "attitude_stale_s"),
        "velocity": ("velocity_fresh_s", "velocity_stale_s"),
        "flight_mode": ("flight_mode_fresh_s", "flight_mode_stale_s"),
        "health": ("health_fresh_s", "health_stale_s"),
    }

    def __init__(self, config: TelemetryFreshnessConfig) -> None:
        self._config = config

    def limits(self, stream: str) -> tuple[float, float]:
        fresh_attr, stale_attr = self._LIMITS.get(
            stream, ("default_fresh_s", "default_stale_s")
        )
        return getattr(self._config, fresh_attr), getattr(self._config, stale_attr)

    def status(
        self, stream: str, value: TimedValue[Any], now: float | None = None
    ) -> TelemetryStatus:
        fresh, stale = self.limits(stream)
        return value.status(fresh, stale, now)

    def render(
        self, stream: str, value: TimedValue[Any], now: float | None = None
    ) -> dict[str, Any]:
        fresh, stale = self.limits(stream)
        return value.render(fresh, stale, now)
