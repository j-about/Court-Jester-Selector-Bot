"""Daily weighted-random player draw.

Picks a "jester" for the current calendar day in the invoking group. A
``Draw`` row per group and date enforces idempotency, and a unique
constraint on ``(group_id, draw_date)`` lets concurrent callers recover the
same winner through an ``IntegrityError`` branch.
"""
from __future__ import annotations

import logging
import random
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import Settings
from database import get_session
from models import Draw, Player
from observability import log_draw_execution
from utils.gates import check_approval_gate, check_min_players_gate
from utils.messages import safe_format
from utils.players import player_display_name
from utils.time import current_draw_date

logger = logging.getLogger(__name__)

__all__ = ["on_crown_the_jester", "register_draw_handlers"]


async def _get_todays_draw(session: AsyncSession, group_id: int, today: date) -> Draw | None:
    result = await session.execute(
        select(Draw).where(Draw.group_id == group_id, Draw.draw_date == today)
    )
    return result.scalars().first()


async def on_crown_the_jester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with today's winner, selecting one if none has been drawn yet.

    If a ``Draw`` row already exists for this group and the current date
    the stored winner is returned. Otherwise a player is chosen with
    ``random.choices`` weighted by ``Player.weight`` and persisted; a
    concurrent insert that violates the unique constraint is recovered by
    re-reading the winning row.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    today = current_draw_date(settings)

    try:
        async with get_session() as session:
            group = await check_approval_gate(update, session, settings)
            if group is None or group.id is None:
                return
            group_pk: int = group.id

            if not await check_min_players_gate(update, session, group, settings):
                return

            players = (await session.execute(select(Player).where(Player.group_id == group_pk))).scalars().all()

            existing = await _get_todays_draw(session, group_pk, today)
            if existing is not None:
                winner = await session.get(Player, existing.player_id)
            else:
                winner = random.choices(
                    list(players),
                    weights=[p.weight for p in players],
                    k=1,
                )[0]
                winner_id = winner.id
                assert winner_id is not None
                try:
                    session.add(
                        Draw(
                            draw_date=today,
                            group_id=group_pk,
                            player_id=winner_id,
                        )
                    )
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    existing = await _get_todays_draw(session, group_pk, today)
                    if existing is None:
                        logger.error("draw race recovery failed", extra={"chat_id": chat.id})
                        return
                    winner = await session.get(Player, existing.player_id)

            if winner is None:
                logger.error("crown_the_jester missing winner", extra={"chat_id": chat.id})
                return

            log_draw_execution(chat.id, winner.telegram_id, today, settings.DRAW_TIMEZONE)
            await message.reply_text(
                safe_format(
                    settings.PICK_PLAYER_PICKED_PLAYER_MESSAGE,
                    {"username": player_display_name(winner)},
                    "PICK_PLAYER_PICKED_PLAYER_MESSAGE",
                )
            )
    except Exception:
        logger.exception("crown_the_jester failed", extra={"chat_id": chat.id})


def register_draw_handlers(app: Application) -> None:
    """Register the daily-draw command handler on the Application."""
    settings: Settings = app.bot_data["settings"]
    app.add_handler(CommandHandler(settings.PICK_PLAYER_COMMAND, on_crown_the_jester))
