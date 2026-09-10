"""Shared FastAPI dependencies.

Authentication, RBAC and container access. Every endpoint that can move an
aircraft depends on :func:`require_operator`, which is the single place role
enforcement happens for command routes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.enums import OperatorRole
from app.core.exceptions import AuthenticationError, AuthorizationError, NotFoundError
from app.core.logging import operator_id_ctx, request_id_ctx
from app.core.security import TokenPrincipal, decode_access_token, verify_companion_key
from app.database.session import get_sessionmaker
from app.models.operator import Operator

_bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - startup guarantees this
        raise RuntimeError("Application container is not initialised")
    return container


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenPrincipal:
    """Authenticate the caller and confirm the account is still active.

    The database check matters: a token issued to an operator who has since
    been deactivated must stop working immediately, not at token expiry.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    principal = decode_access_token(credentials.credentials)

    operator = await session.get(Operator, principal.operator_id)
    if operator is None or not operator.is_active:
        raise AuthenticationError("Operator account is inactive or no longer exists")
    if operator.role is not principal.role:
        # Role changed since the token was issued; the stored role wins.
        principal = TokenPrincipal(
            operator_id=principal.operator_id,
            username=principal.username,
            role=operator.role,
            token_id=principal.token_id,
            expires_at=principal.expires_at,
        )

    operator_id_ctx.set(str(principal.operator_id))
    return principal


CurrentPrincipal = Annotated[TokenPrincipal, Depends(get_principal)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
Services = Annotated[Container, Depends(get_container)]


async def require_viewer(principal: CurrentPrincipal) -> TokenPrincipal:
    """Any authenticated role may read."""
    return principal


async def require_operator(principal: CurrentPrincipal) -> TokenPrincipal:
    """Required for anything that can move an aircraft or change a mission."""
    if principal.role not in (OperatorRole.ADMIN, OperatorRole.OPERATOR):
        raise AuthorizationError(
            "This action requires the OPERATOR or ADMIN role",
            details={"role": str(principal.role)},
        )
    return principal


async def require_admin(principal: CurrentPrincipal) -> TokenPrincipal:
    """Configuration and account management."""
    if principal.role is not OperatorRole.ADMIN:
        raise AuthorizationError(
            "This action requires the ADMIN role", details={"role": str(principal.role)}
        )
    return principal


OperatorPrincipal = Annotated[TokenPrincipal, Depends(require_operator)]
AdminPrincipal = Annotated[TokenPrincipal, Depends(require_admin)]
ViewerPrincipal = Annotated[TokenPrincipal, Depends(require_viewer)]


async def companion_identity(
    x_drone_id: Annotated[str | None, Header(alias="X-Drone-Id")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> str:
    """Authenticate a companion computer posting detection events.

    The key is bound to one drone_id, so a compromised scout cannot post
    detections claiming to be another aircraft. Returns the authenticated
    drone_id, which the caller must use in place of any drone_id in the body.
    """
    if not x_drone_id or not x_api_key:
        raise AuthenticationError(
            "Companion computers must present X-Drone-Id and X-Api-Key headers"
        )
    drone_id = x_drone_id.strip().upper()
    if not verify_companion_key(drone_id, x_api_key):
        raise AuthenticationError(
            "Invalid companion computer credentials",
            details={"drone_id": drone_id},
        )
    return drone_id


def get_request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None) or request_id_ctx.get()
    return rid or str(uuid.uuid4())


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


RequestId = Annotated[str, Depends(get_request_id)]
ClientIp = Annotated[str | None, Depends(client_ip)]


def check_command_rate_limit(
    container: Container, principal: TokenPrincipal, drone_id: str
) -> None:
    """Throttle commands per operator per aircraft."""
    container.command_rate_limiter.check(f"{principal.operator_id}:{drone_id}")


async def resolve_drone_uuid(container: Container, drone_id: str) -> uuid.UUID:
    drone_uuid = container.fleet.drone_uuid(drone_id)
    if drone_uuid is None:
        raise NotFoundError(
            f"Drone {drone_id} has no database record",
            details={"drone_id": drone_id},
        )
    return drone_uuid
