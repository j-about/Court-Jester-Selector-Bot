"""Precondition gates shared by group-chat command handlers."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update

from config import Settings
from models import Group
from queries import count_players, get_group_by_telegram_id
from utils.messages import safe_format

__all__ = ["check_approval_gate", "check_min_players_gate"]


async def check_approval_gate(update: Update, session: AsyncSession, settings: Settings) -> Group | None:
    """Return the approved ``Group`` for the update's chat, replying with a notice otherwise.

    Returns ``None`` if the message or chat is missing, if no ``Group``
    row exists for the chat, or if the group is not yet approved; in the
    latter case the non-approved-group message is sent to the chat.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return None

    group = await get_group_by_telegram_id(session, chat.id)
    if group is None or not group.approved or group.id is None:
        await message.reply_text(settings.NON_APPROVED_GROUP_MESSAGE)
        return None
    return group


async def check_min_players_gate(update: Update, session: AsyncSession, group: Group, settings: Settings) -> bool:
    """Return ``True`` iff the group has at least ``Settings.MIN_PLAYERS`` players.

    When the threshold is not met, replies with the insufficient-players
    message and returns ``False``.
    """
    message = update.effective_message
    if message is None or group.id is None:
        return False

    count = await count_players(session, group.id)
    if count < settings.MIN_PLAYERS:
        await message.reply_text(
            safe_format(
                settings.NOT_ENOUGH_PLAYERS_MESSAGE,
                {"min_players": settings.MIN_PLAYERS},
                "NOT_ENOUGH_PLAYERS_MESSAGE",
            )
        )
        return False
    return True
