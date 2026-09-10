"""Shared request/response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import CheckStatus, TelemetryStatus
from app.core.geo import is_valid_coordinate

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The single error shape every endpoint returns."""

    error: ErrorDetail


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int | None = None
    limit: int
    offset: int


class Coordinate(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)

    @field_validator("longitude")
    @classmethod
    def _check_pair(cls, v: float, info: Any) -> float:
        latitude = info.data.get("latitude")
        if latitude is not None and not is_valid_coordinate(latitude, v):
            raise ValueError("coordinate is not a valid position")
        return v

    def as_tuple(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


class PolygonInput(BaseModel):
    """A closed ring, given as an ordered list of coordinates."""

    points: list[Coordinate] = Field(..., min_length=3, max_length=500)

    def as_tuples(self) -> list[tuple[float, float]]:
        return [p.as_tuple() for p in self.points]


class TelemetryField(BaseModel):
    """A telemetry value that always travels with its freshness.

    ``value`` is ``None`` whenever ``status`` is NO_DATA. The pair is never
    split, so a client cannot render a number without knowing how old it is.
    """

    value: Any | None = None
    timestamp: datetime | None = None
    age_s: float | None = None
    status: TelemetryStatus


class CheckResultSchema(BaseModel):
    check: str
    status: CheckStatus
    drone: str | None = None
    reason: str | None = None
    observed: Any | None = None


class GeoJSONFeature(BaseModel):
    type: str = "Feature"
    geometry: dict[str, Any]
    properties: dict[str, Any] = Field(default_factory=dict)


class GeoJSONFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[GeoJSONFeature] = Field(default_factory=list)


class CommandAcceptedResponse(BaseModel):
    """Result of a command aimed at a physical aircraft.

    ``success`` is true only when the aircraft was observed to comply.
    ``acknowledged`` alone means the flight controller answered -- not that
    anything moved.
    """

    command_id: uuid.UUID | None
    command_type: str
    drone_id: str
    state: str
    acknowledged: bool
    verified: bool
    success: bool
    result_code: str
    detail: str | None = None
    idempotent_replay: bool = False
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime
    completed_at: datetime | None = None


class MessageResponse(BaseModel):
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
