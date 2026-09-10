"""Async SQLAlchemy engine and session management.

The database is local (PostgreSQL + PostGIS on the GCS machine or on the local
network). Losing it must degrade the GCS to "no persistence" rather than
taking flight supervision down with it, so callers that only need to *record*
something use :func:`session_scope_optional`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    settings = get_settings()
    url = database_url or settings.database_url
    _engine = create_async_engine(
        url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    _sessionmaker = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope_optional() -> AsyncIterator[AsyncSession | None]:
    """Best-effort persistence.

    Yields ``None`` when the database cannot be reached, so background
    supervision loops keep running through a database outage rather than
    taking flight supervision down with it. The outage is logged and surfaced
    through the system-health endpoint; it is never silently swallowed.

    Connectivity is established *before* the yield. That separation matters:
    a failure to reach the database yields ``None``, while an error raised by
    the caller body propagates unchanged instead of being mislabelled as a
    database outage.
    """
    session: AsyncSession | None = None
    try:
        session = get_sessionmaker()()
        # Force the connection now so an unreachable database is detected
        # here rather than part-way through the caller body.
        await session.connection()
    except Exception as exc:
        if session is not None:
            with suppress(Exception):
                await session.close()
        logger.error(
            "database_unavailable", error=str(exc), error_type=type(exc).__name__
        )
        yield None
        return

    try:
        yield session
        await session.commit()
    except Exception:
        with suppress(Exception):
            await session.rollback()
        raise
    finally:
        with suppress(Exception):
            await session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
