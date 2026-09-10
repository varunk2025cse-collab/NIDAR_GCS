"""Search sector coverage semantics.

The rule under test: coverage is only ever reported from real PX4 mission
progress. A sector nobody has flown reports NOT_STARTED, not 0% -- those are
different facts, and only one of them means the ground has been looked at.

An operator reading "Sector 3B: 0%" reasonably concludes an aircraft is there
and has not covered anything yet. Reading "NOT_STARTED" they conclude nobody
has been. Conflating the two hides an unsearched sector inside a coverage
figure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.enums import SectorState
from app.services.search_manager import (
    ProgressStatus,
    progress_status,
    reported_progress,
)


class FakeSector:
    """Minimal stand-in: the classifier only reads these three attributes."""

    def __init__(
        self,
        state: SectorState,
        progress: float = 0.0,
        observed_at: datetime | None = None,
    ) -> None:
        self.state = state
        self.progress = progress
        self.progress_observed_at = observed_at


def test_unassigned_sector_is_not_started_not_zero_percent() -> None:
    sector = FakeSector(SectorState.UNASSIGNED)
    assert progress_status(sector) is ProgressStatus.NOT_STARTED
    assert reported_progress(sector) is None, (
        "an unflown sector must not report a coverage number at all"
    )


def test_assigned_but_unflown_sector_is_not_started() -> None:
    sector = FakeSector(SectorState.ASSIGNED)
    assert progress_status(sector) is ProgressStatus.NOT_STARTED
    assert reported_progress(sector) is None


def test_in_progress_without_telemetry_is_unknown() -> None:
    """An aircraft is out there but PX4 has not reported progress.

    Usually a link problem. Reporting 0% would blame the search; UNKNOWN
    points at the telemetry, which is where the fault actually is.
    """
    sector = FakeSector(SectorState.IN_PROGRESS)
    assert progress_status(sector) is ProgressStatus.UNKNOWN
    assert reported_progress(sector) is None


def test_measured_progress_is_reported() -> None:
    sector = FakeSector(
        SectorState.IN_PROGRESS, progress=0.42, observed_at=datetime.now(UTC)
    )
    assert progress_status(sector) is ProgressStatus.MEASURED
    assert reported_progress(sector) == pytest.approx(0.42)


def test_genuinely_measured_zero_is_reported_as_zero() -> None:
    """The other side of the rule: a real measurement of 0% is a real fact."""
    sector = FakeSector(
        SectorState.IN_PROGRESS, progress=0.0, observed_at=datetime.now(UTC)
    )
    assert progress_status(sector) is ProgressStatus.MEASURED
    assert reported_progress(sector) == 0.0, (
        "a measured 0% is data and must be shown, unlike an unmeasured one"
    )


def test_completed_without_measurement_is_unknown_not_full_coverage() -> None:
    """A sector marked complete that never reported progress is suspicious.

    Claiming 100% there would assert the ground was searched on the strength
    of a state transition rather than a measurement.
    """
    sector = FakeSector(SectorState.COMPLETED, progress=1.0)
    assert progress_status(sector) is ProgressStatus.UNKNOWN
    assert reported_progress(sector) is None


def test_completed_with_measurement_reports_its_coverage() -> None:
    sector = FakeSector(
        SectorState.COMPLETED, progress=1.0, observed_at=datetime.now(UTC)
    )
    assert progress_status(sector) is ProgressStatus.MEASURED
    assert reported_progress(sector) == pytest.approx(1.0)


def test_blocked_sector_is_unknown() -> None:
    sector = FakeSector(SectorState.BLOCKED)
    assert progress_status(sector) is ProgressStatus.UNKNOWN
    assert reported_progress(sector) is None
