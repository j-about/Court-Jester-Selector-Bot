"""Async SQLAlchemy engine and session management.

Creates a single process-wide async engine and session factory from the
application ``Settings``, exposes a commit/rollback context manager for
per-request sessions, and provides helpers to initialize and dispose of
the engine over the bot's lifecycle.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from config import Settings

__all__ = [
    "close_db",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "init_db",
]


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(url: str | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call.

    Args:
        url: Optional override for the database URL. When omitted the URL
            is taken from ``Settings.async_database_url``.
    """
    global _engine, _sessionmaker
    if _engine is None:
        settings = Settings()  # ty: ignore[missing-argument]
        database_url = url or settings.async_database_url
        _engine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=15,
            pool_timeout=30,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, creating it if needed."""
    if _sessionmaker is None:
        get_engine()
    if _sessionmaker is None:
        raise RuntimeError("sessionmaker was not initialized")
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` that commits on success and rolls back on error.

    The session is always closed when the context exits.
    """
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Create every table declared on ``SQLModel.metadata`` if missing."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def close_db() -> None:
    """Dispose of the async engine and reset the cached session factory."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
