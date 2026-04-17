"""Tests for silent player registration and admin-menu syncing on status transitions."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler

import database
from config import Settings
from handlers.registration import (
    intercept_message,
    register_registration_handlers,
)
from models import Group, Player
from tests.conftest import requires_db
from utils.command_menu import admin_commands


def test_register_registration_handlers_adds_message_handler() -> None:
    app = MagicMock(spec=Application)
    register_registration_handlers(app)
    app.add_handler.assert_called_once()
    (handler,), _ = app.add_handler.call_args
    assert isinstance(handler, MessageHandler)
    assert handler.filters is not None
    repr_filter = repr(handler.filters)
    # The filter must require a group chat and exclude commands (inverted COMMAND filter).
    assert "ChatType.GROUPS" in repr_filter
    assert "COMMAND" in repr_filter
    assert "inverted" in repr_filter.lower()


def _fake_update(
    *,
    chat_id: int,
    user_id: int,
    first_name: str | None = "Alice",
    last_name: str | None = "Doe",
    username: str | None = "alice",
    is_bot: bool = False,
) -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.first_name = first_name
    user.last_name = last_name
    user.username = username
    user.is_bot = is_bot
    chat = MagicMock()
    chat.id = chat_id
    message = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    return update


def _fake_context(
    *,
    bot: AsyncMock | None = None,
    settings: Settings | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    app = MagicMock()
    app.bot_data = {"settings": settings or _test_settings()}
    ctx.application = app
    return ctx


def _test_settings() -> Settings:
    import os

    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    return Settings()  # ty: ignore[missing-argument]


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Redirect ``database.get_session`` to the test sessionmaker.

    Patches the module-level ``_sessionmaker`` so ``get_session`` yields
    sessions bound to the test engine, and sets a non-``None`` ``_engine``
    to short-circuit ``get_engine`` and prevent it from creating a real
    engine against the production database URL.
    """
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


@requires_db
async def test_new_user_creates_player_with_default_weight(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="active", telegram_id=1000, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="member")
    update = _fake_update(chat_id=1000, user_id=42)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    result = await session.execute(select(Player).where(Player.group_id == group.id, Player.telegram_id == 42))
    player = result.scalars().first()
    assert player is not None
    assert player.weight == ctx.application.bot_data["settings"].DEFAULT_WEIGHT
    assert player.status == "member"
    assert player.telegram_first_name == "Alice"
    assert player.telegram_last_name == "Doe"
    assert player.telegram_username == "alice"
    bot.send_message.assert_not_called()
    update.effective_message.reply_text.assert_not_called()


@requires_db
async def test_existing_user_is_updated_not_duplicated(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="active", telegram_id=1001, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="member",
            group_id=group.id,
            telegram_id=77,
            telegram_first_name="Old",
            telegram_last_name=None,
            telegram_username=None,
            weight=5,
        )
    )
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="administrator")
    update = _fake_update(
        chat_id=1001,
        user_id=77,
        first_name="New",
        last_name="Name",
        username="newhandle",
    )
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    result = await session.execute(select(Player).where(Player.group_id == group.id, Player.telegram_id == 77))
    players = list(result.scalars())
    assert len(players) == 1
    p = players[0]
    assert p.telegram_first_name == "New"
    assert p.telegram_last_name == "Name"
    assert p.telegram_username == "newhandle"
    assert p.status == "administrator"
    # Weight is preserved across profile updates rather than reset to the default.
    assert p.weight == 5


@requires_db
async def test_unknown_group_is_noop(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="member")
    update = _fake_update(chat_id=9999, user_id=1)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    result = await session.execute(select(Player))
    assert result.scalars().first() is None
    bot.send_message.assert_not_called()


@requires_db
async def test_get_chat_member_failure_is_swallowed(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="active", telegram_id=1002, telegram_title="T")
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.side_effect = TelegramError("boom")
    update = _fake_update(chat_id=1002, user_id=3)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    result = await session.execute(select(Player).where(Player.telegram_id == 3))
    assert result.scalars().first() is None
    bot.send_message.assert_not_called()
    update.effective_message.reply_text.assert_not_called()


async def test_bot_user_is_ignored() -> None:
    bot = AsyncMock()
    update = _fake_update(chat_id=1, user_id=2, is_bot=True)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    bot.get_chat_member.assert_not_called()
    bot.send_message.assert_not_called()


@requires_db
async def test_new_admin_player_in_approved_group_sets_admin_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(status="active", telegram_id=1200, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="administrator")
    update = _fake_update(chat_id=1200, user_id=810)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    settings = ctx.application.bot_data["settings"]
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == admin_commands(settings)
    assert kwargs["scope"].chat_id == 810


@requires_db
async def test_existing_admin_posting_again_does_not_resync_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(status="active", telegram_id=1201, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="administrator",
            group_id=group.id,
            telegram_id=811,
            telegram_first_name="A",
            weight=3,
        )
    )
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="administrator")
    update = _fake_update(chat_id=1201, user_id=811)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    # The player's admin status did not change, so no menu sync should fire.
    bot.set_my_commands.assert_not_awaited()
    bot.delete_my_commands.assert_not_awaited()


@requires_db
async def test_demoted_admin_in_only_approved_group_clears_admin_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(status="active", telegram_id=1202, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="administrator",
            group_id=group.id,
            telegram_id=812,
            telegram_first_name="A",
            weight=3,
        )
    )
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="member")
    update = _fake_update(chat_id=1202, user_id=812)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    bot.delete_my_commands.assert_awaited_once()
    assert bot.delete_my_commands.await_args.kwargs["scope"].chat_id == 812


@requires_db
async def test_status_change_in_unapproved_group_does_not_sync(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(status="active", telegram_id=1203, telegram_title="T", approved=False)
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="administrator")
    update = _fake_update(chat_id=1203, user_id=813)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    bot.set_my_commands.assert_not_awaited()
    bot.delete_my_commands.assert_not_awaited()
    # Unapproved groups must not accrue player rows, regardless of sender status.
    result = await session.execute(select(Player).where(Player.group_id == group.id))
    assert result.scalars().first() is None


@requires_db
async def test_unapproved_group_does_not_register_player(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="active", telegram_id=1300, telegram_title="T", approved=False)
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="member")
    update = _fake_update(chat_id=1300, user_id=900)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    result = await session.execute(select(Player).where(Player.group_id == group.id))
    assert result.scalars().first() is None
    bot.set_my_commands.assert_not_awaited()
    bot.delete_my_commands.assert_not_awaited()


@requires_db
async def test_non_admin_status_change_below_threshold_does_not_sync(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(status="active", telegram_id=1204, telegram_title="T", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="member",
            group_id=group.id,
            telegram_id=814,
            telegram_first_name="M",
            weight=3,
        )
    )
    await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = MagicMock(status="restricted")
    update = _fake_update(chat_id=1204, user_id=814)
    ctx = _fake_context(bot=bot)

    await intercept_message(update, ctx)

    # ``member`` → ``restricted`` stays below the admin threshold, so no menu sync occurs.
    bot.set_my_commands.assert_not_awaited()
    bot.delete_my_commands.assert_not_awaited()
