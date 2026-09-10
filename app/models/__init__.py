"""ORM models.

Imported as a package so Alembic autogenerate and ``Base.metadata`` see every
table.
"""

from app.database.base import Base
from app.models.command import CommandTransition, DroneCommand
from app.models.delivery import DeliveryEvent, DeliveryTask
from app.models.drone import Drone, DroneConnection, DroneStateSnapshot
from app.models.event import Alert, AuditLog, MissionEvent, SystemHealthSample
from app.models.mission import (
    Geofence,
    Mission,
    MissionDroneAssignment,
    MissionGeofence,
    MissionOperator,
)
from app.models.operator import Operator
from app.models.search_sector import SearchAssignment, SearchSector, Waypoint
from app.models.survivor import Survivor, SurvivorDetection, SurvivorObservation
from app.models.telemetry import TelemetrySample

__all__ = [
    "Alert",
    "AuditLog",
    "Base",
    "CommandTransition",
    "DeliveryEvent",
    "DeliveryTask",
    "Drone",
    "DroneCommand",
    "DroneConnection",
    "DroneStateSnapshot",
    "Geofence",
    "Mission",
    "MissionDroneAssignment",
    "MissionEvent",
    "MissionGeofence",
    "MissionOperator",
    "Operator",
    "SearchAssignment",
    "SearchSector",
    "Survivor",
    "SurvivorDetection",
    "SurvivorObservation",
    "SystemHealthSample",
    "TelemetrySample",
    "Waypoint",
]
