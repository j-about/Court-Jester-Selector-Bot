"""Race-condition tests for concurrent draws and player registration.

Exercises the ``IntegrityError``-recovery branch in the draw handler and
the unique-constraint contracts on the ``draw`` and ``player`` tables by
simulating multiple concurrent ``/crown_the_jester`` invocations and
concurrent inserts of the same ``(group, telegram_id)`` pair.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import database
from config import Settings
from handlers.draw import on_crown_the_jester
from models import Draw, Group, Player
from tests.conftest import requires_db


def _settings(min_players: int = 2) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    os.environ["MIN_PLAYERS"] = str(min_players)
    return Settings()  # ty: ignore[missing-argument]


def _fake_update(chat_id: int) -> tuple[MagicMock, AsyncMock]:
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update.effective_chat = chat
    update.effective_message = msg
    return update, msg.reply_text


def _fake_ctx(settings: Settings) -> MagicMock:
    ctx = MagicMock()
    app = MagicMock()
    app.bot_data = {"settings": settings}
    ctx.application = app
    return ctx


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


async def _seed_group_with_players(
    session: AsyncSession, telegram_id: int, n_players: int
) -> Group:
    group = Group(status="member", telegram_id=telegram_id, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    for i in range(n_players):
        session.add(
            Player(
                status="member",
                group_id=group.id,
                telegram_id=10_000 + i,
                telegram_first_name=f"P{i}",
                telegram_username=f"p{i}",
                weight=3,
            )
        )
    await session.commit()
    return group


@requires_db
async def test_concurrent_draws_produce_single_row_and_same_winner(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that two simultaneous draws produce a single row and identical announcements.

    The caller that loses the unique-constraint race must re-read the
    committed ``Draw`` row and announce the same winner as the caller
    that won it.
    """
    settings = _settings()
    group = await _seed_group_with_players(session, telegram_id=70_001, n_players=4)

    update_a, reply_a = _fake_update(chat_id=70_001)
    update_b, reply_b = _fake_update(chat_id=70_001)
    ctx = _fake_ctx(settings)

    await asyncio.gather(
        on_crown_the_jester(update_a, ctx),
        on_crown_the_jester(update_b, ctx),
    )

    rows = (
        await session.execute(
            select(Draw).where(Draw.group_id == group.id, Draw.draw_date == date.today())
        )
    ).scalars().all()
    assert len(rows) == 1, f"expected exactly one Draw row, got {len(rows)}"

    reply_a.assert_awaited_once()
    reply_b.assert_awaited_once()
    msg_a = reply_a.await_args.args[0]
    msg_b = reply_b.await_args.args[0]
    assert msg_a == msg_b, "both concurrent callers should see the same winner"


@requires_db
async def test_ten_concurrent_draws_all_resolve_to_one_row(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that ten concurrent draws still resolve to a single ``Draw`` row and a single announcement."""
    settings = _settings()
    group = await _seed_group_with_players(session, telegram_id=70_002, n_players=4)

    coros = []
    replies = []
    for _ in range(10):
        update, reply = _fake_update(chat_id=70_002)
        coros.append(on_crown_the_jester(update, _fake_ctx(settings)))
        replies.append(reply)

    await asyncio.gather(*coros)

    rows = (
        await session.execute(
            select(Draw).where(Draw.group_id == group.id, Draw.draw_date == date.today())
        )
    ).scalars().all()
    assert len(rows) == 1

    msgs = {r.await_args.args[0] for r in replies if r.await_args is not None}
    assert len(msgs) == 1, f"expected one announcement, got {len(msgs)} distinct"


@requires_db
async def test_unique_player_constraint_rejects_duplicate_registration(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that ``uix_group_telegram_id`` rejects duplicate ``(group, telegram_id)`` inserts.

    Two independent sessions attempt to insert the same player
    concurrently; exactly one must survive. The ``intercept_message``
    handler relies on this constraint for its upsert correctness.
    """
    from sqlalchemy.exc import IntegrityError

    group = Group(status="member", telegram_id=70_003, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    group_id: int = group.id

    async def _insert_player() -> bool:
        async with db_sessionmaker() as s:
            s.add(
                Player(
                    status="member",
                    group_id=group_id,
                    telegram_id=42,
                    telegram_first_name="Same",
                    weight=3,
                )
            )
            try:
                await s.commit()
                return True
            except IntegrityError:
                await s.rollback()
                return False

    results = await asyncio.gather(_insert_player(), _insert_player())
    assert sum(results) == 1, f"expected exactly one successful insert, got {results}"

    rows = (
        await session.execute(
            select(Player).where(Player.group_id == group_id, Player.telegram_id == 42)
        )
    ).scalars().all()
    assert len(rows) == 1
