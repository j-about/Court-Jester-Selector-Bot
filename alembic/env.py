"""Alembic migration environment.

Loads the project ``Settings`` to resolve the database URL, registers the
SQLModel metadata for the declared tables, and dispatches to offline or
online migration execution based on the current Alembic context.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool
from sqlmodel import SQLModel

from alembic import context

# Ensure repo root is importable so `config` and `models` resolve.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import Settings  # noqa: E402
from models import Draw, Group, Player  # noqa: E402, F401  (register metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_settings = Settings()  # ty: ignore[missing-argument]
_url = _settings.sync_database_url
config.set_main_option("sqlalchemy.url", _url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live database connection.

    Emits SQL using the configured URL and literal parameter binding, then
    executes any pending migrations inside a single Alembic transaction.
    """
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection.

    Creates an engine with a null pool, opens a single connection, and runs
    any pending migrations inside an Alembic transaction.
    """
    connectable = create_engine(_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
