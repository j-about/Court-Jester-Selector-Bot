"""Display-name formatting for Telegram users and ``Player`` rows."""
from __future__ import annotations

from telegram import User

from models import Player

__all__ = ["format_display_name", "player_display_name", "user_display_name"]


def format_display_name(
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Return the preferred display name for a user.

    Returns ``"@{username}"`` when ``username`` is truthy, otherwise
    returns the space-joined first and last name (either of which may be
    missing).
    """
    if username:
        return f"@{username}"
    parts = [p for p in (first_name, last_name) if p]
    return " ".join(parts)


def player_display_name(player: Player) -> str:
    """Return the display name for a ``Player`` row."""
    return format_display_name(
        player.telegram_username,
        player.telegram_first_name,
        player.telegram_last_name,
    )


def user_display_name(user: User) -> str:
    """Return the display name for a Telegram ``User``, falling back to the numeric id."""
    return format_display_name(user.username, user.first_name, user.last_name) or str(user.id)
