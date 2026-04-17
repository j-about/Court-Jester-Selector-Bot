"""Tests for membership lifecycle handlers and approve / reject callbacks."""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
)

import database
from config import Settings
from handlers.lifecycle import (
    notify_admins_new_group,
    on_approve_callback,
    on_my_chat_member,
    on_reject_callback,
    register_lifecycle_handlers,
)
from models import Draw, Group, Player
from tests.conftest import requires_db
from utils.command_menu import admin_commands, game_commands


def test_register_lifecycle_handlers_adds_three_handlers() -> None:
    app = MagicMock(spec=Application)
    register_lifecycle_handlers(app)
    assert app.add_handler.call_count == 3
    handlers = [call.args[0] for call in app.add_handler.call_args_list]
    assert isinstance(handlers[0], ChatMemberHandler)
    assert isinstance(handlers[1], CallbackQueryHandler)
    assert isinstance(handlers[2], CallbackQueryHandler)
    # ``CallbackQueryHandler`` stores the compiled regex on ``.pattern``.
    assert handlers[1].pattern.pattern == r"^approve:\d+$"
    assert handlers[2].pattern.pattern == r"^reject:\d+$"


def _test_settings(admin_ids: list[int] | None = None) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    if admin_ids is not None:
        os.environ["TG_BOT_ADMIN_RIGHTS_USER_IDS"] = ",".join(str(i) for i in admin_ids)
    else:
        os.environ.pop("TG_BOT_ADMIN_RIGHTS_USER_IDS", None)
    return Settings()  # ty: ignore[missing-argument]


def _fake_context(
    *,
    bot: AsyncMock | None = None,
    settings: Settings | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    app = MagicMock()
    app.bot_data = {"settings": settings or _test_settings()}
    ctx.application = app
    return ctx


def _fake_member_update(
    *,
    chat_id: int,
    chat_title: str | None = "My Group",
    old_status: str,
    new_status: str,
) -> MagicMock:
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    chat.title = chat_title
    old = MagicMock()
    old.status = old_status
    new = MagicMock()
    new.status = new_status
    my = MagicMock()
    my.chat = chat
    my.old_chat_member = old
    my.new_chat_member = new
    update.my_chat_member = my
    return update


def _fake_callback_update(
    *,
    user_id: int,
    data: str,
    message_chat_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> tuple[MagicMock, AsyncMock]:
    update = MagicMock()
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.data = data
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    query.from_user = user
    if message_chat_id is not None:
        msg = MagicMock()
        msg.chat = MagicMock()
        msg.chat.id = message_chat_id
        query.message = msg
    else:
        query.message = None
    update.callback_query = query
    return update, query


@pytest.fixture
def patched_db(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(database, "_sessionmaker", db_sessionmaker, raising=False)
    monkeypatch.setattr(database, "_engine", MagicMock(), raising=False)
    return db_sessionmaker


async def test_non_admin_approve_callback_silently_ignored() -> None:
    bot = AsyncMock()
    settings = _test_settings(admin_ids=[100, 200])
    update, query = _fake_callback_update(user_id=999, data="approve:1")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_approve_callback(update, ctx)

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_called()
    bot.edit_message_text.assert_not_called()
    bot.send_message.assert_not_called()
    bot.leave_chat.assert_not_called()


async def test_non_admin_reject_callback_silently_ignored() -> None:
    bot = AsyncMock()
    settings = _test_settings(admin_ids=[100])
    update, query = _fake_callback_update(user_id=999, data="reject:1")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_reject_callback(update, ctx)

    query.answer.assert_awaited_once()
    bot.leave_chat.assert_not_called()


@requires_db
async def test_notify_admins_fans_out_and_isolates_errors(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="member", telegram_id=5000, telegram_title="Title")
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()

    sent_msg_ok = MagicMock()
    sent_msg_ok.chat_id = 200
    sent_msg_ok.message_id = 42

    async def send(**kwargs: object) -> object:
        if kwargs["chat_id"] == 100:
            raise TelegramError("blocked")
        return sent_msg_ok

    bot.send_message.side_effect = send

    settings = _test_settings(admin_ids=[100, 200])
    await notify_admins_new_group(bot, group.id, group.telegram_id, group.telegram_title, settings)

    assert bot.send_message.await_count == 2

    await session.refresh(group)
    assert group.approval_messages == {"messages": {"200": {"chat_id": 200, "message_id": 42}}}


@requires_db
async def test_bot_added_to_new_group_inserts_and_notifies(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    bot = AsyncMock()
    sent = MagicMock()
    sent.chat_id = 500
    sent.message_id = 7
    bot.send_message.return_value = sent

    settings = _test_settings(admin_ids=[500])
    update = _fake_member_update(chat_id=1234, chat_title="New Group", old_status="left", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    result = await session.execute(select(Group).where(Group.telegram_id == 1234))
    group = result.scalars().first()
    assert group is not None
    assert group.approved is False
    assert group.status == "member"
    assert group.telegram_title == "New Group"
    bot.send_message.assert_awaited_once()
    assert group.approval_messages == {"messages": {"500": {"chat_id": 500, "message_id": 7}}}


@requires_db
async def test_reinstall_of_approved_group_does_not_renotify(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="left",
        telegram_id=2000,
        telegram_title="Old",
        approved=True,
    )
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[111])
    update = _fake_member_update(chat_id=2000, chat_title="Renamed", old_status="left", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    await session.refresh(group)
    assert group.approved is True
    assert group.status == "member"
    assert group.telegram_title == "Renamed"
    bot.send_message.assert_not_called()


@requires_db
async def test_reinstall_of_unapproved_group_renotifies(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="kicked",
        telegram_id=2100,
        telegram_title="Pending",
        approved=False,
    )
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    sent = MagicMock()
    sent.chat_id = 222
    sent.message_id = 9
    bot.send_message.return_value = sent

    settings = _test_settings(admin_ids=[222])
    update = _fake_member_update(chat_id=2100, chat_title="Pending", old_status="kicked", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    await session.refresh(group)
    assert group.approved is False
    assert group.status == "member"
    bot.send_message.assert_awaited_once()


@requires_db
async def test_bot_removed_updates_status_preserves_children(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="member", telegram_id=3000, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()
    assert group.id is not None
    player = Player(
        status="member",
        group_id=group.id,
        telegram_id=55,
        telegram_first_name="P",
        weight=3,
    )
    session.add(player)
    await session.commit()
    assert player.id is not None
    session.add(Draw(draw_date=date.today(), group_id=group.id, player_id=player.id))
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[])
    update = _fake_member_update(chat_id=3000, old_status="member", new_status="kicked")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    await session.refresh(group)
    assert group.status == "kicked"
    # Child ``Player`` and ``Draw`` rows must not be deleted by the removal handler.
    players = (await session.execute(select(Player).where(Player.group_id == group.id))).scalars().all()
    assert len(players) == 1
    draws = (await session.execute(select(Draw).where(Draw.group_id == group.id))).scalars().all()
    assert len(draws) == 1
    bot.send_message.assert_not_called()


@requires_db
async def test_approve_callback_flips_flag_and_skips_group_message(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="member",
        telegram_id=4000,
        telegram_title="Kingdom",
        approved=False,
        approval_messages={
            "messages": {
                "100": {"chat_id": 100, "message_id": 11},
                "200": {"chat_id": 200, "message_id": 12},
            }
        },
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[100, 200])
    update, query = _fake_callback_update(
        user_id=100,
        data=f"approve:{group.id}",
        message_chat_id=100,
        username="alice",
    )
    ctx = _fake_context(bot=bot, settings=settings)

    await on_approve_callback(update, ctx)

    await session.refresh(group)
    assert group.approved is True
    assert group.approval_messages is not None
    decision = group.approval_messages["decision"]
    assert decision["admin_telegram_id"] == 100
    assert decision["action"] == "approved"
    assert "decided_at" in decision

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    # The acting admin's own prompt is edited via ``query``; the other admin's prompt is edited via ``bot``.
    bot.edit_message_text.assert_awaited_once()
    edit_kwargs = bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["chat_id"] == 200
    assert edit_kwargs["message_id"] == 12
    assert edit_kwargs["text"] == "✅ Approved by admin @alice — Kingdom"
    # The group chat itself must not receive any decision message.
    assert not any(call.kwargs.get("chat_id") == group.telegram_id for call in bot.send_message.await_args_list)
    bot.leave_chat.assert_not_called()


async def _run_approve_and_get_broadcast_text(
    session: AsyncSession,
    *,
    telegram_id: int,
    title: str,
    acting_admin_id: int,
    observer_admin_id: int,
    observer_chat_id: int,
    observer_message_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    group = Group(
        status="member",
        telegram_id=telegram_id,
        telegram_title=title,
        approved=False,
        approval_messages={
            "messages": {
                str(acting_admin_id): {"chat_id": acting_admin_id, "message_id": 1},
                str(observer_admin_id): {"chat_id": observer_chat_id, "message_id": observer_message_id},
            }
        },
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[acting_admin_id, observer_admin_id])
    update, _query = _fake_callback_update(
        user_id=acting_admin_id,
        data=f"approve:{group.id}",
        message_chat_id=acting_admin_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )
    ctx = _fake_context(bot=bot, settings=settings)

    await on_approve_callback(update, ctx)

    bot.edit_message_text.assert_awaited_once()
    return bot.edit_message_text.await_args.kwargs["text"]


@requires_db
async def test_approve_broadcast_uses_username_when_present(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    text = await _run_approve_and_get_broadcast_text(
        session,
        telegram_id=4300,
        title="Kingdom",
        acting_admin_id=100,
        observer_admin_id=200,
        observer_chat_id=200,
        observer_message_id=12,
        username="alice",
        first_name="Unused",
        last_name="Unused",
    )
    assert text == "✅ Approved by admin @alice — Kingdom"


@requires_db
async def test_approve_broadcast_uses_full_name_when_no_username(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    text = await _run_approve_and_get_broadcast_text(
        session,
        telegram_id=4400,
        title="Realm",
        acting_admin_id=101,
        observer_admin_id=201,
        observer_chat_id=201,
        observer_message_id=13,
        first_name="Alice",
        last_name="Martin",
    )
    assert text == "✅ Approved by admin Alice Martin — Realm"


@requires_db
async def test_approve_broadcast_falls_back_to_id_when_no_identity(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    text = await _run_approve_and_get_broadcast_text(
        session,
        telegram_id=4500,
        title="Domain",
        acting_admin_id=102,
        observer_admin_id=202,
        observer_chat_id=202,
        observer_message_id=14,
    )
    assert text == "✅ Approved by admin 102 — Domain"


@requires_db
async def test_reject_callback_leaves_chat_and_sets_status(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="member",
        telegram_id=4100,
        telegram_title="Realm",
        approved=False,
        approval_messages={"messages": {"300": {"chat_id": 300, "message_id": 15}}},
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[300])
    update, _query = _fake_callback_update(user_id=300, data=f"reject:{group.id}", message_chat_id=300)
    ctx = _fake_context(bot=bot, settings=settings)

    await on_reject_callback(update, ctx)

    await session.refresh(group)
    assert group.approved is False
    assert group.status == "rejected"
    assert group.approval_messages is not None
    assert group.approval_messages["decision"]["action"] == "rejected"

    bot.leave_chat.assert_awaited_once_with(chat_id=4100)


@requires_db
async def test_add_to_previously_approved_group_syncs_game_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="left",
        telegram_id=8000,
        telegram_title="Old",
        approved=True,
    )
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[])
    update = _fake_member_update(chat_id=8000, chat_title="Old", old_status="left", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == game_commands(settings)
    assert kwargs["scope"].chat_id == 8000


@requires_db
async def test_add_to_unapproved_group_does_not_set_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    bot = AsyncMock()
    settings = _test_settings(admin_ids=[999])
    update = _fake_member_update(chat_id=8001, chat_title="New", old_status="left", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    bot.set_my_commands.assert_not_awaited()


@requires_db
async def test_add_to_new_group_auto_approves_when_no_admins(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    bot = AsyncMock()
    settings = _test_settings(admin_ids=[])
    update = _fake_member_update(chat_id=8500, chat_title="Open Realm", old_status="left", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    result = await session.execute(select(Group).where(Group.telegram_id == 8500))
    group = result.scalars().first()
    assert group is not None
    assert group.approved is True
    assert group.status == "member"
    assert group.telegram_title == "Open Realm"
    assert group.approval_messages is None

    bot.send_message.assert_not_awaited()
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == game_commands(settings)
    assert kwargs["scope"].chat_id == 8500


@requires_db
async def test_reinstall_of_unapproved_group_auto_approves_when_no_admins(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="kicked",
        telegram_id=8600,
        telegram_title="Pending",
        approved=False,
    )
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[])
    update = _fake_member_update(chat_id=8600, chat_title="Pending", old_status="kicked", new_status="member")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    await session.refresh(group)
    assert group.approved is True
    assert group.status == "member"

    bot.send_message.assert_not_awaited()
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert kwargs["commands"] == game_commands(settings)
    assert kwargs["scope"].chat_id == 8600


@requires_db
async def test_bot_removed_clears_group_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(status="member", telegram_id=8002, telegram_title="G", approved=True)
    session.add(group)
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[])
    update = _fake_member_update(chat_id=8002, old_status="member", new_status="kicked")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_my_chat_member(update, ctx)

    bot.delete_my_commands.assert_awaited_once()
    kwargs = bot.delete_my_commands.await_args.kwargs
    assert kwargs["scope"].chat_id == 8002


@requires_db
async def test_approve_callback_syncs_game_menu_and_status_admin_menus(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    group = Group(
        status="member",
        telegram_id=8100,
        telegram_title="Kingdom",
        approved=False,
    )
    session.add(group)
    await session.commit()
    assert group.id is not None
    session.add_all([
        Player(
            status="administrator",
            group_id=group.id,
            telegram_id=901,
            telegram_first_name="A1",
            weight=3,
        ),
        Player(
            status="member",
            group_id=group.id,
            telegram_id=902,
            telegram_first_name="M",
            weight=3,
        ),
    ])
    await session.commit()

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[500])
    update, _ = _fake_callback_update(user_id=500, data=f"approve:{group.id}", message_chat_id=500)
    ctx = _fake_context(bot=bot, settings=settings)

    await on_approve_callback(update, ctx)

    game_calls = [
        c for c in bot.set_my_commands.await_args_list
        if c.kwargs["commands"] == game_commands(settings)
    ]
    assert len(game_calls) == 1
    assert game_calls[0].kwargs["scope"].chat_id == 8100

    admin_calls = [
        c for c in bot.set_my_commands.await_args_list
        if c.kwargs["commands"] == admin_commands(settings)
    ]
    # Only the status-tier admin (901) receives the admin menu; the plain member (902) does not.
    admin_scope_ids = {c.kwargs["scope"].chat_id for c in admin_calls}
    assert admin_scope_ids == {901}


@requires_db
async def test_reject_callback_clears_group_menu(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="member",
        telegram_id=8200,
        telegram_title="Realm",
        approved=False,
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    settings = _test_settings(admin_ids=[500])
    update, _ = _fake_callback_update(user_id=500, data=f"reject:{group.id}")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_reject_callback(update, ctx)

    bot.delete_my_commands.assert_awaited_once()
    kwargs = bot.delete_my_commands.await_args.kwargs
    assert kwargs["scope"].chat_id == 8200
    bot.set_my_commands.assert_not_awaited()


@requires_db
async def test_reject_callback_swallows_leave_chat_error(
    patched_db: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    group = Group(
        status="member",
        telegram_id=4200,
        telegram_title="Realm",
        approved=False,
    )
    session.add(group)
    await session.commit()
    assert group.id is not None

    bot = AsyncMock()
    bot.leave_chat.side_effect = TelegramError("already gone")
    settings = _test_settings(admin_ids=[400])
    update, _ = _fake_callback_update(user_id=400, data=f"reject:{group.id}")
    ctx = _fake_context(bot=bot, settings=settings)

    await on_reject_callback(update, ctx)

    await session.refresh(group)
    assert group.status == "rejected"
