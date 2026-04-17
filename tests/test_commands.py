"""Tests for the ``/court_leaderboard`` and ``/my_jester_stats`` command handlers."""
from __future__ import annotations

import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.ext import Application, CommandHandler

import database
from config import Settings
from handlers.commands import (
    on_court_leaderboard,
    on_my_jester_stats,
    register_command_handlers,
)
from models import Draw, Group, Player
from tests.conftest import requires_db


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


def _fake_command_update(
    *,
    chat_id: int,
    user_id: int | None = None,
    first_name: str | None = "U",
    last_name: str | None = None,
    username: str | None = None,
) -> tuple[MagicMock, AsyncMock]:
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_chat = chat
    update.effective_message = message
    if user_id is not None:
        user = MagicMock()
        user.id = user_id
        user.first_name = first_name
        user.last_name = last_name
        user.username = username
        update.effective_user = user
    else:
        update.effective_user = None
    return update, message.reply_text


def _reply_text(reply: AsyncMock) -> str:
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


async def _seed_group_with_players_and_draws(
    session: AsyncSession,
    *,
    telegram_id: int,
    player_draws: list[tuple[int, str | None, int]],  # One tuple per player: (telegram_id, username, draw_count).
    approved: bool = True,
    last_names: dict[int, str] | None = None,
) -> tuple[Group, dict[int, Player]]:
    group = Group(
        status="member",
        telegram_id=telegram_id,
        telegram_title="G",
        approved=approved,
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    players: dict[int, Player] = {}
    day_cursor = 0
    base = date.today() - timedelta(days=5000)
    last_names = last_names or {}
    for tg_id, username, draws in player_draws:
        p = Player(
            status="member",
            group_id=group.id,
            telegram_id=tg_id,
            telegram_first_name="First",
            telegram_last_name=last_names.get(tg_id),
            telegram_username=username,
            weight=3,
        )
        session.add(p)
        await session.commit()
        assert p.id is not None
        players[tg_id] = p
        for _ in range(draws):
            session.add(
                Draw(
                    draw_date=base + timedelta(days=day_cursor),
                    group_id=group.id,
                    player_id=p.id,
                )
            )
            day_cursor += 1
        await session.commit()
    return group, players


def test_register_command_handlers_adds_two_handlers() -> None:
    app = MagicMock(spec=Application)
    app.bot_data = {"settings": _test_settings()}
    register_command_handlers(app)
    assert app.add_handler.call_count == 2
    commands = set()
    for call in app.add_handler.call_args_list:
        handler = call.args[0]
        assert isinstance(handler, CommandHandler)
        commands.update(handler.commands)
    assert commands == {"court_leaderboard", "my_jester_stats"}


@requires_db
async def test_leaderboard_unapproved_group(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    await _seed_group_with_players_and_draws(session, telegram_id=5000, player_draws=[], approved=False)
    settings = _test_settings()
    update, reply = _fake_command_update(chat_id=5000)
    await on_court_leaderboard(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)


@requires_db
async def test_leaderboard_not_enough_players(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=5)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=5001,
        player_draws=[(1, "a", 0), (2, "b", 0)],
    )
    update, reply = _fake_command_update(chat_id=5001)
    await on_court_leaderboard(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(settings.NOT_ENOUGH_PLAYERS_MESSAGE.format_map({"min_players": 5}))


@requires_db
async def test_leaderboard_no_draws_yet(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=5002,
        player_draws=[(10, "a", 0), (11, "b", 0)],
    )
    update, reply = _fake_command_update(chat_id=5002)
    await on_court_leaderboard(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(settings.LEADERBOARD_NOT_ENOUGH_PICKED_PLAYERS_MESSAGE)


@requires_db
async def test_leaderboard_renders_top_with_competition_ranking(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that draw counts ``(5, 5, 3, 2)`` render as competition ranks ``(1, 1, 3, 4)``."""
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=5003,
        player_draws=[(1, "alpha", 5), (2, "beta", 5), (3, "gamma", 3), (4, "delta", 2)],
    )
    update, reply = _fake_command_update(chat_id=5003)
    await on_court_leaderboard(update, _fake_context(settings=settings))

    sent = _reply_text(reply)
    lines = sent.split("\n")
    assert lines[0] == settings.LEADERBOARD_INTRO_MESSAGE
    assert lines[-1] == settings.LEADERBOARD_OUTRO_MESSAGE
    entries = lines[1:-1]
    assert entries[0].startswith("1. @alpha - 5") or entries[0].startswith("1. @beta - 5")
    assert entries[1].startswith("1. ")
    assert entries[2].startswith("3. @gamma - 3")
    assert entries[3].startswith("4. @delta - 2")


@requires_db
async def test_leaderboard_limits_to_ten(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    player_draws = [(100 + i, f"p{i}", 12 - i) for i in range(12)]
    await _seed_group_with_players_and_draws(session, telegram_id=5004, player_draws=player_draws)
    update, reply = _fake_command_update(chat_id=5004)
    await on_court_leaderboard(update, _fake_context(settings=settings))
    sent = _reply_text(reply)
    entries = sent.split("\n")[1:-1]
    assert len(entries) == 10


@requires_db
@pytest.mark.parametrize(
    ("counts", "expected_ranks"),
    [
        ([5, 4, 3, 2, 1], [1, 2, 3, 4, 5]),
        ([5, 5, 3, 2], [1, 1, 3, 4]),
        ([5, 3, 3, 3, 1], [1, 2, 2, 2, 5]),
        ([4, 4, 4, 4], [1, 1, 1, 1]),
        ([5, 5, 3, 3, 1, 1], [1, 1, 3, 3, 5, 5]),
    ],
)
async def test_leaderboard_ranking_edge_cases(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    counts: list[int],
    expected_ranks: list[int],
) -> None:
    settings = _test_settings(min_players=2)
    player_draws = [(i + 1, f"u{i}", c) for i, c in enumerate(counts)]
    await _seed_group_with_players_and_draws(session, telegram_id=6000 + sum(counts), player_draws=player_draws)
    update, reply = _fake_command_update(chat_id=6000 + sum(counts))
    await on_court_leaderboard(update, _fake_context(settings=settings))
    entries = _reply_text(reply).split("\n")[1:-1]
    ranks_in_output = [int(line.split(".", 1)[0]) for line in entries]
    assert ranks_in_output == expected_ranks


@requires_db
async def test_stats_unapproved_group(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    await _seed_group_with_players_and_draws(session, telegram_id=7000, player_draws=[], approved=False)
    settings = _test_settings()
    update, reply = _fake_command_update(chat_id=7000, user_id=1)
    await on_my_jester_stats(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)


@requires_db
async def test_stats_unregistered_user(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7001,
        player_draws=[(1, "a", 1), (2, "b", 1)],
    )
    update, reply = _fake_command_update(chat_id=7001, user_id=999, first_name="Stranger")
    await on_my_jester_stats(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(
        settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE.format_map({"username": "Stranger"})
    )


@requires_db
async def test_stats_registered_user_zero_draws(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7002,
        player_draws=[(50, "zero", 0), (51, "other", 1)],
    )
    update, reply = _fake_command_update(chat_id=7002, user_id=50)
    await on_my_jester_stats(update, _fake_context(settings=settings))
    sent = _reply_text(reply)
    assert sent == settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE.format_map({"username": "@zero"})


@requires_db
async def test_stats_registered_user_with_draws(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7003,
        player_draws=[(80, "top", 5), (81, "mid", 3), (82, "low", 1)],
    )
    update, reply = _fake_command_update(chat_id=7003, user_id=81)
    await on_my_jester_stats(update, _fake_context(settings=settings))
    sent = _reply_text(reply)
    assert sent == settings.PERSONAL_STATS_MESSAGE.format_map({"username": "@mid", "draw_count": 3, "rank": 2})


@requires_db
async def test_stats_rank_matches_leaderboard_for_tied_player(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    """Verify that a tied player's personal rank equals their rank in the rendered leaderboard."""
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7004,
        player_draws=[(10, "alpha", 5), (11, "beta", 5), (12, "gamma", 2)],
    )

    update_lb, reply_lb = _fake_command_update(chat_id=7004)
    await on_court_leaderboard(update_lb, _fake_context(settings=settings))
    lb_entries = _reply_text(reply_lb).split("\n")[1:-1]
    beta_line = next(line for line in lb_entries if "@beta" in line)
    beta_rank_in_lb = int(beta_line.split(".", 1)[0])

    update_ps, reply_ps = _fake_command_update(chat_id=7004, user_id=11)
    await on_my_jester_stats(update_ps, _fake_context(settings=settings))
    sent = _reply_text(reply_ps)
    expected = settings.PERSONAL_STATS_MESSAGE.format_map(
        {"username": "@beta", "draw_count": 5, "rank": beta_rank_in_lb}
    )
    assert sent == expected
    assert beta_rank_in_lb == 1


@requires_db
async def test_stats_unregistered_user_prefers_username(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7010,
        player_draws=[(1, "a", 1), (2, "b", 1)],
    )
    update, reply = _fake_command_update(
        chat_id=7010, user_id=999, first_name="Stranger", username="stranger_handle"
    )
    await on_my_jester_stats(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(
        settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE.format_map({"username": "@stranger_handle"})
    )


@requires_db
async def test_stats_unregistered_user_falls_back_to_id(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7011,
        player_draws=[(1, "a", 1), (2, "b", 1)],
    )
    update, reply = _fake_command_update(
        chat_id=7011, user_id=424242, first_name=None, username=None
    )
    await on_my_jester_stats(update, _fake_context(settings=settings))
    reply.assert_awaited_once_with(
        settings.PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE.format_map({"username": "424242"})
    )


@requires_db
async def test_stats_registered_player_includes_last_name(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings(min_players=2)
    await _seed_group_with_players_and_draws(
        session,
        telegram_id=7012,
        player_draws=[(50, None, 2), (51, None, 1)],
        last_names={50: "Last", 51: "Other"},
    )

    update_lb, reply_lb = _fake_command_update(chat_id=7012)
    await on_court_leaderboard(update_lb, _fake_context(settings=settings))
    assert "First Last" in _reply_text(reply_lb)

    update_ps, reply_ps = _fake_command_update(chat_id=7012, user_id=50)
    await on_my_jester_stats(update_ps, _fake_context(settings=settings))
    sent_ps = _reply_text(reply_ps)
    assert sent_ps == settings.PERSONAL_STATS_MESSAGE.format_map(
        {"username": "First Last", "draw_count": 2, "rank": 1}
    )
