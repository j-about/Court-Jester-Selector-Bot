"""Tests for the admin wizard callback handlers.

Covers the callback handlers that ``test_admin.py`` does not exercise:
group pagination, player pagination, the weight picker, back navigation,
and the remaining branches of weight confirmation.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import database
from config import Settings
from handlers.admin import (
    on_group_page,
    on_player_page,
    on_weight_back,
    on_weight_confirm,
    on_weight_select,
)
from models import Group, Player
from tests.conftest import requires_db


def _settings() -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    return Settings()  # ty: ignore[missing-argument]


def _ctx(settings: Settings, *, user_data: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    app = MagicMock()
    app.bot_data = {"settings": settings}
    ctx.application = app
    ctx.user_data = user_data if user_data is not None else {}
    return ctx


def _query_update(user_id: int, data: str) -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    q = MagicMock()
    q.data = data
    q.from_user = user
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    update.callback_query = q
    update.effective_user = user
    return update


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


async def _seed_group_and_players(session: AsyncSession, n_players: int = 3) -> tuple[Group, list[Player]]:
    g = Group(status="active", telegram_id=4242, telegram_title="G", approved=True)
    session.add(g)
    await session.commit()
    assert g.id is not None
    players: list[Player] = []
    for i in range(n_players):
        p = Player(
            status="member",
            group_id=g.id,
            telegram_id=900 + i,
            telegram_first_name=f"Pl{i}",
            weight=3,
        )
        session.add(p)
        players.append(p)
    await session.commit()
    return g, players


@requires_db
async def test_on_group_page_renders_requested_page(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    for i in range(7):
        session.add(Group(status="active", telegram_id=1000 + i, telegram_title=f"G{i}", approved=True))
    await session.commit()

    update = _query_update(user_id=42, data="wg:1")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_group_page(update, ctx)

    update.callback_query.edit_message_text.assert_awaited_once()
    text, kwargs = update.callback_query.edit_message_text.await_args.args[0], update.callback_query.edit_message_text.await_args.kwargs
    assert text == "Select a group:"
    assert kwargs["reply_markup"] is not None


@requires_db
async def test_on_group_page_rejects_non_admin(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _settings()
    update = _query_update(user_id=999, data="wg:0")
    ctx = _ctx(settings)
    await on_group_page(update, ctx)
    # Non-admin caller: a second ``answer`` call delivers the rejection alert.
    assert update.callback_query.answer.await_count == 2
    update.callback_query.answer.await_args.assert_called  # noqa: B015


@requires_db
async def test_on_group_page_ignores_malformed_callback_data(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    update = _query_update(user_id=42, data="wg:not-a-number")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_group_page(update, ctx)
    update.callback_query.edit_message_text.assert_not_awaited()


@requires_db
async def test_on_player_page_lists_players_for_group(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    g, _ = await _seed_group_and_players(session, n_players=3)
    update = _query_update(user_id=42, data=f"wp:{g.id}:0")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_player_page(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()
    assert update.callback_query.edit_message_text.await_args.args[0] == "Select a player:"
    assert ctx.user_data["wc_group_id"] == g.id


@requires_db
async def test_on_player_page_with_no_players_shows_back_button(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    g = Group(status="active", telegram_id=4243, telegram_title="Empty", approved=True)
    session.add(g)
    await session.commit()
    assert g.id is not None
    update = _query_update(user_id=42, data=f"wp:{g.id}:0")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_player_page(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "No players" in text


@requires_db
async def test_on_player_page_ignores_malformed_data(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    update = _query_update(user_id=42, data="wp:bad:data")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_player_page(update, ctx)
    update.callback_query.edit_message_text.assert_not_awaited()


@requires_db
async def test_on_weight_select_renders_weight_picker(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    _, players = await _seed_group_and_players(session)
    pid = players[0].id
    assert pid is not None
    update = _query_update(user_id=42, data=f"ws:{pid}")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_select(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "Select new weight" in text


@requires_db
async def test_on_weight_select_uses_display_name_with_username(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    g = Group(status="active", telegram_id=4250, telegram_title="G", approved=True)
    session.add(g)
    await session.commit()
    assert g.id is not None
    p = Player(
        status="member",
        group_id=g.id,
        telegram_id=901,
        telegram_first_name="Pl0",
        telegram_username="pl0_handle",
        weight=3,
    )
    session.add(p)
    await session.commit()
    assert p.id is not None

    update = _query_update(user_id=42, data=f"ws:{p.id}")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_select(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "@pl0_handle" in text
    # The bare first name must not also appear alongside the ``@username`` form.
    assert "Pl0 " not in text


@requires_db
async def test_on_weight_select_missing_player_reports_gone(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    update = _query_update(user_id=42, data="ws:99999")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_select(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "Player no longer exists" in text


@requires_db
async def test_on_weight_confirm_no_change_message(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    _, players = await _seed_group_and_players(session)
    pid = players[0].id
    assert pid is not None
    # The seeded players have ``weight=3``; passing the same value exercises the no-change branch.
    update = _query_update(user_id=42, data=f"wc:{pid}:3")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_confirm(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "already 3" in text


@requires_db
async def test_on_weight_confirm_missing_player(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    update = _query_update(user_id=42, data="wc:99999:3")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_confirm(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "Player no longer exists" in text


@requires_db
async def test_on_weight_back_to_groups(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    session.add(Group(status="active", telegram_id=8000, telegram_title="G1", approved=True))
    await session.commit()
    update = _query_update(user_id=42, data="wb:g")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_back(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()


@requires_db
async def test_on_weight_back_to_players(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    g, _ = await _seed_group_and_players(session, n_players=2)
    update = _query_update(user_id=42, data=f"wb:p:{g.id}")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_back(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert text == "Select a player:"


@requires_db
async def test_on_weight_back_to_players_empty_group(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    g = Group(status="active", telegram_id=8001, telegram_title="Empty", approved=True)
    session.add(g)
    await session.commit()
    update = _query_update(user_id=42, data=f"wb:p:{g.id}")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_back(update, ctx)
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "No players" in text


@requires_db
async def test_on_weight_back_invalid_group_id(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    update = _query_update(user_id=42, data="wb:p:notanint")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_back(update, ctx)
    update.callback_query.edit_message_text.assert_not_awaited()


@requires_db
async def test_on_weight_confirm_out_of_range(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that a weight above ``MAX_WEIGHT`` is reported via ``answer`` with no message edit."""
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _settings()
    _, players = await _seed_group_and_players(session)
    pid = players[0].id
    assert pid is not None
    update = _query_update(user_id=42, data=f"wc:{pid}:99")
    ctx = _ctx(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_confirm(update, ctx)
    update.callback_query.edit_message_text.assert_not_awaited()
    # The second ``answer`` call carries the out-of-range alert.
    assert update.callback_query.answer.await_count >= 2
