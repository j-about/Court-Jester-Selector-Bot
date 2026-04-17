"""Silent player registration from group messages.

Listens to every non-command group message and upserts the sending user as
a ``Player`` of the corresponding ``Group``. When an approved group sees a
user cross the admin-status threshold, the user's private-chat command
menu is refreshed.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import Settings
from database import get_session
from models import Player
from queries import get_group_by_telegram_id, get_player
from utils.command_menu import is_admin_user, sync_user_admin_menu

logger = logging.getLogger(__name__)

__all__ = ["intercept_message", "register_registration_handlers"]


async def intercept_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upsert the author of a group message as a ``Player`` of the group.

    New players are inserted with the default weight and the sender's
    current Telegram chat-member status; existing players have their
    profile and status refreshed. When the player's admin-status
    eligibility flips inside an approved group, their private-chat command
    menu is re-synced.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None or user.is_bot:
        return

    try:
        chat_member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        logger.exception("get_chat_member failed", extra={"chat_id": chat.id, "user_id": user.id})
        return
    member_status = chat_member.status

    settings: Settings = context.application.bot_data["settings"]

    allowed_statuses = settings.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS
    try:
        async with get_session() as session:
            group = await get_group_by_telegram_id(session, chat.id)
            if group is None or group.id is None or not group.approved:
                return
            group_pk: int = group.id
            group_approved = group.approved

            player = await get_player(session, group_pk, user.id)
            old_status = player.status if player is not None else None

            first_name = user.first_name or ""
            if player is None:
                session.add(
                    Player(
                        group_id=group_pk,
                        telegram_id=user.id,
                        telegram_first_name=first_name,
                        telegram_last_name=user.last_name,
                        telegram_username=user.username,
                        weight=settings.DEFAULT_WEIGHT,
                        status=member_status,
                    )
                )
                logger.info(
                    "player_registered",
                    extra={
                        "group_telegram_id": chat.id,
                        "player_telegram_id": user.id,
                        "status": member_status,
                    },
                )
            else:
                player.telegram_first_name = first_name
                player.telegram_last_name = user.last_name
                player.telegram_username = user.username
                player.status = member_status
                logger.debug(
                    "player_updated",
                    extra={
                        "group_telegram_id": chat.id,
                        "player_telegram_id": user.id,
                        "status": member_status,
                    },
                )

            was_admin_here = old_status in allowed_statuses if old_status is not None else False
            now_admin_here = member_status in allowed_statuses
            threshold_crossed = group_approved and was_admin_here != now_admin_here
            if threshold_crossed:
                is_admin = await is_admin_user(session, telegram_id=user.id, settings=settings)
            else:
                is_admin = None

        if is_admin is not None:
            await sync_user_admin_menu(context.bot, user_id=user.id, is_admin=is_admin, settings=settings)
    except Exception:
        logger.exception(
            "player upsert failed",
            extra={
                "chat_id": chat.id,
                "user_id": user.id,
            },
        )


def register_registration_handlers(app: Application) -> None:
    """Register the silent-registration message handler on the Application."""
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            intercept_message,
        )
    )
