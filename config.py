"""Application settings loaded from environment variables.

Exposes the ``Settings`` class, a ``pydantic-settings`` model that reads
secrets and operational parameters from the process environment (and the
``.env`` file, when present), validates them, and derives helpers such as
the async and sync SQLAlchemy database URLs.
"""
from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["Settings"]

_TELEGRAM_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ALLOWED_ADMIN_STATUSES = {"creator", "administrator"}


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _must_contain(template: str, field_name: str, tokens: tuple[str, ...]) -> str:
    missing = [t for t in tokens if t not in template]
    if missing:
        raise ValueError(f"{field_name} must contain placeholder(s): {', '.join(missing)}")
    return template


class Settings(BaseSettings):
    """Runtime configuration for the bot.

    Values are read from the process environment and from an optional
    ``.env`` file. Extra variables are ignored. All message templates,
    command names, admin rosters, database connection parameters, weight
    bounds, and pagination sizes flow through this model.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    TG_BOT_TOKEN: str = Field(min_length=1)

    SENTRY_DSN: str | None = None

    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = Field(min_length=1)
    POSTGRES_PASSWORD: SecretStr = Field(min_length=1)
    POSTGRES_DB: str = Field(min_length=1)

    TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["creator", "administrator"]
    )
    TG_BOT_ADMIN_RIGHTS_USER_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)

    MIN_WEIGHT: int = 1
    MAX_WEIGHT: int = 5
    DEFAULT_WEIGHT: int = 3

    GROUPS_PER_PAGE: int = Field(default=5, ge=1)
    PLAYERS_PER_PAGE: int = Field(default=5, ge=1)
    MIN_PLAYERS: int = 10

    NON_APPROVED_GROUP_MESSAGE: str = (
        "🏰 Halt! This royal entertainment has not yet been sanctioned! "
        "The Court Jester Selector awaits approval from the kingdom's nobles "
        "before the foolery can commence."
    )
    NOT_ENOUGH_PLAYERS_MESSAGE: str = (
        "⚜️ Insufficient subjects detected in the realm! The Court requires a minimum of "
        "{min_players} participants before any royal proceedings or records can be accessed. "
        "Expand thy circle of jesters!"
    )

    PICK_PLAYER_COMMAND: str = "crown_the_jester"
    PICK_PLAYER_COMMAND_DESCRIPTION: str = "Crown today's jester."
    PICK_PLAYER_PICKED_PLAYER_MESSAGE: str = (
        "🎪 By royal decree, {username} is hereby appointed as today's Royal Entertainer! "
        "The throne awaits your foolery! 🎭"
    )

    SHOW_LEADERBOARD_COMMAND: str = "court_leaderboard"
    SHOW_LEADERBOARD_COMMAND_DESCRIPTION: str = "View the court rankings."
    LEADERBOARD_NOT_ENOUGH_PICKED_PLAYERS_MESSAGE: str = (
        "📜 The royal court cannot establish a hierarchy of fools yet! "
        "More jesters must be selected before we can rank their foolery."
    )
    LEADERBOARD_INTRO_MESSAGE: str = (
        "🏆 Behold the Royal Jester Rankings! From the most frequently summoned fools to the rarely seen tricksters:"
    )
    LEADERBOARD_RANK_MESSAGE: str = "{rank}. {username} - {draw_count}"
    LEADERBOARD_OUTRO_MESSAGE: str = (
        "These are the top jesters of our noble court! May the odds forever favor the truly entertaining! 👑"
    )

    SHOW_PERSONAL_STATS_COMMAND: str = "my_jester_stats"
    SHOW_PERSONAL_STATS_COMMAND_DESCRIPTION: str = "Check your jester stats."
    PERSONAL_STATS_NO_PICKED_PLAYER_MESSAGE: str = (
        "🎭 Hark, {username}! The jester's hat has never graced thy noble head. "
        "You remain untouched by the royal selection. A blessing or a curse? Only time will tell!"
    )
    PERSONAL_STATS_MESSAGE: str = (
        "🃏 Hear this, {username}! You have entertained the court {draw_count} times as Jester, "
        "placing you at position {rank} among all court entertainers!"
    )

    @field_validator("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", mode="before")
    @classmethod
    def _parse_status_list(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("TG_BOT_ADMIN_RIGHTS_USER_IDS", mode="before")
    @classmethod
    def _parse_user_id_list(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS")
    @classmethod
    def _validate_statuses(cls, v: list[str]) -> list[str]:
        invalid = [s for s in v if s not in _ALLOWED_ADMIN_STATUSES]
        if invalid:
            raise ValueError(
                f"TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS contains invalid values: {invalid}. "
                f"Allowed: {sorted(_ALLOWED_ADMIN_STATUSES)}"
            )
        return v

    @field_validator("TG_BOT_ADMIN_RIGHTS_USER_IDS")
    @classmethod
    def _validate_user_ids(cls, v: list[int]) -> list[int]:
        if any(uid <= 0 for uid in v):
            raise ValueError("TG_BOT_ADMIN_RIGHTS_USER_IDS must contain positive integers")
        return v

    @field_validator(
        "PICK_PLAYER_COMMAND",
        "SHOW_LEADERBOARD_COMMAND",
        "SHOW_PERSONAL_STATS_COMMAND",
    )
    @classmethod
    def _validate_command_name(cls, v: str) -> str:
        if not _TELEGRAM_COMMAND_RE.match(v):
            raise ValueError(f"'{v}' is not a valid Telegram command name (must match ^[a-z][a-z0-9_]{{0,31}}$)")
        return v

    @field_validator(
        "NON_APPROVED_GROUP_MESSAGE",
        "PICK_PLAYER_COMMAND_DESCRIPTION",
        "SHOW_LEADERBOARD_COMMAND_DESCRIPTION",
        "LEADERBOARD_NOT_ENOUGH_PICKED_PLAYERS_MESSAGE",
        "SHOW_PERSONAL_STATS_COMMAND_DESCRIPTION",
    )
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("PICK_PLAYER_PICKED_PLAYER_MESSAGE")
    @classmethod
    def _check_username_placeholder(cls, v: str, info: Any) -> str:
        return _must_contain(v, info.field_name, ("{username}",))

    @field_validator("LEADERBOARD_RANK_MESSAGE")
    @classmethod
    def _check_leaderboard_rank(cls, v: str) -> str:
        return _must_contain(v, "LEADERBOARD_RANK_MESSAGE", ("{rank}", "{username}"))

    @field_validator("PERSONAL_STATS_MESSAGE")
    @classmethod
    def _check_personal_stats(cls, v: str) -> str:
        return _must_contain(v, "PERSONAL_STATS_MESSAGE", ("{draw_count}",))

    def _build_url(self, driver: str) -> str:
        user = quote(self.POSTGRES_USER, safe="")
        password = quote(self.POSTGRES_PASSWORD.get_secret_value(), safe="")
        return f"{driver}://{user}:{password}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """SQLAlchemy URL for the async ``asyncpg`` driver."""
        return self._build_url("postgresql+asyncpg")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """SQLAlchemy URL for the synchronous ``psycopg`` driver."""
        return self._build_url("postgresql+psycopg")

    @model_validator(mode="after")
    def _validate_weight_bounds(self) -> Settings:
        if self.MIN_WEIGHT < 0:
            raise ValueError("MIN_WEIGHT must be >= 0")
        if self.MAX_WEIGHT < self.MIN_WEIGHT:
            raise ValueError("MAX_WEIGHT must be >= MIN_WEIGHT")
        if not (self.MIN_WEIGHT <= self.DEFAULT_WEIGHT <= self.MAX_WEIGHT):
            raise ValueError("DEFAULT_WEIGHT must be within [MIN_WEIGHT, MAX_WEIGHT]")
        if self.MIN_PLAYERS < 2:
            raise ValueError("MIN_PLAYERS must be >= 2")
        return self
