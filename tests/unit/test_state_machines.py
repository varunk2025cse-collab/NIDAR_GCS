"""State machine transition tables.

These tables are the contract shared by the database, the REST API and the
WebSocket feed. A transition that is not listed is not reachable, which is
what stops a completed mission being pushed back into takeoff, or a survivor
being marked delivered without ever having been assigned.
"""

from __future__ import annotations

import pytest

from app.core.enums import (
    DELIVERY_TRANSITIONS,
    MISSION_ACTIVE_STATES,
    MISSION_TERMINAL_STATES,
    MISSION_TRANSITIONS,
    SECTOR_TRANSITIONS,
    SURVIVOR_TRANSITIONS,
    DeliveryState,
    MissionState,
    SectorState,
    SurvivorState,
)


# ---------------------------------------------------------------------------
# completeness -- every state must appear in its table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("enum_cls", "table"),
    [
        (MissionState, MISSION_TRANSITIONS),
        (SectorState, SECTOR_TRANSITIONS),
        (SurvivorState, SURVIVOR_TRANSITIONS),
        (DeliveryState, DELIVERY_TRANSITIONS),
    ],
)
def test_every_state_has_a_transition_entry(enum_cls, table) -> None:
    missing = [s for s in enum_cls if s not in table]
    assert not missing, f"states with no transition rule: {missing}"


@pytest.mark.parametrize(
    ("enum_cls", "table"),
    [
        (MissionState, MISSION_TRANSITIONS),
        (SectorState, SECTOR_TRANSITIONS),
        (SurvivorState, SURVIVOR_TRANSITIONS),
        (DeliveryState, DELIVERY_TRANSITIONS),
    ],
)
def test_transitions_only_target_known_states(enum_cls, table) -> None:
    valid = set(enum_cls)
    for state, targets in table.items():
        unknown = targets - valid
        assert not unknown, f"{state} points at unknown states: {unknown}"


@pytest.mark.parametrize(
    ("enum_cls", "table"),
    [
        (MissionState, MISSION_TRANSITIONS),
        (SurvivorState, SURVIVOR_TRANSITIONS),
        (DeliveryState, DELIVERY_TRANSITIONS),
    ],
)
def test_no_state_transitions_to_itself(enum_cls, table) -> None:
    """Self-transitions would let a no-op look like progress in the timeline."""
    for state, targets in table.items():
        assert state not in targets, f"{state} lists itself as a target"


# ---------------------------------------------------------------------------
# mission
# ---------------------------------------------------------------------------
def test_completed_mission_is_terminal() -> None:
    assert MISSION_TRANSITIONS[MissionState.COMPLETED] == frozenset()
    assert MissionState.TAKEOFF not in MISSION_TRANSITIONS[MissionState.COMPLETED]


def test_every_terminal_mission_state_is_a_dead_end() -> None:
    for state in MISSION_TERMINAL_STATES:
        assert MISSION_TRANSITIONS[state] == frozenset(), f"{state} must be terminal"


def test_every_active_mission_state_can_abort() -> None:
    """An operator must be able to abort from any live state."""
    for state in MISSION_ACTIVE_STATES:
        assert MissionState.ABORTING in MISSION_TRANSITIONS[state], (
            f"{state} cannot reach ABORTING"
        )


def test_aborting_ends_in_aborted_or_partial_failure() -> None:
    targets = MISSION_TRANSITIONS[MissionState.ABORTING]
    assert MissionState.ABORTED in targets
    assert MissionState.PARTIAL_ABORT_FAILURE in targets
    assert MissionState.SEARCHING not in targets


def test_mission_cannot_skip_from_draft_to_flying() -> None:
    """DRAFT must pass through READY and PRECHECK, which is where preflight
    runs."""
    from_draft = MISSION_TRANSITIONS[MissionState.DRAFT]
    assert MissionState.ARMING not in from_draft
    assert MissionState.TAKEOFF not in from_draft
    assert MissionState.SEARCHING not in from_draft
    assert MissionState.READY in from_draft


def test_arming_is_only_reachable_from_precheck() -> None:
    sources = [s for s, t in MISSION_TRANSITIONS.items() if MissionState.ARMING in t]
    assert sources == [MissionState.PRECHECK], (
        "arming must be gated behind the preflight state"
    )


# ---------------------------------------------------------------------------
# survivor
# ---------------------------------------------------------------------------
def test_survivor_cannot_be_delivered_without_being_assigned() -> None:
    for state in SurvivorState:
        if state is SurvivorState.DELIVERY_IN_PROGRESS:
            continue
        assert SurvivorState.DELIVERED not in SURVIVOR_TRANSITIONS[state], (
            f"{state} must not jump straight to DELIVERED"
        )


def test_delivered_survivor_is_terminal() -> None:
    assert SURVIVOR_TRANSITIONS[SurvivorState.DELIVERED] == frozenset()


def test_survivor_can_be_marked_duplicate_before_delivery() -> None:
    for state in (
        SurvivorState.DETECTED,
        SurvivorState.VALIDATING,
        SurvivorState.CONFIRMED,
    ):
        assert SurvivorState.DUPLICATE in SURVIVOR_TRANSITIONS[state]


def test_a_rejected_survivor_can_be_reopened() -> None:
    """A false-positive call that turns out to be wrong must be recoverable."""
    assert SurvivorState.DETECTED in SURVIVOR_TRANSITIONS[SurvivorState.REJECTED]


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
def test_delivered_is_only_reachable_from_delivery_initiated() -> None:
    """The evidence rule in state-machine form: nothing reaches DELIVERED
    without first having commanded a release."""
    sources = [s for s, t in DELIVERY_TRANSITIONS.items() if DeliveryState.DELIVERED in t]
    assert sources == [DeliveryState.DELIVERY_INITIATED]


def test_delivery_cannot_skip_arrival() -> None:
    assert DeliveryState.DELIVERY_INITIATED not in DELIVERY_TRANSITIONS[
        DeliveryState.EN_ROUTE
    ]
    assert DeliveryState.AT_TARGET in DELIVERY_TRANSITIONS[DeliveryState.EN_ROUTE]


def test_delivery_can_fail_from_any_working_state() -> None:
    for state in (
        DeliveryState.PENDING,
        DeliveryState.ASSIGNED,
        DeliveryState.ROUTE_PLANNED,
        DeliveryState.EN_ROUTE,
        DeliveryState.AT_TARGET,
        DeliveryState.DELIVERY_INITIATED,
    ):
        assert DeliveryState.FAILED in DELIVERY_TRANSITIONS[state]


def test_pending_delivery_cannot_jump_to_en_route() -> None:
    assert DeliveryState.EN_ROUTE not in DELIVERY_TRANSITIONS[DeliveryState.PENDING]


# ---------------------------------------------------------------------------
# sectors
# ---------------------------------------------------------------------------
def test_completed_sector_is_terminal() -> None:
    assert SECTOR_TRANSITIONS[SectorState.COMPLETED] == frozenset()


def test_sector_must_be_assigned_before_it_can_start() -> None:
    assert SectorState.IN_PROGRESS not in SECTOR_TRANSITIONS[SectorState.UNASSIGNED]
    assert SectorState.IN_PROGRESS in SECTOR_TRANSITIONS[SectorState.ASSIGNED]


def test_in_progress_sector_can_be_handed_back() -> None:
    """When a scout drops out mid-sector the work must be reassignable."""
    assert SectorState.UNASSIGNED in SECTOR_TRANSITIONS[SectorState.IN_PROGRESS]
