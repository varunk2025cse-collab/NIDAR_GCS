"""Domain exceptions and the wire error contract.

Every error that reaches a client is shaped as::

    {"error": {"code", "message", "details", "request_id"}}

Stack traces never leave the process; they go to the log with the same
request_id so an operator report can be correlated with the server log.
"""

from __future__ import annotations

from typing import Any


class GCSError(Exception):
    """Base class for every domain error in the backend."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    message: str = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


# --- configuration ---------------------------------------------------------
class ConfigurationError(GCSError):
    code = "CONFIGURATION_ERROR"
    status_code = 500
    message = "Backend configuration is invalid"


# --- auth ------------------------------------------------------------------
class AuthenticationError(GCSError):
    code = "AUTHENTICATION_FAILED"
    status_code = 401
    message = "Authentication failed"


class AuthorizationError(GCSError):
    code = "NOT_AUTHORIZED"
    status_code = 403
    message = "Operator is not authorised for this action"


# --- generic resource ------------------------------------------------------
class NotFoundError(GCSError):
    code = "NOT_FOUND"
    status_code = 404
    message = "Resource not found"


class ValidationError(GCSError):
    code = "VALIDATION_ERROR"
    status_code = 422
    message = "Request failed validation"


class ConflictError(GCSError):
    code = "CONFLICT"
    status_code = 409
    message = "Conflicting state"


class RateLimitError(GCSError):
    code = "RATE_LIMITED"
    status_code = 429
    message = "Too many requests"


# --- drone / link ----------------------------------------------------------
class DroneNotFoundError(NotFoundError):
    code = "DRONE_NOT_FOUND"
    message = "Unknown drone"


class DroneNotConnectedError(GCSError):
    code = "DRONE_NOT_CONNECTED"
    status_code = 409
    message = "Drone is not connected"


class DroneNotReadyError(GCSError):
    code = "DRONE_NOT_READY"
    status_code = 409
    message = "Drone is not ready for this operation"


class DroneIdentityError(GCSError):
    code = "DRONE_IDENTITY_MISMATCH"
    status_code = 409
    message = "Observed MAVLink identity does not match the configured drone"


class TelemetryUnavailableError(GCSError):
    code = "TELEMETRY_UNAVAILABLE"
    status_code = 409
    message = "Required telemetry is unavailable or stale"


# --- commands --------------------------------------------------------------
class CommandError(GCSError):
    code = "COMMAND_FAILED"
    status_code = 502
    message = "Command failed"


class CommandRejectedError(CommandError):
    code = "COMMAND_REJECTED"
    status_code = 409
    message = "Command rejected by the flight controller"


class CommandTimeoutError(CommandError):
    code = "COMMAND_TIMEOUT"
    status_code = 504
    message = "Command timed out without acknowledgement"


class CommandInProgressError(ConflictError):
    code = "COMMAND_IN_PROGRESS"
    message = "Another command for this drone is still in progress"


# --- mission ---------------------------------------------------------------
class MissionNotFoundError(NotFoundError):
    code = "MISSION_NOT_FOUND"
    message = "Unknown mission"


class InvalidStateTransitionError(ConflictError):
    code = "INVALID_STATE_TRANSITION"
    message = "State transition is not permitted"

    def __init__(self, entity: str, current: str, requested: str) -> None:
        super().__init__(
            f"{entity} cannot move from {current} to {requested}",
            details={"entity": entity, "current_state": current, "requested_state": requested},
        )


class PreflightFailedError(GCSError):
    code = "PREFLIGHT_FAILED"
    status_code = 409
    message = "Preflight checks failed; mission start is blocked"


class MissionTimeoutError(GCSError):
    code = "MISSION_TIMEOUT"
    status_code = 409
    message = "Mission exceeded its maximum permitted duration"


# --- survivors / delivery --------------------------------------------------
class DetectionRejectedError(ValidationError):
    code = "DETECTION_REJECTED"
    message = "Detection event rejected"


class DeliveryRejectedUnsafeError(GCSError):
    code = "DELIVERY_REJECTED_UNSAFE"
    status_code = 409
    message = "Delivery rejected: aircraft is not safe to dispatch"


class NoDeliveryDroneAvailableError(GCSError):
    code = "NO_DELIVERY_DRONE_AVAILABLE"
    status_code = 409
    message = "No delivery-capable drone is available"


# --- geofence --------------------------------------------------------------
class GeofenceViolationError(GCSError):
    code = "GEOFENCE_VIOLATION"
    status_code = 409
    message = "Requested location lies outside the mission geofence"
