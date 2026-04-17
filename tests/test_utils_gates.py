"""Tests for ``check_approval_gate`` and ``check_min_players_gate``."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from models import Group
from utils import gates as gates_module
from utils.gates import check_approval_gate, check_min_players_gate


def _settings(min_players: int = 2) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    os.environ["MIN_PLAYERS"] = str(min_players)
    return Settings()  # ty: ignore[missing-argument]


def _fake_update(chat_id: int = 123) -> tuple[MagicMock, AsyncMock]:
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_chat = chat
    update.effective_message = message
    return update, message.reply_text


async def test_approval_gate_returns_none_when_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    update, reply = _fake_update()
    session = MagicMock()
    monkeypatch.setattr(gates_module, "get_group_by_telegram_id", AsyncMock(return_value=None))

    result = await check_approval_gate(update, session, settings)

    assert result is None
    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)


async def test_approval_gate_returns_none_when_group_not_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    update, reply = _fake_update()
    session = MagicMock()
    group = Group(id=1, telegram_id=123, approved=False)
    monkeypatch.setattr(gates_module, "get_group_by_telegram_id", AsyncMock(return_value=group))

    result = await check_approval_gate(update, session, settings)

    assert result is None
    reply.assert_awaited_once_with(settings.NON_APPROVED_GROUP_MESSAGE)


async def test_approval_gate_returns_group_when_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    update, reply = _fake_update()
    session = MagicMock()
    group = Group(id=7, telegram_id=123, approved=True)
    monkeypatch.setattr(gates_module, "get_group_by_telegram_id", AsyncMock(return_value=group))

    result = await check_approval_gate(update, session, settings)

    assert result is group
    reply.assert_not_called()


async def test_min_players_gate_false_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(min_players=3)
    update, reply = _fake_update()
    session = MagicMock()
    group = Group(id=7, telegram_id=123, approved=True)
    monkeypatch.setattr(gates_module, "count_players", AsyncMock(return_value=1))

    result = await check_min_players_gate(update, session, group, settings)

    assert result is False
    reply.assert_awaited_once()
    expected = settings.NOT_ENOUGH_PLAYERS_MESSAGE.format_map({"min_players": 3})
    assert reply.await_args.args[0] == expected


async def test_min_players_gate_true_at_or_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(min_players=2)
    update, reply = _fake_update()
    session = MagicMock()
    group = Group(id=7, telegram_id=123, approved=True)
    monkeypatch.setattr(gates_module, "count_players", AsyncMock(return_value=5))

    result = await check_min_players_gate(update, session, group, settings)

    assert result is True
    reply.assert_not_called()
