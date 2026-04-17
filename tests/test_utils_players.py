"""Tests for display-name formatting helpers in ``utils.players``."""
from __future__ import annotations

from unittest.mock import MagicMock

from models import Player
from utils.players import format_display_name, player_display_name, user_display_name


def _player(
    *,
    username: str | None = None,
    first_name: str = "First",
    last_name: str | None = None,
) -> Player:
    return Player(
        status="member",
        group_id=1,
        telegram_id=10,
        telegram_first_name=first_name,
        telegram_last_name=last_name,
        telegram_username=username,
        weight=3,
    )


def _user(
    *,
    user_id: int = 999,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    return user


def test_format_prefers_username() -> None:
    assert format_display_name("alice", "Alice", "Doe") == "@alice"


def test_format_joins_first_and_last() -> None:
    assert format_display_name(None, "Alice", "Doe") == "Alice Doe"


def test_format_first_only_when_no_last() -> None:
    assert format_display_name(None, "Alice", None) == "Alice"


def test_format_last_only_when_no_first() -> None:
    assert format_display_name(None, None, "Doe") == "Doe"


def test_format_empty_when_all_none() -> None:
    assert format_display_name(None, None, None) == ""


def test_format_empty_string_username_falls_through() -> None:
    assert format_display_name("", "Alice", None) == "Alice"


def test_format_empty_first_name_drops_gap() -> None:
    assert format_display_name(None, "", "Doe") == "Doe"


def test_player_display_name_uses_username() -> None:
    assert player_display_name(_player(username="alice", first_name="Alice", last_name="Doe")) == "@alice"


def test_player_display_name_joins_first_and_last() -> None:
    assert player_display_name(_player(first_name="Alice", last_name="Doe")) == "Alice Doe"


def test_player_display_name_first_only() -> None:
    assert player_display_name(_player(first_name="Alice")) == "Alice"


def test_user_display_name_prefers_username() -> None:
    assert user_display_name(_user(username="alice", first_name="Alice", last_name="Doe")) == "@alice"


def test_user_display_name_joins_first_and_last() -> None:
    assert user_display_name(_user(first_name="Alice", last_name="Doe")) == "Alice Doe"


def test_user_display_name_falls_back_to_id() -> None:
    assert user_display_name(_user(user_id=12345)) == "12345"
