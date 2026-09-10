"""Authentication and operator schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import OperatorRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    operator: OperatorResponse


class OperatorResponse(ORMModel):
    id: uuid.UUID
    username: str
    full_name: str | None = None
    role: OperatorRole
    is_active: bool
    last_login_at: datetime | None = None


class OperatorCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(..., min_length=12, max_length=256)
    full_name: str | None = Field(default=None, max_length=128)
    role: OperatorRole = OperatorRole.VIEWER


class OperatorUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=128)
    role: OperatorRole | None = None
    is_active: bool | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=12, max_length=256)


TokenResponse.model_rebuild()
