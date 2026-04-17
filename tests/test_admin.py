"""Tests for the admin weight-change wizard: keyboards, tier detection, and confirmation."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
)

import database
from config import Settings
from handlers.admin import (
    _build_group_keyboard,
    _build_player_keyboard,
    _build_weight_keyboard,
    on_weight_confirm,
    register_admin_handlers,
    start_weight_change,
)
from models import Group, Player
from tests.conftest import requires_db


def _test_settings(**env: str) -> Settings:
    import os

    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    for k, v in env.items():
        os.environ[k] = v
    return Settings()  # ty: ignore[missing-argument]


def _fake_context(settings: Settings, *, user_data: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    app = MagicMock()
    app.bot_data = {"settings": settings}
    ctx.application = app
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = AsyncMock()
    return ctx


def _fake_update_msg(user_id: int) -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_user = user
    update.effective_message = message
    update.callback_query = None
    return update


def _fake_update_query(user_id: int, data: str) -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    q = MagicMock()
    q.data = data
    q.from_user = user
    q.answer = AsyncMock()
    q.message = MagicMock()
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


def test_register_admin_handlers_registers_all() -> None:
    app = MagicMock(spec=Application)
    register_admin_handlers(app)
    handlers = [call.args[0] for call in app.add_handler.call_args_list]
    assert any(isinstance(h, CommandHandler) for h in handlers)
    patterns = {h.pattern.pattern for h in handlers if isinstance(h, CallbackQueryHandler)}
    assert r"^wg:\d+$" in patterns
    assert r"^wp:\d+:\d+$" in patterns
    assert r"^ws:\d+$" in patterns
    assert r"^wc:\d+:\d+$" in patterns
    assert r"^wb:(g|p:\d+)$" in patterns


def test_build_group_keyboard_first_page_has_only_next() -> None:
    groups = [Group(id=i, status="active", telegram_id=i, telegram_title=f"G{i}") for i in range(1, 4)]
    kb = _build_group_keyboard(groups, page=0, total=8, per_page=3)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "◀ Previous" not in labels
    assert "Next ▶" in labels


def test_build_group_keyboard_middle_page_has_both() -> None:
    groups = [Group(id=i, status="active", telegram_id=i, telegram_title=f"G{i}") for i in range(4, 7)]
    kb = _build_group_keyboard(groups, page=1, total=8, per_page=3)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "◀ Previous" in labels
    assert "Next ▶" in labels


def test_build_group_keyboard_last_page_has_only_prev() -> None:
    groups = [Group(id=i, status="active", telegram_id=i, telegram_title=f"G{i}") for i in range(7, 9)]
    kb = _build_group_keyboard(groups, page=2, total=8, per_page=3)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "◀ Previous" in labels
    assert "Next ▶" not in labels


def test_build_player_keyboard_includes_back() -> None:
    players = [
        Player(
            id=1,
            status="member",
            group_id=1,
            telegram_id=10,
            telegram_first_name="Alice",
            weight=3,
        )
    ]
    kb = _build_player_keyboard(players, group_id=1, page=0, total=1, per_page=5)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Alice" in label and "w:3" in label for label in labels)
    assert "↩ Back to groups" in labels


def test_build_player_keyboard_uses_username_when_available() -> None:
    players = [
        Player(
            id=1,
            status="member",
            group_id=1,
            telegram_id=10,
            telegram_first_name="Alice",
            telegram_username="alice",
            weight=3,
        )
    ]
    kb = _build_player_keyboard(players, group_id=1, page=0, total=1, per_page=5)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("@alice" in label and "w:3" in label for label in labels)
    assert not any("Alice" in label and "@" not in label for label in labels)


def test_build_player_keyboard_joins_first_and_last_without_username() -> None:
    players = [
        Player(
            id=1,
            status="member",
            group_id=1,
            telegram_id=10,
            telegram_first_name="Alice",
            telegram_last_name="Doe",
            weight=3,
        )
    ]
    kb = _build_player_keyboard(players, group_id=1, page=0, total=1, per_page=5)
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("Alice Doe" in label and "w:3" in label for label in labels)


def test_build_weight_keyboard_marks_current() -> None:
    kb = _build_weight_keyboard(player_id=7, group_id=3, current=3, min_w=1, max_w=5)
    weight_row = kb.inline_keyboard[0]
    labels = [btn.text for btn in weight_row]
    assert labels == ["1", "2", "✓ 3", "4", "5"]
    back = kb.inline_keyboard[1][0]
    assert back.callback_data == "wb:p:3"


@requires_db
async def test_non_admin_receives_rejection(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    update = _fake_update_msg(user_id=999)
    ctx = _fake_context(settings)

    await start_weight_change(update, ctx)

    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")


@requires_db
async def test_user_id_admin_sees_all_approved_groups(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _test_settings()
    session.add(Group(status="active", telegram_id=1, telegram_title="Approved-A", approved=True))
    session.add(Group(status="active", telegram_id=2, telegram_title="Approved-B", approved=True))
    session.add(Group(status="active", telegram_id=3, telegram_title="Pending-C", approved=False))
    await session.commit()

    update = _fake_update_msg(user_id=42)
    ctx = _fake_context(settings)

    await start_weight_change(update, ctx)

    update.effective_message.reply_text.assert_awaited_once()
    kwargs = update.effective_message.reply_text.await_args.kwargs
    kb = kwargs["reply_markup"]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "Approved-A" in labels
    assert "Approved-B" in labels
    assert "Pending-C" not in labels


@requires_db
async def test_status_admin_sees_only_their_groups(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    g1 = Group(status="active", telegram_id=101, telegram_title="HasAdmin", approved=True)
    g2 = Group(status="active", telegram_id=102, telegram_title="NoAdmin", approved=True)
    session.add(g1)
    session.add(g2)
    await session.commit()
    assert g1.id is not None and g2.id is not None
    session.add(
        Player(
            status="administrator",
            group_id=g1.id,
            telegram_id=555,
            telegram_first_name="Bob",
            weight=3,
        )
    )
    session.add(
        Player(
            status="member",
            group_id=g2.id,
            telegram_id=555,
            telegram_first_name="Bob",
            weight=3,
        )
    )
    await session.commit()

    update = _fake_update_msg(user_id=555)
    ctx = _fake_context(settings)

    await start_weight_change(update, ctx)

    kwargs = update.effective_message.reply_text.await_args.kwargs
    kb = kwargs["reply_markup"]
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "HasAdmin" in labels
    assert "NoAdmin" not in labels
    assert ctx.user_data["wc_admin_tier"] == "status"


@requires_db
async def test_weight_confirm_persists_and_logs(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _test_settings()
    group = Group(status="active", telegram_id=200, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    player = Player(
        status="member",
        group_id=group.id,
        telegram_id=777,
        telegram_first_name="Carol",
        weight=3,
    )
    session.add(player)
    await session.commit()
    assert player.id is not None

    calls: list[dict] = []

    def _capture(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("handlers.admin.log_weight_change", _capture)

    update = _fake_update_query(user_id=42, data=f"wc:{player.id}:5")
    ctx = _fake_context(settings, user_data={"wc_admin_tier": "user_id"})

    await on_weight_confirm(update, ctx)

    await session.refresh(player)
    refreshed = (await session.execute(select(Player).where(Player.id == player.id))).scalars().first()
    assert refreshed is not None
    assert refreshed.weight == 5

    assert len(calls) == 1
    assert calls[0] == {
        "admin_id": 42,
        "player_id": 777,
        "old_weight": 3,
        "new_weight": 5,
    }
    update.callback_query.edit_message_text.assert_awaited()


@requires_db
async def test_weight_confirm_rejects_non_admin(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    settings = _test_settings()
    group = Group(status="active", telegram_id=300, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    player = Player(
        status="member",
        group_id=group.id,
        telegram_id=9001,
        telegram_first_name="Dave",
        weight=2,
    )
    session.add(player)
    await session.commit()
    assert player.id is not None

    # User 999 holds neither the User-ID admin right nor a status-tier admin right.
    update = _fake_update_query(user_id=999, data=f"wc:{player.id}:5")
    ctx = _fake_context(settings)

    await on_weight_confirm(update, ctx)

    await session.refresh(player)
    assert player.weight == 2
    # The handler calls ``answer`` twice: once to acknowledge, once with ``show_alert`` to reject.
    assert update.callback_query.answer.await_count >= 2


async def test_weight_confirm_out_of_range_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _test_settings()
    update = _fake_update_query(user_id=42, data=f"wc:1:{settings.MAX_WEIGHT + 1}")
    ctx = _fake_context(settings, user_data={"wc_admin_tier": "user_id"})

    await on_weight_confirm(update, ctx)
    update.callback_query.edit_message_text.assert_not_called()
    # Two ``answer`` calls are expected: the initial acknowledgement and the ``show_alert`` rejection.
    assert update.callback_query.answer.await_count >= 2


@requires_db
async def test_weight_confirm_change_message_uses_username(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _test_settings()
    group = Group(status="active", telegram_id=210, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    player = Player(
        status="member",
        group_id=group.id,
        telegram_id=778,
        telegram_first_name="Carol",
        telegram_username="carol_handle",
        weight=3,
    )
    session.add(player)
    await session.commit()
    assert player.id is not None

    monkeypatch.setattr("handlers.admin.log_weight_change", lambda **_: None)

    update = _fake_update_query(user_id=42, data=f"wc:{player.id}:5")
    ctx = _fake_context(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_confirm(update, ctx)

    text = update.callback_query.edit_message_text.await_args.args[0]
    assert text == "@carol_handle's weight changed from 3 to 5."


@requires_db
async def test_weight_confirm_no_change_message_uses_display_name(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "42")
    settings = _test_settings()
    group = Group(status="active", telegram_id=211, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    player = Player(
        status="member",
        group_id=group.id,
        telegram_id=779,
        telegram_first_name="Eve",
        telegram_last_name="Smith",
        weight=3,
    )
    session.add(player)
    await session.commit()
    assert player.id is not None

    update = _fake_update_query(user_id=42, data=f"wc:{player.id}:3")
    ctx = _fake_context(settings, user_data={"wc_admin_tier": "user_id"})
    await on_weight_confirm(update, ctx)

    text = update.callback_query.edit_message_text.await_args.args[0]
    assert text == "Eve Smith's weight is already 3. No change."
