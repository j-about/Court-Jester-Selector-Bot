"""Pytest fixtures shared across the test suite.

Provides a neutralized Sentry client, a per-test database sessionmaker
that starts from a freshly reset schema, and a convenience ``session``
fixture yielding an ``AsyncSession`` bound to that sessionmaker.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel


@pytest.fixture(autouse=True)
def _sentry_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize Sentry for every test by unsetting ``SENTRY_DSN`` and reinitialising with no DSN."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    sentry_sdk.init(dsn=None)


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an ``async_sessionmaker`` bound to a freshly reset schema.

    Drops and recreates every declared table before each test, chosen over
    SAVEPOINT-based isolation for simplicity given the suite's current
    size (around 120 tests completing in a few seconds).
    """
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as s:
        yield s
