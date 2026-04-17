"""Integration tests for the Alembic migration stack and async session plumbing."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from config import Settings

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="TEST_DATABASE_URL not set; skipping DB integration tests",
)

_ROOT = Path(__file__).resolve().parent.parent


def _components_from_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    assert parsed.username and parsed.password and parsed.hostname and parsed.path
    return {
        "POSTGRES_HOST": parsed.hostname,
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_USER": unquote(parsed.username),
        "POSTGRES_PASSWORD": unquote(parsed.password),
        "POSTGRES_DB": parsed.path.lstrip("/"),
    }


def _alembic_cfg(async_url: str) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    for k, v in _components_from_url(async_url).items():
        os.environ[k] = v
    return cfg


@pytest.fixture
def async_url() -> str:
    return os.environ["TEST_DATABASE_URL"]


def _reset_schema(async_url: str) -> None:
    """Drop and recreate the ``public`` schema so Alembic starts from a clean slate.

    The rest of the suite uses ``SQLModel.metadata.create_all`` rather
    than Alembic, so the Alembic version table can be absent while the
    application tables exist. In that state ``downgrade('base')`` is a
    no-op and the following ``upgrade`` fails with ``DuplicateTable``; a
    full schema reset isolates the migration tests from that interference.
    """
    s = Settings(_env_file=None, TG_BOT_TOKEN="t", **_components_from_url(async_url))  # type: ignore[arg-type]
    from sqlalchemy import create_engine

    engine = create_engine(s.sync_database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def test_upgrade_creates_tables(async_url: str) -> None:
    _reset_schema(async_url)
    cfg = _alembic_cfg(async_url)
    command.upgrade(cfg, "head")

    s = Settings(_env_file=None, TG_BOT_TOKEN="t", **_components_from_url(async_url))  # type: ignore[arg-type]
    from sqlalchemy import create_engine

    engine = create_engine(s.sync_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name")
        ).fetchall()
    names = {r[0] for r in rows}
    assert {"group", "player", "draw"}.issubset(names)
    engine.dispose()


def test_downgrade_removes_tables(async_url: str) -> None:
    _reset_schema(async_url)
    cfg = _alembic_cfg(async_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    s = Settings(_env_file=None, TG_BOT_TOKEN="t", **_components_from_url(async_url))  # type: ignore[arg-type]
    from sqlalchemy import create_engine

    engine = create_engine(s.sync_database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN ('group','player','draw')"
            )
        ).fetchall()
    assert rows == []
    engine.dispose()


async def test_async_session_roundtrip(async_url: str) -> None:
    engine = create_async_engine(async_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    await engine.dispose()
