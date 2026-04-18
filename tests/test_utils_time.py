"""Tests for ``utils.time.current_draw_date``.

Rather than pulling in a freezegun-style dependency, each test patches
``utils.time.datetime`` with a stub whose ``now(tz)`` returns a fixed
aware ``datetime`` — enough to exercise rollover behaviour at arbitrary
instants while keeping the helper itself trivially thin.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from config import Settings
from utils import time as utils_time


def _settings(draw_timezone: str) -> Settings:
    os.environ.setdefault("TG_BOT_TOKEN", "test-token")
    os.environ.setdefault("POSTGRES_USER", "u")
    os.environ.setdefault("POSTGRES_PASSWORD", "p")
    os.environ.setdefault("POSTGRES_DB", "db")
    return Settings(_env_file=None, DRAW_TIMEZONE=draw_timezone)  # type: ignore[call-arg]


class _FrozenDatetime:
    """Stand-in for ``datetime`` that answers ``.now(tz)`` with a fixed instant."""

    def __init__(self, fixed_utc: datetime) -> None:
        assert fixed_utc.tzinfo is not None
        self._fixed_utc = fixed_utc

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        if tz is None:
            return self._fixed_utc
        return self._fixed_utc.astimezone(tz)


def _freeze(monkeypatch: pytest.MonkeyPatch, fixed_utc: datetime) -> None:
    monkeypatch.setattr(utils_time, "datetime", _FrozenDatetime(fixed_utc))


def test_rolls_over_in_configured_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # 22:59 UTC on the 18th is already 00:59 Paris on the 19th (UTC+2 DST).
    _freeze(monkeypatch, datetime(2026, 4, 18, 22, 59, 30, tzinfo=ZoneInfo("UTC")))

    assert utils_time.current_draw_date(_settings("Europe/Paris")) == date(2026, 4, 19)
    assert utils_time.current_draw_date(_settings("UTC")) == date(2026, 4, 18)


def test_before_midnight_stays_on_previous_day(monkeypatch: pytest.MonkeyPatch) -> None:
    # 21:58 Paris on the 18th (UTC+2 DST) == 19:58 UTC on the 18th.
    _freeze(monkeypatch, datetime(2026, 4, 18, 19, 58, 0, tzinfo=ZoneInfo("UTC")))

    assert utils_time.current_draw_date(_settings("Europe/Paris")) == date(2026, 4, 18)


def test_dst_spring_forward_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    # America/New_York skips 02:00 -> 03:00 on 2026-03-08; either side
    # must still resolve to the correct civil date.
    _freeze(monkeypatch, datetime(2026, 3, 8, 6, 30, 0, tzinfo=ZoneInfo("UTC")))
    assert utils_time.current_draw_date(_settings("America/New_York")) == date(2026, 3, 8)

    _freeze(monkeypatch, datetime(2026, 3, 8, 7, 30, 0, tzinfo=ZoneInfo("UTC")))
    assert utils_time.current_draw_date(_settings("America/New_York")) == date(2026, 3, 8)
