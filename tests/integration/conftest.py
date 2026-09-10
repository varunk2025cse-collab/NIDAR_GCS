"""Integration fixtures.

These tests need a real PostgreSQL + PostGIS database, because that is the
only way to exercise the parts that matter here: the spatial queries, the
idempotency constraint, and the transactional behaviour of the state machines.
Faking those would test the fake.

Enable them with a throwaway database:

    createdb nidar_gcs_test
    psql -d nidar_gcs_test -c "CREATE EXTENSION postgis"
    TEST_DATABASE_URL=postgresql+asyncpg://gcs:gcs@localhost:5432/nidar_gcs_test \\
        pytest tests/integration

Without ``TEST_DATABASE_URL`` they skip rather than fail.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests are skipped")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[object]:
    engine = create_async_engine(database_url, poolclass=None)
    async with engine.begin() as connection:
        from sqlalchemy import text

        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def wired_database(engine, settings) -> AsyncIterator[None]:
    """Point the global session factory at the test database.

    The unit-test isolation fixture in the parent conftest disables the
    database entirely; this puts a real one back for these tests only.
    """
    import app.database.session as session_module

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original = session_module.get_sessionmaker
    session_module.get_sessionmaker = lambda: factory  # type: ignore[assignment]
    try:
        yield
    finally:
        session_module.get_sessionmaker = original  # type: ignore[assignment]
