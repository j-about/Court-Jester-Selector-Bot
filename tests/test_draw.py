"""Tests for the daily weighted-draw command handler."""
from __future__ import annotations

import os
import random
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from telegram.ext import Application, CommandHandler

import database
from config import Settings
from handlers.draw import (
    on_crown_the_jester,
    register_draw_handlers,
)
from models import Draw, Group, Player
from tests.conftest import requires_db
from utils import time as utils_time


def _test_settings(min_players: int | None = None) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    if min_players is not None:
        os.environ["MIN_PLAYERS"] = str(min_players)
    else:
        os.environ.pop("MIN_PLAYERS", None)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _fake_context(*, settings: Settings | None = None) -> MagicMock:
    ctx = MagicMock()
    app = MagicMock()
    app.bot_data = {"settings": settings or _test_settings()}
    ctx.application = app
    return ctx


def _fake_command_update(*, chat_id: int) -> tuple[MagicMock, AsyncMock]:
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_chat = chat
    update.effective_message = message
    return update, message.reply_text


def _last_reply_text(reply: AsyncMock) -> str:
    call = reply.await_args
    assert call is not None
    return call.args[0]


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


def _make_player(
    *,
    group_id: int,
    telegram_id: int,
    username: str | None = None,
    first_name: str = "Player",
    weight: int = 3,
) -> Player:
    return Player(
        status="member",
        group_id=group_id,
        telegram_id=telegram_id,
        telegram_first_name=first_name,
        telegram_username=username,
        weight=weight,
    )


def test_register_draw_handlers_adds_one_command_handler() -> None:
    app = MagicMock(spec=Application)
    app.bot_data = {"settings": _test_settings()}
    register_draw_handlers(app)
    assert app.add_handler.call_count == 1
    handler = app.add_handler.call_args.args[0]
    assert isinstance(handler, CommandHandler)
    assert "crown_the_jester" in handler.commands


@requires_db
async def test_unapproved_group_rejected(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="member", telegram_id=9001, telegram_title="G", approved=False)
    session.add(group)
    await session.commit()

    settings = _test_settings()
    update, reply = _fake_command_update(chat_id=9001)
    ctx = _fake_context(settings=settings)

    await on_crown_the_jester(update, ctx)

    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)
    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert draws == []


@requires_db
async def test_unknown_group_rejected_as_non_approved(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    update, reply = _fake_command_update(chat_id=404404)
    ctx = _fake_context(settings=settings)

    await on_crown_the_jester(update, ctx)

    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)


@requires_db
async def test_insufficient_players_rejected(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=5)
    group = Group(status="member", telegram_id=9100, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    for i in range(3):
        session.add(_make_player(group_id=group.id, telegram_id=1000 + i))
    await session.commit()

    update, reply = _fake_command_update(chat_id=9100)
    ctx = _fake_context(settings=settings)

    await on_crown_the_jester(update, ctx)

    reply.assert_awaited_once_with(settings.NOT_ENOUGH_PLAYERS_MESSAGE.format_map({"min_players": 5}))
    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert draws == []


@requires_db
async def test_first_call_creates_draw_and_announces(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    group = Group(status="member", telegram_id=9200, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(_make_player(group_id=group.id, telegram_id=7001, username="only", weight=3))
    session.add(_make_player(group_id=group.id, telegram_id=7002, weight=3))
    await session.commit()

    update, reply = _fake_command_update(chat_id=9200)
    ctx = _fake_context(settings=settings)

    await on_crown_the_jester(update, ctx)

    reply.assert_awaited_once()
    sent = _last_reply_text(reply)
    # The configured success template begins with 🎪; its presence confirms the template was used.
    assert "🎪" in sent

    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert len(draws) == 1


@requires_db
async def test_second_call_same_day_returns_existing_winner(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    group = Group(status="member", telegram_id=9300, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    p1 = _make_player(group_id=group.id, telegram_id=8001, username="p1", weight=3)
    p2 = _make_player(group_id=group.id, telegram_id=8002, username="p2", weight=3)
    session.add(p1)
    session.add(p2)
    await session.commit()

    update1, reply1 = _fake_command_update(chat_id=9300)
    ctx = _fake_context(settings=settings)
    await on_crown_the_jester(update1, ctx)
    first_msg = _last_reply_text(reply1)

    update2, reply2 = _fake_command_update(chat_id=9300)
    await on_crown_the_jester(update2, ctx)
    second_msg = _last_reply_text(reply2)

    assert first_msg == second_msg
    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert len(draws) == 1


@requires_db
async def test_weighted_selection_favors_higher_weights(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that, across many independent draws, the weight-5 player wins far more often than the weight-1 player."""
    settings = _test_settings(min_players=2)
    random.seed(1234)

    heavy_wins = 0
    light_wins = 0
    iterations = 50
    for i in range(iterations):
        group = Group(
            status="member",
            telegram_id=10_000 + i,
            telegram_title=f"G{i}",
            approved=True,
        )
        session.add(group)
        await session.commit()
        assert group.id is not None
        session.add(
            _make_player(
                group_id=group.id,
                telegram_id=20_000 + i,
                username="heavy",
                weight=5,
            )
        )
        session.add(
            _make_player(
                group_id=group.id,
                telegram_id=30_000 + i,
                username="light",
                weight=1,
            )
        )
        await session.commit()

        update, reply = _fake_command_update(chat_id=10_000 + i)
        ctx = _fake_context(settings=settings)
        await on_crown_the_jester(update, ctx)
        sent = _last_reply_text(reply)
        if "@heavy" in sent:
            heavy_wins += 1
        elif "@light" in sent:
            light_wins += 1

    assert heavy_wins + light_wins == iterations
    # A 5:1 weight ratio has an expected heavy-win share near 83%; the ``> 2x`` threshold is a generous bound.
    assert heavy_wins > light_wins * 2, f"expected heavy bias; got heavy={heavy_wins}, light={light_wins}"


@requires_db
async def test_preexisting_draw_is_returned_not_overwritten(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that an existing ``Draw`` row for today is returned unchanged on a subsequent call."""
    from datetime import date as _date

    settings = _test_settings(min_players=2)
    group = Group(status="member", telegram_id=9400, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    winner = _make_player(
        group_id=group.id,
        telegram_id=9501,
        username="winner",
        weight=3,
    )
    other = _make_player(group_id=group.id, telegram_id=9502, username="other", weight=3)
    session.add(winner)
    session.add(other)
    await session.commit()
    assert winner.id is not None
    session.add(Draw(draw_date=_date.today(), group_id=group.id, player_id=winner.id))
    await session.commit()

    update, reply = _fake_command_update(chat_id=9400)
    ctx = _fake_context(settings=settings)
    await on_crown_the_jester(update, ctx)

    reply.assert_awaited_once()
    sent = _last_reply_text(reply)
    assert "@winner" in sent

    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert len(draws) == 1


class _FrozenDatetime:
    """Substitute for ``datetime`` whose ``.now(tz)`` returns a fixed instant."""

    def __init__(self, fixed_utc: datetime) -> None:
        assert fixed_utc.tzinfo is not None
        self._fixed_utc = fixed_utc

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        if tz is None:
            return self._fixed_utc
        return self._fixed_utc.astimezone(tz)


@requires_db
async def test_paris_midnight_boundary_creates_two_draws(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls straddling Paris midnight must yield two distinct ``draw_date``s.

    Verifies the scenario motivating the ``DRAW_TIMEZONE`` setting: when
    the deployer sets Europe/Paris, a draw at 23:59 Paris and one at 00:00
    Paris are on different civil days even though they sit inside the
    same UTC hour.
    """
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    os.environ["MIN_PLAYERS"] = "2"
    settings = Settings(_env_file=None, DRAW_TIMEZONE="Europe/Paris")  # type: ignore[call-arg]

    group = Group(status="member", telegram_id=9700, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add(_make_player(group_id=group.id, telegram_id=11001, username="a", weight=3))
    session.add(_make_player(group_id=group.id, telegram_id=11002, username="b", weight=3))
    await session.commit()

    ctx = _fake_context(settings=settings)

    # 21:59:00 UTC == 23:59:00 Paris (DST, UTC+2) -> civil date 2026-04-18.
    monkeypatch.setattr(
        utils_time,
        "datetime",
        _FrozenDatetime(datetime(2026, 4, 18, 21, 59, 0, tzinfo=ZoneInfo("UTC"))),
    )
    update1, _reply1 = _fake_command_update(chat_id=9700)
    await on_crown_the_jester(update1, ctx)

    # 22:00:30 UTC == 00:00:30 Paris the next day -> civil date 2026-04-19.
    monkeypatch.setattr(
        utils_time,
        "datetime",
        _FrozenDatetime(datetime(2026, 4, 18, 22, 0, 30, tzinfo=ZoneInfo("UTC"))),
    )
    update2, _reply2 = _fake_command_update(chat_id=9700)
    await on_crown_the_jester(update2, ctx)

    draws = (
        await session.execute(
            select(Draw).where(Draw.group_id == group.id).order_by(Draw.draw_date)
        )
    ).scalars().all()
    assert [d.draw_date.isoformat() for d in draws] == ["2026-04-18", "2026-04-19"]
