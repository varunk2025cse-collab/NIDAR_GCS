"""Operator accounts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import OperatorRole
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Operator(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operators"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[OperatorRole] = mapped_column(
        SAEnum(OperatorRole, name="operator_role", native_enum=False, length=16),
        nullable=False,
        default=OperatorRole.VIEWER,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Operator {self.username} {self.role}>"
