"""Canonical state vocabularies for the whole backend.

Every state machine in the system draws its states from here so that the
database, the REST API and the WebSocket contract cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------------------
# Connection / telemetry
# ---------------------------------------------------------------------------
class ConnectionState(StrEnum):
    """Lifecycle of the link to one physical aircraft.

    CONNECTED is only ever entered after real MAVLink traffic has been
    observed and the identity has been verified -- never because a socket
    exists.
    """

    DISCOVERING = "DISCOVERING"
    CONNECTING = "CONNECTING"
    IDENTIFYING = "IDENTIFYING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class TelemetryStatus(StrEnum):
    """Freshness of a single telemetry field."""

    FRESH = "FRESH"
    STALE = "STALE"
    NO_DATA = "NO_DATA"


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------
class MissionState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    PRECHECK = "PRECHECK"
    ARMING = "ARMING"
    TAKEOFF = "TAKEOFF"
    SEARCHING = "SEARCHING"
    SURVIVOR_RESPONSE = "SURVIVOR_RESPONSE"
    DELIVERY = "DELIVERY"
    RTL = "RTL"
    COMPLETED = "COMPLETED"

    PAUSED = "PAUSED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    PARTIAL_ABORT_FAILURE = "PARTIAL_ABORT_FAILURE"
    FAILED = "FAILED"
    EMERGENCY = "EMERGENCY"


MISSION_TERMINAL_STATES: frozenset[MissionState] = frozenset(
    {
        MissionState.COMPLETED,
        MissionState.ABORTED,
        MissionState.PARTIAL_ABORT_FAILURE,
        MissionState.FAILED,
    }
)

MISSION_ACTIVE_STATES: frozenset[MissionState] = frozenset(
    {
        MissionState.ARMING,
        MissionState.TAKEOFF,
        MissionState.SEARCHING,
        MissionState.SURVIVOR_RESPONSE,
        MissionState.DELIVERY,
        MissionState.RTL,
        MissionState.PAUSED,
        MissionState.EMERGENCY,
    }
)

#: Allowed mission transitions. Anything not listed is rejected.
MISSION_TRANSITIONS: dict[MissionState, frozenset[MissionState]] = {
    MissionState.DRAFT: frozenset({MissionState.READY, MissionState.FAILED}),
    MissionState.READY: frozenset({MissionState.DRAFT, MissionState.PRECHECK, MissionState.FAILED}),
    MissionState.PRECHECK: frozenset(
        {MissionState.READY, MissionState.ARMING, MissionState.FAILED, MissionState.ABORTING}
    ),
    MissionState.ARMING: frozenset(
        {
            MissionState.TAKEOFF,
            MissionState.ABORTING,
            MissionState.FAILED,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.TAKEOFF: frozenset(
        {
            MissionState.SEARCHING,
            MissionState.RTL,
            MissionState.ABORTING,
            MissionState.FAILED,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.SEARCHING: frozenset(
        {
            MissionState.SURVIVOR_RESPONSE,
            MissionState.DELIVERY,
            MissionState.PAUSED,
            MissionState.RTL,
            MissionState.ABORTING,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.SURVIVOR_RESPONSE: frozenset(
        {
            MissionState.SEARCHING,
            MissionState.DELIVERY,
            MissionState.PAUSED,
            MissionState.RTL,
            MissionState.ABORTING,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.DELIVERY: frozenset(
        {
            MissionState.SEARCHING,
            MissionState.SURVIVOR_RESPONSE,
            MissionState.PAUSED,
            MissionState.RTL,
            MissionState.ABORTING,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.PAUSED: frozenset(
        {
            MissionState.SEARCHING,
            MissionState.SURVIVOR_RESPONSE,
            MissionState.DELIVERY,
            MissionState.RTL,
            MissionState.ABORTING,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.RTL: frozenset(
        {
            MissionState.COMPLETED,
            MissionState.ABORTING,
            MissionState.FAILED,
            MissionState.EMERGENCY,
        }
    ),
    MissionState.EMERGENCY: frozenset(
        {MissionState.ABORTING, MissionState.RTL, MissionState.FAILED}
    ),
    MissionState.ABORTING: frozenset(
        {MissionState.ABORTED, MissionState.PARTIAL_ABORT_FAILURE, MissionState.FAILED}
    ),
    MissionState.COMPLETED: frozenset(),
    MissionState.ABORTED: frozenset(),
    MissionState.PARTIAL_ABORT_FAILURE: frozenset(),
    MissionState.FAILED: frozenset(),
}


# ---------------------------------------------------------------------------
# Search sectors
# ---------------------------------------------------------------------------
class SectorState(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    ABORTED = "ABORTED"


SECTOR_TRANSITIONS: dict[SectorState, frozenset[SectorState]] = {
    SectorState.UNASSIGNED: frozenset({SectorState.ASSIGNED, SectorState.BLOCKED}),
    SectorState.ASSIGNED: frozenset(
        {
            SectorState.IN_PROGRESS,
            SectorState.UNASSIGNED,
            SectorState.BLOCKED,
            SectorState.ABORTED,
        }
    ),
    SectorState.IN_PROGRESS: frozenset(
        {
            SectorState.COMPLETED,
            SectorState.BLOCKED,
            SectorState.ABORTED,
            SectorState.UNASSIGNED,
        }
    ),
    SectorState.BLOCKED: frozenset({SectorState.UNASSIGNED, SectorState.ABORTED}),
    SectorState.COMPLETED: frozenset(),
    SectorState.ABORTED: frozenset({SectorState.UNASSIGNED}),
}


# ---------------------------------------------------------------------------
# Survivors
# ---------------------------------------------------------------------------
class SurvivorState(StrEnum):
    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    CONFIRMED = "CONFIRMED"
    PENDING_DELIVERY = "PENDING_DELIVERY"
    ASSIGNED = "ASSIGNED"
    DELIVERY_IN_PROGRESS = "DELIVERY_IN_PROGRESS"
    DELIVERED = "DELIVERED"

    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


SURVIVOR_TRANSITIONS: dict[SurvivorState, frozenset[SurvivorState]] = {
    SurvivorState.DETECTED: frozenset(
        {
            SurvivorState.VALIDATING,
            SurvivorState.CONFIRMED,
            SurvivorState.DUPLICATE,
            SurvivorState.REJECTED,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.VALIDATING: frozenset(
        {
            SurvivorState.CONFIRMED,
            SurvivorState.DUPLICATE,
            SurvivorState.REJECTED,
            SurvivorState.LOST,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.CONFIRMED: frozenset(
        {
            SurvivorState.PENDING_DELIVERY,
            SurvivorState.DUPLICATE,
            SurvivorState.LOST,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.PENDING_DELIVERY: frozenset(
        {
            SurvivorState.ASSIGNED,
            SurvivorState.CONFIRMED,
            SurvivorState.LOST,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.ASSIGNED: frozenset(
        {
            SurvivorState.DELIVERY_IN_PROGRESS,
            SurvivorState.PENDING_DELIVERY,
            SurvivorState.LOST,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.DELIVERY_IN_PROGRESS: frozenset(
        {
            SurvivorState.DELIVERED,
            SurvivorState.PENDING_DELIVERY,
            SurvivorState.LOST,
            SurvivorState.CANCELLED,
        }
    ),
    SurvivorState.DELIVERED: frozenset(),
    SurvivorState.DUPLICATE: frozenset({SurvivorState.DETECTED}),
    SurvivorState.REJECTED: frozenset({SurvivorState.DETECTED}),
    SurvivorState.LOST: frozenset({SurvivorState.DETECTED, SurvivorState.CONFIRMED}),
    SurvivorState.CANCELLED: frozenset(),
}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
class DeliveryState(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    ROUTE_PLANNED = "ROUTE_PLANNED"
    EN_ROUTE = "EN_ROUTE"
    AT_TARGET = "AT_TARGET"
    DELIVERY_INITIATED = "DELIVERY_INITIATED"
    DELIVERED = "DELIVERED"
    RETURNING = "RETURNING"

    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


DELIVERY_TRANSITIONS: dict[DeliveryState, frozenset[DeliveryState]] = {
    DeliveryState.PENDING: frozenset(
        {DeliveryState.ASSIGNED, DeliveryState.CANCELLED, DeliveryState.FAILED}
    ),
    DeliveryState.ASSIGNED: frozenset(
        {DeliveryState.ROUTE_PLANNED, DeliveryState.CANCELLED, DeliveryState.FAILED}
    ),
    DeliveryState.ROUTE_PLANNED: frozenset(
        {DeliveryState.EN_ROUTE, DeliveryState.CANCELLED, DeliveryState.FAILED}
    ),
    DeliveryState.EN_ROUTE: frozenset(
        {DeliveryState.AT_TARGET, DeliveryState.CANCELLED, DeliveryState.FAILED}
    ),
    DeliveryState.AT_TARGET: frozenset(
        {DeliveryState.DELIVERY_INITIATED, DeliveryState.CANCELLED, DeliveryState.FAILED}
    ),
    DeliveryState.DELIVERY_INITIATED: frozenset(
        {DeliveryState.DELIVERED, DeliveryState.FAILED, DeliveryState.CANCELLED}
    ),
    DeliveryState.DELIVERED: frozenset({DeliveryState.RETURNING}),
    DeliveryState.RETURNING: frozenset({DeliveryState.FAILED}),
    DeliveryState.FAILED: frozenset(),
    DeliveryState.CANCELLED: frozenset(),
}

DELIVERY_ACTIVE_STATES: frozenset[DeliveryState] = frozenset(
    {
        DeliveryState.ASSIGNED,
        DeliveryState.ROUTE_PLANNED,
        DeliveryState.EN_ROUTE,
        DeliveryState.AT_TARGET,
        DeliveryState.DELIVERY_INITIATED,
        DeliveryState.RETURNING,
    }
)


class DeliveryConfirmationSource(StrEnum):
    PAYLOAD_MECHANISM = "PAYLOAD_MECHANISM"
    COMPANION_COMPUTER = "COMPANION_COMPUTER"
    OPERATOR = "OPERATOR"
    SENSOR = "SENSOR"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
class CommandType(StrEnum):
    ARM = "ARM"
    DISARM = "DISARM"
    TAKEOFF = "TAKEOFF"
    LAND = "LAND"
    RTL = "RTL"
    HOLD = "HOLD"
    PAUSE_MISSION = "PAUSE_MISSION"
    RESUME_MISSION = "RESUME_MISSION"
    UPLOAD_MISSION = "UPLOAD_MISSION"
    START_MISSION = "START_MISSION"
    CLEAR_MISSION = "CLEAR_MISSION"
    GOTO = "GOTO"
    UPLOAD_GEOFENCE = "UPLOAD_GEOFENCE"
    CLEAR_GEOFENCE = "CLEAR_GEOFENCE"
    RELEASE_PAYLOAD = "RELEASE_PAYLOAD"


class CommandState(StrEnum):
    """Lifecycle of a single command aimed at a physical aircraft.

    ``SENT_TO_PX4`` means the bytes left MAVSDK. It never means the aircraft
    did anything. Only ``COMPLETED`` -- which requires an observed state
    change -- means the aircraft acted.
    """

    REQUESTED = "COMMAND_REQUESTED"
    SENT_TO_MAVSDK = "SENT_TO_MAVSDK"
    SENT_TO_PX4 = "SENT_TO_PX4"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    STATE_CHANGED = "STATE_CHANGED"
    COMPLETED = "COMPLETED"

    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


COMMAND_TERMINAL_STATES: frozenset[CommandState] = frozenset(
    {
        CommandState.COMPLETED,
        CommandState.TIMEOUT,
        CommandState.REJECTED,
        CommandState.FAILED,
        CommandState.UNKNOWN,
    }
)


# ---------------------------------------------------------------------------
# Alerts / health
# ---------------------------------------------------------------------------
class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


class AlertCategory(StrEnum):
    BATTERY = "BATTERY"
    GPS = "GPS"
    CONNECTION = "CONNECTION"
    TELEMETRY_FRESHNESS = "TELEMETRY_FRESHNESS"
    GEOFENCE = "GEOFENCE"
    MISSION_TIMEOUT = "MISSION_TIMEOUT"
    FLIGHT_MODE = "FLIGHT_MODE"
    HEALTH = "HEALTH"
    MISSION_DEVIATION = "MISSION_DEVIATION"
    DELIVERY = "DELIVERY"
    SURVIVOR = "SURVIVOR"
    SYSTEM = "SYSTEM"
    COMMAND = "COMMAND"


class ComponentStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class OperatorRole(StrEnum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


class DetectionSource(StrEnum):
    ONBOARD_AI = "ONBOARD_AI"
    OPERATOR = "OPERATOR"
    EXTERNAL = "EXTERNAL"


class GeofenceType(StrEnum):
    INCLUSION = "INCLUSION"
    EXCLUSION = "EXCLUSION"


class GeofenceStatus(StrEnum):
    INSIDE = "INSIDE"
    NEAR_BOUNDARY = "NEAR_BOUNDARY"
    BREACHED = "BREACHED"
    UNKNOWN = "UNKNOWN"


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
