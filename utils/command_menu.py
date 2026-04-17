"""Per-scope Telegram command menu management.

Builds the game-command and admin-command menus from ``Settings``, syncs
them into per-chat and per-user scopes through the Bot API, and reconciles
the full set of scoped menus against the current database state on
startup.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

from database import get_session
from queries import (
    get_admin_groups_by_status_page,
    get_admin_player_ids_for_group,
    get_approved_groups,
)

if TYPE_CHECKING:
    from telegram import Bot

    from config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "CHANGE_WEIGHT_COMMAND",
    "CHANGE_WEIGHT_DESCRIPTION",
    "admin_commands",
    "game_commands",
    "is_admin_user",
    "is_status_admin",
    "reconcile_all_menus",
    "sync_group_menu",
    "sync_user_admin_menu",
]


CHANGE_WEIGHT_COMMAND = "change_weight"
CHANGE_WEIGHT_DESCRIPTION = "Change a player's draw weight."


def game_commands(settings: Settings) -> list[BotCommand]:
    """Return the in-group game command menu (draw, leaderboard, personal stats)."""
    return [
        BotCommand(settings.PICK_PLAYER_COMMAND, settings.PICK_PLAYER_COMMAND_DESCRIPTION),
        BotCommand(settings.SHOW_LEADERBOARD_COMMAND, settings.SHOW_LEADERBOARD_COMMAND_DESCRIPTION),
        BotCommand(settings.SHOW_PERSONAL_STATS_COMMAND, settings.SHOW_PERSONAL_STATS_COMMAND_DESCRIPTION),
    ]


def admin_commands(_settings: Settings) -> list[BotCommand]:
    """Return the private-chat admin command menu (weight change wizard)."""
    return [BotCommand(CHANGE_WEIGHT_COMMAND, CHANGE_WEIGHT_DESCRIPTION)]


async def sync_group_menu(
    bot: Bot,
    *,
    chat_id: int,
    approved: bool,
    settings: Settings,
) -> None:
    """Set the group's game-command menu when ``approved`` is true, or clear it otherwise."""
    scope = BotCommandScopeChat(chat_id=chat_id)
    try:
        if approved:
            await bot.set_my_commands(commands=game_commands(settings), scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramError:
        logger.exception(
            "command_menu.sync_group_menu failed",
            extra={
                "chat_id": chat_id,
                "approved": approved,
            },
        )


async def sync_user_admin_menu(
    bot: Bot,
    *,
    user_id: int,
    is_admin: bool,
    settings: Settings,
) -> None:
    """Set the admin-command menu in ``user_id``'s private chat when ``is_admin`` is true, or clear it otherwise."""
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        if is_admin:
            await bot.set_my_commands(commands=admin_commands(settings), scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramError:
        logger.exception(
            "command_menu.sync_user_admin_menu failed",
            extra={
                "user_id": user_id,
                "is_admin": is_admin,
            },
        )


async def is_status_admin(session, *, telegram_id: int, settings: Settings) -> bool:
    """Return ``True`` iff ``telegram_id`` holds an admin status in at least one approved group."""
    _, total = await get_admin_groups_by_status_page(
        session,
        telegram_id=telegram_id,
        allowed_statuses=settings.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS,
        page=0,
        per_page=1,
    )
    return total > 0


async def is_admin_user(session, *, telegram_id: int, settings: Settings) -> bool:
    """Return ``True`` if the user qualifies as admin via either the User-ID tier or the status tier."""
    if telegram_id in settings.TG_BOT_ADMIN_RIGHTS_USER_IDS:
        return True
    return await is_status_admin(session, telegram_id=telegram_id, settings=settings)


async def reconcile_all_menus(bot: Bot, settings: Settings) -> None:
    """Rebuild every scoped command menu from the current database state.

    Applies the game menu to each approved group's chat scope and the
    admin menu to each User-ID admin and each status-tier admin's private
    chat scope. Intended to be invoked during bot startup.
    """
    async with get_session() as session:
        approved_groups = await get_approved_groups(session)
        status_admin_ids: set[int] = set()
        for group in approved_groups:
            if group.id is None:
                continue
            ids = await get_admin_player_ids_for_group(
                session,
                group_id=group.id,
                allowed_statuses=settings.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS,
            )
            status_admin_ids.update(ids)

    admin_user_ids = set(settings.TG_BOT_ADMIN_RIGHTS_USER_IDS) | status_admin_ids

    group_tasks = [
        sync_group_menu(bot, chat_id=group.telegram_id, approved=True, settings=settings) for group in approved_groups
    ]
    admin_tasks = [sync_user_admin_menu(bot, user_id=uid, is_admin=True, settings=settings) for uid in admin_user_ids]
    if group_tasks or admin_tasks:
        await asyncio.gather(*group_tasks, *admin_tasks)
