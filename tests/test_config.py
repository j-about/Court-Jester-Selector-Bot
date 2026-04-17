"""Tests for ``Settings`` loading, validation, and URL construction."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import Settings

_BASE_ENV = {
    "TG_BOT_TOKEN": "test-token",
    "POSTGRES_USER": "u",
    "POSTGRES_PASSWORD": "p",
    "POSTGRES_DB": "db",
}


def _settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **{**_BASE_ENV, **overrides})  # type: ignore[arg-type]


def test_loads_with_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.TG_BOT_TOKEN == "test-token"
    assert s.async_database_url.startswith("postgresql+asyncpg://")
    assert s.sync_database_url.startswith("postgresql+psycopg://")
    assert s.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS == ["creator", "administrator"]
    assert s.TG_BOT_ADMIN_RIGHTS_USER_IDS == []
    assert s.DEFAULT_WEIGHT == 3


def test_csv_list_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS", "creator,administrator")
    monkeypatch.setenv("TG_BOT_ADMIN_RIGHTS_USER_IDS", "12345678,87654321")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS == ["creator", "administrator"]
    assert s.TG_BOT_ADMIN_RIGHTS_USER_IDS == [12345678, 87654321]


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    for k, v in _BASE_ENV.items():
        if k != "TG_BOT_TOKEN":
            monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("missing", ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"])
def test_missing_db_component_raises(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    for k, v in _BASE_ENV.items():
        if k == missing or k == "TG_BOT_TOKEN":
            continue
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_weight_bounds_min_gt_max() -> None:
    with pytest.raises(ValidationError):
        _settings(MIN_WEIGHT="5", MAX_WEIGHT="1")


def test_default_weight_outside_bounds() -> None:
    with pytest.raises(ValidationError):
        _settings(MIN_WEIGHT="1", MAX_WEIGHT="3", DEFAULT_WEIGHT="5")


def test_min_players_too_low() -> None:
    with pytest.raises(ValidationError):
        _settings(MIN_PLAYERS="1")


def test_leaderboard_rank_missing_placeholder() -> None:
    with pytest.raises(ValidationError):
        _settings(LEADERBOARD_RANK_MESSAGE="{rank}. only")


def test_leaderboard_rank_without_draw_count_is_allowed() -> None:
    s = _settings(LEADERBOARD_RANK_MESSAGE="{rank}. {username}")
    assert s.LEADERBOARD_RANK_MESSAGE == "{rank}. {username}"


def test_not_enough_players_without_placeholder_is_allowed() -> None:
    s = _settings(NOT_ENOUGH_PLAYERS_MESSAGE="Need more players.")
    assert s.NOT_ENOUGH_PLAYERS_MESSAGE == "Need more players."


def test_personal_stats_missing_draw_count_raises() -> None:
    with pytest.raises(ValidationError):
        _settings(PERSONAL_STATS_MESSAGE="hello {username}")


def test_invalid_admin_status() -> None:
    with pytest.raises(ValidationError):
        _settings(TG_BOT_ADMIN_RIGHTS_CHAT_MEMBER_STATUS="creator,root")


def test_invalid_command_name() -> None:
    with pytest.raises(ValidationError):
        _settings(PICK_PLAYER_COMMAND="Invalid Command")


def test_negative_admin_user_id() -> None:
    with pytest.raises(ValidationError):
        _settings(TG_BOT_ADMIN_RIGHTS_USER_IDS="-1,2")


def test_url_construction_uses_components() -> None:
    s = _settings(
        POSTGRES_HOST="myhost",
        POSTGRES_PORT="6543",
        POSTGRES_USER="alice",
        POSTGRES_PASSWORD="s/p@",
        POSTGRES_DB="court",
    )
    assert s.async_database_url == "postgresql+asyncpg://alice:s%2Fp%40@myhost:6543/court"
    assert s.sync_database_url == "postgresql+psycopg://alice:s%2Fp%40@myhost:6543/court"
