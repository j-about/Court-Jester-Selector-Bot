"""Group-lifecycle handlers: membership changes and approval callbacks.

Tracks the bot's own chat-member transitions to record or retire a
``Group`` record, sends an approval prompt to each configured admin when a
new group is detected, and processes the resulting Approve / Reject button
presses.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
)

from config import Settings
from database import get_session
from models import Group
from observability import log_bot_membership_change, log_group_approval
from queries import get_admin_player_ids_for_group, get_group_by_telegram_id
from utils.command_menu import sync_group_menu, sync_user_admin_menu
from utils.players import user_display_name

logger = logging.getLogger(__name__)

__all__ = [
    "notify_admins_new_group",
    "on_approve_callback",
    "on_my_chat_member",
    "on_reject_callback",
    "register_lifecycle_handlers",
]

_ACTIVE_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})
_INACTIVE_STATUSES = frozenset({"left", "kicked"})


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist membership transitions of the bot into or out of a group.

    On addition, an unseen group is inserted as pending approval and
    approval prompts are sent to User-ID admins; a returning group has its
    title and status refreshed. On removal, the group's status is updated
    and its command menu is cleared.
    """
    my = update.my_chat_member
    if my is None:
        return
    chat = my.chat
    old_status = my.old_chat_member.status
    new_status = my.new_chat_member.status

    added = old_status in _INACTIVE_STATUSES and new_status in _ACTIVE_STATUSES
    removed = old_status in _ACTIVE_STATUSES and new_status in _INACTIVE_STATUSES
    if not added and not removed:
        return

    settings: Settings = context.application.bot_data["settings"]

    try:
        async with get_session() as session:
            group = await get_group_by_telegram_id(session, chat.id)

            if added:
                title = chat.title or ""
                if group is None:
                    group = Group(
                        status=new_status,
                        telegram_id=chat.id,
                        telegram_title=title,
                        approved=False,
                    )
                    session.add(group)
                    await session.flush()
                    notify = True
                else:
                    group.status = new_status
                    group.telegram_title = title
                    notify = not group.approved

                if notify and not settings.TG_BOT_ADMIN_RIGHTS_USER_IDS:
                    group.approved = True
                    notify = False
                    logger.info(
                        "lifecycle.group.auto_approved",
                        extra={"group_id": chat.id, "reason": "empty_admin_roster"},
                    )

                group_pk = group.id
                group_approved = group.approved
                group_telegram_id = group.telegram_id
                group_title = group.telegram_title
            else:
                if group is not None:
                    group.status = new_status
                notify = False
                group_pk = None
                group_approved = False
                group_telegram_id = chat.id
                group_title = chat.title or ""

            log_bot_membership_change(group_id=chat.id, added=added)

        if added and group_approved:
            await sync_group_menu(
                context.bot,
                chat_id=group_telegram_id,
                approved=True,
                settings=settings,
            )
        elif removed:
            await sync_group_menu(
                context.bot,
                chat_id=chat.id,
                approved=False,
                settings=settings,
            )

        if notify and group_pk is not None and not group_approved:
            await notify_admins_new_group(context.bot, group_pk, group_telegram_id, group_title, settings)
    except Exception:
        logger.exception(
            "lifecycle.chat_member_update failed",
            extra={
                "chat_id": chat.id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )


async def notify_admins_new_group(
    bot: Any,
    group_pk: int,
    group_telegram_id: int,
    title: str,
    settings: Settings,
) -> None:
    """Send an Approve/Reject prompt to every configured User-ID admin.

    Each admin receives a private message with inline buttons tied to the
    given ``group_pk``. The ``(chat_id, message_id)`` pairs of every
    successful delivery are stored on ``Group.approval_messages`` so the
    original prompts can later be edited to reflect the final decision.
    """
    admin_ids = settings.TG_BOT_ADMIN_RIGHTS_USER_IDS
    if not admin_ids:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{group_pk}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{group_pk}"),
            ]
        ]
    )
    text = f"New group request:\nTitle: {title}\nTelegram ID: {group_telegram_id}"

    async def _send(admin_id: int) -> tuple[int, int] | None:
        try:
            msg = await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except TelegramError:
            logger.exception(
                "lifecycle.admin_notify failed",
                extra={
                    "admin_id": admin_id,
                    "group_id": group_telegram_id,
                },
            )
            return None
        return msg.chat_id, msg.message_id

    results = await asyncio.gather(*(_send(admin_id) for admin_id in admin_ids))
    messages: dict[str, dict[str, int]] = {
        str(admin_id): {"chat_id": res[0], "message_id": res[1]}
        for admin_id, res in zip(admin_ids, results, strict=True)
        if res is not None
    }

    if not messages:
        return

    async with get_session() as session:
        group = await session.get(Group, group_pk)
        if group is None:
            return
        existing = dict(group.approval_messages or {})
        existing["messages"] = {**(existing.get("messages") or {}), **messages}
        group.approval_messages = existing


async def _handle_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    action: str,
) -> None:
    """Apply an approve or reject decision to the group named in the callback.

    Authorizes the caller against the User-ID admin set, updates the
    ``Group`` row, syncs the group's command menu, propagates the decision
    to every admin's original prompt message, and leaves the chat on
    rejection.
    """
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    admin_id = query.from_user.id
    if admin_id not in settings.TG_BOT_ADMIN_RIGHTS_USER_IDS:
        await query.answer()
        return

    try:
        group_pk = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    approved = action == "approve"
    await query.answer()

    try:
        async with get_session() as session:
            group = await session.get(Group, group_pk)
            if group is None:
                return

            group_telegram_id = group.telegram_id
            title = group.telegram_title

            if approved:
                group.approved = True
            else:
                group.status = "rejected"

            meta = dict(group.approval_messages or {})
            meta["decision"] = {
                "admin_telegram_id": admin_id,
                "action": "approved" if approved else "rejected",
                "decided_at": datetime.now(UTC).isoformat(),
            }
            messages = dict(meta.get("messages") or {})
            group.approval_messages = meta

            if approved:
                status_admin_ids = await get_admin_player_ids_for_group(
                    session,
                    group_id=group_pk,
                    allowed_statuses=settings.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS,
                )
            else:
                status_admin_ids = []

        await sync_group_menu(
            context.bot,
            chat_id=group_telegram_id,
            approved=approved,
            settings=settings,
        )
        if approved and status_admin_ids:
            await asyncio.gather(
                *(
                    sync_user_admin_menu(context.bot, user_id=uid, is_admin=True, settings=settings)
                    for uid in status_admin_ids
                )
            )

        verdict = "✅ Approved" if approved else "❌ Rejected"
        edited_text = f"{verdict} — {title}"
        try:
            await query.edit_message_text(text=edited_text)
        except TelegramError:
            logger.exception(
                "lifecycle.edit_acting_admin failed",
                extra={
                    "admin_id": admin_id,
                    "group_id": group_telegram_id,
                },
            )

        acting_key = str(query.message.chat.id) if query.message is not None else None
        other_text = f"{verdict} by admin {user_display_name(query.from_user)} — {title}"

        async def _edit_other(admin_key: str, chat_id: int, message_id: int) -> None:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=other_text,
                )
            except TelegramError:
                logger.exception(
                    "lifecycle.edit_other_admin failed",
                    extra={
                        "admin_id": admin_key,
                        "group_id": group_telegram_id,
                    },
                )

        edits = []
        for admin_key, record in messages.items():
            if admin_key == str(admin_id):
                continue
            chat_id = record.get("chat_id")
            message_id = record.get("message_id")
            if chat_id is None or message_id is None:
                continue
            if acting_key is not None and str(chat_id) == acting_key:
                continue
            edits.append(_edit_other(admin_key, chat_id, message_id))
        if edits:
            await asyncio.gather(*edits)

        if not approved:
            try:
                await context.bot.leave_chat(chat_id=group_telegram_id)
            except TelegramError:
                logger.exception(
                    "lifecycle.leave_chat failed",
                    extra={"group_id": group_telegram_id},
                )

        log_group_approval(admin_id=admin_id, group_id=group_telegram_id, approved=approved)
    except Exception:
        logger.exception(
            "lifecycle.decision_callback failed",
            extra={
                "admin_id": admin_id,
                "group_pk": group_pk,
                "action": action,
            },
        )


async def on_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Approve button on an admin approval prompt."""
    await _handle_decision(update, context, action="approve")


async def on_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Reject button on an admin approval prompt."""
    await _handle_decision(update, context, action="reject")


def register_lifecycle_handlers(app: Application) -> None:
    """Register the membership-change and approval-callback handlers."""
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_approve_callback, pattern=r"^approve:\d+$"))
    app.add_handler(CallbackQueryHandler(on_reject_callback, pattern=r"^reject:\d+$"))
