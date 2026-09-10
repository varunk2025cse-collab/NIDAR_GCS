"""Detection validation and duplicate matching.

An endpoint that creates survivors is an endpoint that redirects aircraft, so
these rules decide what the GCS is willing to believe from a companion
computer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ai.detection_validator import DetectionValidator, is_stale_for_dispatch
from app.core.enums import MissionState
from app.services.survivor_manager import (
    duplicate_match_threshold_m,
    is_duplicate_candidate,
)


@pytest.fixture
def validator(settings, geofence_service) -> DetectionValidator:
    return DetectionValidator(settings, geofence_service)


def _valid_kwargs(state, **overrides):
    position = state.position.value
    payload = {
        "drone_state": state,
        "mission_id": "mission-1",
        "mission_state": MissionState.SEARCHING,
        "latitude": position.latitude + 0.0002,
        "longitude": position.longitude + 0.0002,
        "confidence": 0.9,
        "detected_at": datetime.now(UTC),
        "estimated_accuracy_m": 8.0,
    }
    payload.update(overrides)
    return payload


async def test_a_good_detection_is_accepted(validator, d1_state) -> None:
    outcome = validator.validate(**_valid_kwargs(d1_state))
    assert outcome.accepted is True
    assert outcome.code is None


async def test_detection_from_an_unknown_aircraft_is_rejected(validator, d1_state) -> None:
    outcome = validator.validate(**_valid_kwargs(d1_state, drone_state=None))
    assert outcome.accepted is False
    assert outcome.code == "UNKNOWN_DRONE"


async def test_detection_outside_an_active_mission_is_rejected(
    validator, d1_state
) -> None:
    """Nothing may create survivor records when no mission is flying."""
    for state in (None, MissionState.DRAFT, MissionState.COMPLETED, MissionState.ABORTED):
        outcome = validator.validate(**_valid_kwargs(d1_state, mission_state=state))
        assert outcome.accepted is False
        assert outcome.code == "MISSION_NOT_ACTIVE"


async def test_null_island_is_rejected(validator, d1_state) -> None:
    """(0, 0) is almost always an uninitialised GPS value, not a location."""
    outcome = validator.validate(**_valid_kwargs(d1_state, latitude=0.0, longitude=0.0))
    assert outcome.accepted is False
    assert outcome.code == "INVALID_COORDINATE"


async def test_confidence_out_of_range_is_rejected(validator, d1_state) -> None:
    outcome = validator.validate(**_valid_kwargs(d1_state, confidence=1.4))
    assert outcome.accepted is False
    assert outcome.code == "INVALID_CONFIDENCE"


async def test_confidence_below_the_floor_is_rejected(validator, d1_state, settings) -> None:
    outcome = validator.validate(
        **_valid_kwargs(d1_state, confidence=settings.survivor.min_confidence - 0.1)
    )
    assert outcome.accepted is False
    assert outcome.code == "CONFIDENCE_TOO_LOW"


async def test_future_timestamp_is_rejected(validator, d1_state) -> None:
    """A companion computer with a wrong clock must be caught, not trusted."""
    outcome = validator.validate(
        **_valid_kwargs(d1_state, detected_at=datetime.now(UTC) + timedelta(minutes=5))
    )
    assert outcome.accepted is False
    assert outcome.code == "TIMESTAMP_IN_FUTURE"


async def test_stale_detection_is_rejected(validator, d1_state, settings) -> None:
    outcome = validator.validate(
        **_valid_kwargs(
            d1_state,
            detected_at=datetime.now(UTC)
            - timedelta(seconds=settings.survivor.max_detection_age_s + 60),
        )
    )
    assert outcome.accepted is False
    assert outcome.code == "DETECTION_TOO_OLD"


async def test_implausible_geometry_is_rejected(validator, d1_state) -> None:
    """A downward camera at 100 m cannot see a survivor 5 km away. Accepting
    that would send a delivery aircraft to a fabricated position."""
    position = d1_state.position.value
    outcome = validator.validate(
        **_valid_kwargs(
            d1_state,
            latitude=position.latitude + 0.05,
            longitude=position.longitude + 0.05,
        )
    )
    assert outcome.accepted is False
    assert outcome.code == "IMPLAUSIBLE_GEOMETRY"


async def test_missing_geofence_warns_but_does_not_reject(validator, d1_state) -> None:
    outcome = validator.validate(**_valid_kwargs(d1_state))
    assert outcome.accepted is True
    assert any("geofence" in w.lower() for w in outcome.warnings)


async def test_poor_accuracy_warns_about_auto_confirmation(
    validator, d1_state, settings
) -> None:
    outcome = validator.validate(
        **_valid_kwargs(
            d1_state,
            estimated_accuracy_m=settings.survivor.max_auto_confirm_accuracy_m + 20,
        )
    )
    assert outcome.accepted is True
    assert any("auto-confirm" in w for w in outcome.warnings)


async def test_negative_accuracy_is_rejected(validator, d1_state) -> None:
    outcome = validator.validate(**_valid_kwargs(d1_state, estimated_accuracy_m=-5.0))
    assert outcome.accepted is False
    assert outcome.code == "INVALID_ACCURACY"


def test_dispatch_staleness_helper() -> None:
    assert is_stale_for_dispatch(datetime.now(UTC) - timedelta(seconds=200), 120) is True
    assert is_stale_for_dispatch(datetime.now(UTC) - timedelta(seconds=10), 120) is False
    # A naive timestamp is treated as UTC rather than crashing.
    assert is_stale_for_dispatch(datetime.now(UTC).replace(tzinfo=None), 120) is False


# ---------------------------------------------------------------------------
# duplicate matching
# ---------------------------------------------------------------------------
def test_match_radius_widens_with_position_uncertainty() -> None:
    """Two sightings each accurate to +/-15 m can be 30 m apart and still be
    one person; using the bare radius would create a second record and send a
    second aircraft."""
    assert duplicate_match_threshold_m(25.0, None, None) == 25.0
    assert duplicate_match_threshold_m(25.0, 15.0, 15.0) == 55.0
    assert duplicate_match_threshold_m(25.0, 15.0, None) == 40.0


def test_precise_sightings_are_not_over_merged() -> None:
    """With good fixes on both sides, two people 30 m apart stay two records."""
    assert is_duplicate_candidate(30.0, 25.0, 1.0, 1.0) is False
    assert is_duplicate_candidate(20.0, 25.0, 1.0, 1.0) is True


def test_uncertain_sightings_are_merged_rather_than_duplicated() -> None:
    """The D1/D2 overlap case: both scouts see the same person from altitude
    with 15 m accuracy, and their fixes land 40 m apart."""
    assert is_duplicate_candidate(40.0, 25.0, 15.0, 15.0) is True


def test_zero_and_negative_accuracies_do_not_shrink_the_radius() -> None:
    assert duplicate_match_threshold_m(25.0, 0.0, -3.0) == 25.0
