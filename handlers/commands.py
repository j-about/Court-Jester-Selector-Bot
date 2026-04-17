"""Read-only command handlers for group chats.

Exposes the leaderboard and personal-stats commands. Both gate on the
group's approval state and on a minimum-player threshold before querying
draw history.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Settings
from database import get_session
from queries import get_leaderboard, get_player, get_player_stats
from utils.gates import check_approval_gate, check_min_players_gate
from utils.messages import safe_format
from utils.players import player_display_name, user_display_name

logger = logging.getLogger(__name__)

__all__ = [
    "on_court_leaderboard",
    "on_my_jester_stats",
    "register_command_handlers",
]


async def on_court_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the top ten most-drawn players using competition ranking."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    settings: Settings = context.application.bot_data["settings"]

    try:
        async with get_session() as session:
            group = await check_approval_gate(update, session, settings)
            if group is None or group.id is None:
                return
            if not await check_min_players_gate(update, session, group, settings):
                return

            leaderboard = await get_leaderboard(session, group.id, limit=10)
    except Exception:
        logger.exception("court_leaderboard failed", extra={"chat_id": chat.id})
        return

    if not leaderboard:
        await message.reply_text(settings.LEADERBOARD_NOT_ENOUGH_PICKED_PLAYERS_MESSAGE)
        return

    # Competition ranking: equal draw counts share a rank and the next rank
    # jumps to its positional index (the 1, 2, 2, 4 pattern).
    lines = [settings.LEADERBOARD_INTRO_MESSAGE]
    rank = 0
    prev_count: int | None = None
    for i, (player, draw_count) in enumerate(leaderboard):
        if draw_count != prev_count:
            rank = i + 1
        prev_count = draw_count
        lines.append(
            safe_format(
                settings.LEADERBOARD_RANK_MESSAGE,
                {
                    "rank": rank,
                    "username": player_display_name(player),
                    "draw_count": draw_count,
                },
                "LEADERBOARD_RANK_MESSAGE",
            )
        )
    lines.append(settings.LEADERBOARD_OUTRO_MESSAGE)

    await message.reply_text("\n".join(lines))


async def on_my_jester_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the invoking user's draw count and competition rank."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    fallback_username = user_display_name(user)

    try:
        async with get_session() as session:
            group = await check_approval_gate(update, session, settings)
            if group is None or group.id is None:
                return
            if not await check_min_players_gate(update, session, group, settings):
                return

            player = await get_player(session, group.id, user.id)
            if player is None or player.id is None:
                await message.reply_text(
                    safe_format(
                        settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE,
                        {"username": fallback_username},
                        "PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE",
                    )
                )
                return

            display_name = player_display_name(player)
            draw_count, rank = await get_player_stats(session, group.id, player.id)
    except Exception:
        logger.exception("my_jester_stats failed", extra={"chat_id": chat.id})
        return

    if draw_count == 0:
        await message.reply_text(
            safe_format(
                settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE,
                {"username": display_name},
                "PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE",
            )
        )
        return

    await message.reply_text(
        safe_format(
            settings.PERSONAL_STATS_MESSAGE,
            {
                "username": display_name,
                "draw_count": draw_count,
                "rank": rank,
            },
            "PERSONAL_STATS_MESSAGE",
        )
    )


def register_command_handlers(app: Application) -> None:
    """Register the leaderboard and personal-stats command handlers."""
    settings: Settings = app.bot_data["settings"]
    app.add_handler(CommandHandler(settings.SHOW_LEADERBOARD_COMMAND, on_court_leaderboard))
    app.add_handler(CommandHandler(settings.SHOW_PERSONAL_STATS_COMMAND, on_my_jester_stats))
