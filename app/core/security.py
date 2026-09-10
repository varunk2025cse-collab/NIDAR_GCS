"""Authentication, password hashing and RBAC primitives.

Everything that can move a physical aircraft sits behind these checks.
"""

from __future__ import annotations

import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import Settings, get_settings
from app.core.enums import OperatorRole
from app.core.exceptions import AuthenticationError, AuthorizationError, RateLimitError

_pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def needs_rehash(hashed: str) -> bool:
    return _pwd_context.needs_update(hashed)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    """The authenticated caller, as decoded from a bearer token."""

    operator_id: uuid.UUID
    username: str
    role: OperatorRole
    token_id: str
    expires_at: datetime

    def require_role(self, *allowed: OperatorRole) -> None:
        if self.role not in allowed:
            raise AuthorizationError(
                f"Role {self.role} may not perform this action",
                details={"role": str(self.role), "required": [str(r) for r in allowed]},
            )

    @property
    def can_command(self) -> bool:
        return self.role in (OperatorRole.ADMIN, OperatorRole.OPERATOR)


def create_access_token(
    operator_id: uuid.UUID,
    username: str,
    role: OperatorRole,
    settings: Settings | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "sub": str(operator_id),
        "username": username,
        "role": str(role),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, expires


def decode_access_token(token: str, settings: Settings | None = None) -> TokenPrincipal:
    settings = settings or get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise AuthenticationError("Invalid or expired token") from exc

    try:
        return TokenPrincipal(
            operator_id=uuid.UUID(payload["sub"]),
            username=payload["username"],
            role=OperatorRole(payload["role"]),
            token_id=payload.get("jti", ""),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token payload") from exc


# ---------------------------------------------------------------------------
# Companion-computer API keys
# ---------------------------------------------------------------------------
def verify_companion_key(
    drone_id: str, presented_key: str, settings: Settings | None = None
) -> bool:
    """Constant-time check of a companion computer shared secret.

    A key is bound to one drone_id, so a compromised scout cannot post
    detections claiming to be another aircraft.
    """
    settings = settings or get_settings()
    expected = settings.companion_api_keys.get(drone_id.upper())
    if not expected or not presented_key:
        return False
    return hmac.compare_digest(expected, presented_key)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
@dataclass
class SlidingWindowRateLimiter:
    """In-process limiter for safety-critical command endpoints.

    Deliberately local: the GCS must work with no external services.
    """

    limit: int
    window_s: float = 60.0
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, [])
        cutoff = now - self.window_s
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= self.limit:
            retry_after = max(0.0, self.window_s - (now - bucket[0]))
            raise RateLimitError(
                "Command rate limit exceeded",
                details={"limit": self.limit, "window_s": self.window_s,
                         "retry_after_s": round(retry_after, 1)},
            )
        bucket.append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
