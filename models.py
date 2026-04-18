"""SQLModel table definitions for the bot's persistent state."""
from datetime import date
from typing import Any

from sqlalchemy import BigInteger, Column, Index, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, Relationship, SQLModel

from config import Settings

__all__ = ["Draw", "Group", "Player"]

_settings = Settings()  # ty: ignore[missing-argument]


class Group(SQLModel, table=True):
    """A Telegram group in which the bot operates.

    ``telegram_id`` is unique across the table. ``approved`` gates every
    interactive command in the group. ``approval_messages`` stores the
    per-admin ``(chat_id, message_id)`` pairs of any pending approval
    prompts plus, once a decision has been made, the acting admin and
    timestamp.
    """

    __tablename__ = "group"
    __table_args__ = (
        Index("ix_group_telegram_id", "telegram_id"),
        Index("ix_group_telegram_title", "telegram_title"),
    )

    id: int | None = Field(default=None, primary_key=True)
    status: str
    telegram_id: int = Field(
        sa_column=Column(BigInteger, unique=True, nullable=False)
    )
    telegram_title: str
    approved: bool = Field(default=False)
    approval_messages: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    players: list["Player"] = Relationship(back_populates="group")
    draws: list["Draw"] = Relationship(back_populates="group")


class Player(SQLModel, table=True):
    """A Telegram user observed in a specific group.

    ``(group_id, telegram_id)`` is unique. ``status`` mirrors the user's
    current Telegram chat-member status. ``weight`` drives the daily
    weighted draw and is constrained to the configured
    ``[MIN_WEIGHT, MAX_WEIGHT]`` range.
    """

    __tablename__ = "player"
    __table_args__ = (
        UniqueConstraint("group_id", "telegram_id", name="uix_group_telegram_id"),
        Index("ix_player_telegram_id", "telegram_id"),
        Index("ix_player_telegram_first_name", "telegram_first_name"),
        Index("ix_player_telegram_username", "telegram_username"),
    )

    id: int | None = Field(default=None, primary_key=True)
    status: str
    group_id: int = Field(foreign_key="group.id")
    telegram_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    telegram_first_name: str
    telegram_last_name: str | None = None
    telegram_username: str | None = None
    weight: int = Field(
        default=_settings.DEFAULT_WEIGHT,
        ge=_settings.MIN_WEIGHT,
        le=_settings.MAX_WEIGHT,
    )

    group: Group = Relationship(back_populates="players")
    draws: list["Draw"] = Relationship(back_populates="player")


class Draw(SQLModel, table=True):
    """A single daily draw result for one group.

    The ``(group_id, draw_date)`` unique constraint enforces idempotency:
    one draw per group per calendar day.
    """

    __tablename__ = "draw"
    __table_args__ = (
        UniqueConstraint("group_id", "draw_date", name="uix_group_date"),
    )

    id: int | None = Field(default=None, primary_key=True)
    draw_date: date
    group_id: int = Field(foreign_key="group.id")
    player_id: int = Field(foreign_key="player.id")

    group: Group = Relationship(back_populates="draws")
    player: Player = Relationship(back_populates="draws")
