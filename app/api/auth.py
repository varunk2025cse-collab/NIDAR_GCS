"""Authentication and operator management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.deps import (
    AdminPrincipal,
    ClientIp,
    CurrentPrincipal,
    DbSession,
    RequestId,
    Services,
)
from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models.operator import Operator
from app.schemas.auth import (
    LoginRequest,
    OperatorCreateRequest,
    OperatorResponse,
    OperatorUpdateRequest,
    PasswordChangeRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    session: DbSession,
    services: Services,
    request_id: RequestId,
    source_ip: ClientIp,
) -> TokenResponse:
    """Exchange credentials for a bearer token.

    Failures are deliberately indistinguishable to the caller (unknown user
    and wrong password give the same message) but are logged and audited
    distinctly.
    """
    result = await session.execute(
        select(Operator).where(Operator.username == payload.username)
    )
    operator = result.scalar_one_or_none()

    if operator is None or not verify_password(payload.password, operator.password_hash):
        if operator is not None:
            operator.failed_login_count += 1
        await services.events.audit(
            action="LOGIN",
            result="FAILED",
            operator_username=payload.username,
            request_id=request_id,
            source_ip=source_ip,
            failure_reason="invalid credentials",
            session=session,
        )
        logger.warning("login_failed", username=payload.username, source_ip=source_ip)
        raise AuthenticationError("Invalid username or password")

    if not operator.is_active:
        await services.events.audit(
            action="LOGIN",
            result="REJECTED",
            operator_id=operator.id,
            operator_username=operator.username,
            request_id=request_id,
            source_ip=source_ip,
            failure_reason="account disabled",
            session=session,
        )
        raise AuthenticationError("This account is disabled")

    token, expires_at = create_access_token(
        operator.id, operator.username, operator.role, services.settings
    )
    operator.last_login_at = datetime.now(UTC)
    operator.failed_login_count = 0

    await services.events.audit(
        action="LOGIN",
        result="SUCCESS",
        operator_id=operator.id,
        operator_username=operator.username,
        operator_role=str(operator.role),
        request_id=request_id,
        source_ip=source_ip,
        session=session,
    )
    logger.info("login_success", username=operator.username, role=str(operator.role))
    return TokenResponse(
        access_token=token,
        expires_at=expires_at,
        operator=OperatorResponse.model_validate(operator),
    )


@router.get("/me", response_model=OperatorResponse)
async def me(principal: CurrentPrincipal, session: DbSession) -> OperatorResponse:
    operator = await session.get(Operator, principal.operator_id)
    if operator is None:
        raise NotFoundError("Operator account no longer exists")
    return OperatorResponse.model_validate(operator)


@router.post("/password", response_model=MessageResponse)
async def change_password(
    payload: PasswordChangeRequest,
    principal: CurrentPrincipal,
    session: DbSession,
    services: Services,
    request_id: RequestId,
) -> MessageResponse:
    operator = await session.get(Operator, principal.operator_id)
    if operator is None:
        raise NotFoundError("Operator account no longer exists")
    if not verify_password(payload.current_password, operator.password_hash):
        await services.events.audit(
            action="PASSWORD_CHANGE",
            result="FAILED",
            operator_id=operator.id,
            operator_username=operator.username,
            request_id=request_id,
            failure_reason="current password incorrect",
            session=session,
        )
        raise AuthenticationError("Current password is incorrect")

    operator.password_hash = hash_password(payload.new_password)
    await services.events.audit(
        action="PASSWORD_CHANGE",
        result="SUCCESS",
        operator_id=operator.id,
        operator_username=operator.username,
        request_id=request_id,
        session=session,
    )
    return MessageResponse(message="Password updated")


# ---------------------------------------------------------------------------
# operator administration
# ---------------------------------------------------------------------------
@router.get("/operators", response_model=list[OperatorResponse])
async def list_operators(
    _: AdminPrincipal, session: DbSession
) -> list[OperatorResponse]:
    result = await session.execute(select(Operator).order_by(Operator.username))
    return [OperatorResponse.model_validate(o) for o in result.scalars().all()]


@router.post(
    "/operators", response_model=OperatorResponse, status_code=status.HTTP_201_CREATED
)
async def create_operator(
    payload: OperatorCreateRequest,
    principal: AdminPrincipal,
    session: DbSession,
    services: Services,
    request_id: RequestId,
) -> OperatorResponse:
    existing = await session.execute(
        select(Operator).where(Operator.username == payload.username)
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError(
            f"Operator {payload.username} already exists",
            details={"username": payload.username},
        )

    operator = Operator(
        username=payload.username,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    session.add(operator)
    await session.flush()

    await services.events.audit(
        action="OPERATOR_CREATED",
        result="SUCCESS",
        operator_id=principal.operator_id,
        operator_username=principal.username,
        operator_role=str(principal.role),
        request_id=request_id,
        detail={"created_username": payload.username, "role": str(payload.role)},
        session=session,
    )
    logger.info("operator_created", username=payload.username, role=str(payload.role))
    return OperatorResponse.model_validate(operator)


@router.patch("/operators/{operator_id}", response_model=OperatorResponse)
async def update_operator(
    operator_id: uuid.UUID,
    payload: OperatorUpdateRequest,
    principal: AdminPrincipal,
    session: DbSession,
    services: Services,
    request_id: RequestId,
) -> OperatorResponse:
    operator = await session.get(Operator, operator_id)
    if operator is None:
        raise NotFoundError(
            f"Operator {operator_id} does not exist",
            details={"operator_id": str(operator_id)},
        )

    changes: dict[str, object] = {}
    if payload.full_name is not None:
        operator.full_name = payload.full_name
        changes["full_name"] = payload.full_name
    if payload.role is not None:
        operator.role = payload.role
        changes["role"] = str(payload.role)
    if payload.is_active is not None:
        if not payload.is_active and operator.id == principal.operator_id:
            raise ConflictError("An administrator cannot deactivate their own account")
        operator.is_active = payload.is_active
        changes["is_active"] = payload.is_active

    await services.events.audit(
        action="OPERATOR_UPDATED",
        result="SUCCESS",
        operator_id=principal.operator_id,
        operator_username=principal.username,
        operator_role=str(principal.role),
        request_id=request_id,
        detail={"target": operator.username, "changes": changes},
        session=session,
    )
    return OperatorResponse.model_validate(operator)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    principal: CurrentPrincipal,
    services: Services,
    session: DbSession,
    request_id: RequestId,
    request: Request,
) -> MessageResponse:
    """Record the logout.

    Tokens are stateless and short-lived, so this does not revoke anything --
    it records that the operator stepped away, which matters when reading the
    audit trail of a mission.
    """
    await services.events.audit(
        action="LOGOUT",
        result="SUCCESS",
        operator_id=principal.operator_id,
        operator_username=principal.username,
        operator_role=str(principal.role),
        request_id=request_id,
        source_ip=request.client.host if request.client else None,
        session=session,
    )
    return MessageResponse(
        message="Logged out",
        detail={"note": "The bearer token remains valid until it expires"},
    )
