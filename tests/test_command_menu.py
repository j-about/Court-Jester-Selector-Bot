"""Tests for command-menu construction, per-scope syncing, and reconciliation."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

import database
from config import Settings
from models import Group, Player
from tests.conftest import requires_db
from utils.command_menu import (
    CHANGE_WEIGHT_COMMAND,
    CHANGE_WEIGHT_DESCRIPTION,
    admin_commands,
    game_commands,
    is_admin_user,
    is_status_admin,
    reconcile_all_menus,
    sync_group_menu,
    sync_user_admin_menu,
)


def _test_settings(
    *,
    admin_ids: list[int] | None = None,
    allowed_statuses: list[str] | None = None,
) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    os.environ["TG_BOT_ADMIN_RIGHTS_USER_IDS"] = ",".join(str(i) for i in admin_ids) if admin_ids else ""
    os.environ["TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS"] = ",".join(
        allowed_statuses if allowed_statuses is not None else ["creator", "administrator"]
    )
    return Settings()  # ty: ignore[missing-argument]


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


def test_game_commands_uses_configured_names_and_descriptions() -> None:
    settings = _test_settings()
    cmds = game_commands(settings)
    assert [c.command for c in cmds] == [
        settings.PICK_PLAYER_COMMAND,
        settings.SHOW_LEADERBOARD_COMMAND,
        settings.SHOW_PERSONAL_STATS_COMMAND,
    ]
    assert [c.description for c in cmds] == [
        settings.PICK_PLAYER_COMMAND_DESCRIPTION,
        settings.SHOW_LEADERBOARD_COMMAND_DESCRIPTION,
        settings.SHOW_PERSONAL_STATS_COMMAND_DESCRIPTION,
    ]


def test_admin_commands_contains_change_weight() -> None:
    settings = _test_settings()
    cmds = admin_commands(settings)
    assert cmds == [BotCommand(CHANGE_WEIGHT_COMMAND, CHANGE_WEIGHT_DESCRIPTION)]


async def test_sync_group_menu_approved_sets_scoped_game_commands() -> None:
    bot = AsyncMock()
    settings = _test_settings()
    await sync_group_menu(bot, chat_id=-100200, approved=True, settings=settings)
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == game_commands(settings)
    scope = kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == -100200
    bot.delete_my_commands.assert_not_awaited()


async def test_sync_group_menu_unapproved_deletes_scoped_commands() -> None:
    bot = AsyncMock()
    settings = _test_settings()
    await sync_group_menu(bot, chat_id=-100300, approved=False, settings=settings)
    bot.delete_my_commands.assert_awaited_once()
    kwargs = bot.delete_my_commands.await_args.kwargs
    scope = kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == -100300
    bot.set_my_commands.assert_not_awaited()


async def test_sync_group_menu_swallows_telegram_error() -> None:
    bot = AsyncMock()
    bot.set_my_commands.side_effect = TelegramError("nope")
    settings = _test_settings()
    await sync_group_menu(bot, chat_id=1, approved=True, settings=settings)


async def test_sync_user_admin_menu_admin_sets_scoped_admin_commands() -> None:
    bot = AsyncMock()
    settings = _test_settings()
    await sync_user_admin_menu(bot, user_id=777, is_admin=True, settings=settings)
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == admin_commands(settings)
    scope = kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == 777
    bot.delete_my_commands.assert_not_awaited()


async def test_sync_user_admin_menu_non_admin_deletes_scoped_commands() -> None:
    bot = AsyncMock()
    settings = _test_settings()
    await sync_user_admin_menu(bot, user_id=778, is_admin=False, settings=settings)
    bot.delete_my_commands.assert_awaited_once()
    kwargs = bot.delete_my_commands.await_args.kwargs
    scope = kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == 778
    bot.set_my_commands.assert_not_awaited()


async def test_sync_user_admin_menu_swallows_telegram_error() -> None:
    bot = AsyncMock()
    bot.delete_my_commands.side_effect = TelegramError("blocked")
    settings = _test_settings()
    await sync_user_admin_menu(bot, user_id=1, is_admin=False, settings=settings)


@requires_db
async def test_is_status_admin_false_when_no_matching_player(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    assert await is_status_admin(session, telegram_id=42, settings=settings) is False


@requires_db
async def test_is_status_admin_true_when_admin_in_approved_group(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    group = Group(status="member", telegram_id=6000, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="administrator",
            group_id=group.id,
            telegram_id=42,
            telegram_first_name="A",
            weight=3,
        )
    )
    await session.commit()

    assert await is_status_admin(session, telegram_id=42, settings=settings) is True


@requires_db
async def test_is_status_admin_false_when_group_not_approved(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    group = Group(status="member", telegram_id=6100, telegram_title="G", approved=False)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(
        Player(
            status="administrator",
            group_id=group.id,
            telegram_id=42,
            telegram_first_name="A",
            weight=3,
        )
    )
    await session.commit()

    assert await is_status_admin(session, telegram_id=42, settings=settings) is False


@requires_db
async def test_is_admin_user_short_circuits_on_hardcoded_id(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(admin_ids=[99])
    # No DB rows exist: the User-ID short-circuit must return True without any query.
    assert await is_admin_user(session, telegram_id=99, settings=settings) is True


@requires_db
async def test_is_admin_user_false_when_neither_tier_matches(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(admin_ids=[99])
    assert await is_admin_user(session, telegram_id=100, settings=settings) is False


@requires_db
async def test_reconcile_all_menus_syncs_approved_groups_and_all_admin_users(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(admin_ids=[500])

    approved = Group(status="member", telegram_id=7000, telegram_title="A", approved=True)
    unapproved = Group(status="member", telegram_id=7001, telegram_title="B", approved=False)
    session.add_all([approved, unapproved])
    await session.commit()
    assert approved.id is not None
    assert unapproved.id is not None
    session.add_all(
        [
            # Admin in an approved group: qualifies as a status-tier admin.
            Player(
                status="administrator",
                group_id=approved.id,
                telegram_id=601,
                telegram_first_name="P1",
                weight=3,
            ),
            # Non-admin in an approved group: must not receive an admin menu.
            Player(
                status="member",
                group_id=approved.id,
                telegram_id=602,
                telegram_first_name="P2",
                weight=3,
            ),
            # Admin in an unapproved group: does not count toward the admin tier.
            Player(
                status="administrator",
                group_id=unapproved.id,
                telegram_id=603,
                telegram_first_name="P3",
                weight=3,
            ),
        ]
    )
    await session.commit()

    bot = AsyncMock()
    await reconcile_all_menus(bot, settings)

    # Only the approved group must receive the game-command menu.
    group_set_calls = [
        call for call in bot.set_my_commands.await_args_list if call.kwargs["commands"] == game_commands(settings)
    ]
    assert len(group_set_calls) == 1
    assert group_set_calls[0].kwargs["scope"].chat_id == 7000

    # Both tiers must receive the admin menu: the User-ID admin (500) and the status-tier admin (601).
    admin_set_calls = [
        call for call in bot.set_my_commands.await_args_list if call.kwargs["commands"] == admin_commands(settings)
    ]
    scoped_user_ids = {call.kwargs["scope"].chat_id for call in admin_set_calls}
    assert scoped_user_ids == {500, 601}

    # The non-admin (602) and the admin in the unapproved group (603) must be excluded.
    assert 602 not in scoped_user_ids
    assert 603 not in scoped_user_ids


@requires_db
async def test_reconcile_all_menus_with_no_data_is_noop(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    bot = AsyncMock()
    settings = _test_settings()
    await reconcile_all_menus(bot, settings)
    bot.set_my_commands.assert_not_awaited()
    bot.delete_my_commands.assert_not_awaited()
