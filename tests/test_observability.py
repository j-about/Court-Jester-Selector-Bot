"""Tests for Sentry configuration, token scrubbing, and audit-log helpers."""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import observability
from config import Settings


@pytest.fixture
def settings_with_dsn() -> Settings:
    return Settings(  # ty: ignore[missing-argument]
        TG_BOT_TOKEN="123456:ABC-DEF_very_secret_token",
        POSTGRES_USER="u",
        POSTGRES_PASSWORD="p",
        POSTGRES_DB="test",
        SENTRY_DSN="https://public@example.ingest.sentry.io/1",
    )


@pytest.fixture
def settings_no_dsn() -> Settings:
    return Settings(  # ty: ignore[missing-argument]
        TG_BOT_TOKEN="123456:ABC-DEF_very_secret_token",
        POSTGRES_USER="u",
        POSTGRES_PASSWORD="p",
        POSTGRES_DB="test",
    )


def test_configure_sentry_passes_dsn_and_integrations(settings_with_dsn: Settings) -> None:
    with patch("observability.sentry_sdk.init") as init:
        observability.configure_sentry(settings_with_dsn)

    init.assert_called_once()
    kwargs = init.call_args.kwargs
    assert kwargs["dsn"] == settings_with_dsn.SENTRY_DSN
    assert kwargs["send_default_pii"] is False
    integration_types = {type(i).__name__ for i in kwargs["integrations"]}
    assert {"LoggingIntegration", "SqlalchemyIntegration", "AsyncPGIntegration"} <= integration_types
    assert callable(kwargs["before_send"])
    assert callable(kwargs["before_breadcrumb"])


def test_configure_sentry_inert_when_dsn_missing(settings_no_dsn: Settings) -> None:
    with patch("observability.sentry_sdk.init") as init:
        observability.configure_sentry(settings_no_dsn)

    assert init.call_args.kwargs["dsn"] is None


def test_before_send_scrubs_bot_token_from_nested_strings(settings_with_dsn: Settings) -> None:
    with patch("observability.sentry_sdk.init") as init:
        observability.configure_sentry(settings_with_dsn)

    before_send = init.call_args.kwargs["before_send"]
    token = settings_with_dsn.TG_BOT_TOKEN
    event = {
        "message": f"oops token={token}",
        "extra": {"url": f"https://api.telegram.org/bot{token}/send"},
        "tags": [f"t:{token}"],
        "nested": ({"inner": token},),
    }

    scrubbed = before_send(event, {})

    assert token not in scrubbed["message"]
    assert token not in scrubbed["extra"]["url"]
    assert token not in scrubbed["tags"][0]
    assert token not in scrubbed["nested"][0]["inner"]


def test_before_breadcrumb_scrubs_bot_token(settings_with_dsn: Settings) -> None:
    with patch("observability.sentry_sdk.init") as init:
        observability.configure_sentry(settings_with_dsn)

    before_breadcrumb = init.call_args.kwargs["before_breadcrumb"]
    token = settings_with_dsn.TG_BOT_TOKEN
    crumb = {"message": f"sending to {token}", "data": {"token": token}}

    scrubbed = before_breadcrumb(crumb, {})

    assert token not in scrubbed["message"]
    assert token not in scrubbed["data"]["token"]


def test_log_audit_event_emits_structured_info_record(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cjsb"):
        observability.log_audit_event(
            "something.happened",
            group_telegram_id=1,
            player_telegram_id=2,
            admin_telegram_id=3,
            extra_field="value",
        )

    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert rec.message == "something.happened"
    assert rec.group_telegram_id == 1
    assert rec.player_telegram_id == 2
    assert rec.admin_telegram_id == 3
    assert rec.extra_field == "value"


def test_log_group_approval(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cjsb"):
        observability.log_group_approval(admin_id=10, group_id=20, approved=True)

    rec = caplog.records[-1]
    assert rec.message == "group.approval"
    assert rec.admin_telegram_id == 10
    assert rec.group_telegram_id == 20
    assert rec.player_telegram_id is None
    assert rec.approved is True


def test_log_weight_change(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cjsb"):
        observability.log_weight_change(admin_id=10, player_id=20, old_weight=3, new_weight=5)

    rec = caplog.records[-1]
    assert rec.message == "player.weight_change"
    assert rec.admin_telegram_id == 10
    assert rec.player_telegram_id == 20
    assert rec.group_telegram_id is None
    assert rec.old_weight == 3
    assert rec.new_weight == 5


def test_log_draw_execution(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cjsb"):
        observability.log_draw_execution(group_id=100, player_id=200, draw_date=date(2026, 4, 15))

    rec = caplog.records[-1]
    assert rec.message == "draw.execution"
    assert rec.group_telegram_id == 100
    assert rec.player_telegram_id == 200
    assert rec.admin_telegram_id is None
    assert rec.draw_date == "2026-04-15"


def test_log_bot_membership_change(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cjsb"):
        observability.log_bot_membership_change(group_id=42, added=False)

    rec = caplog.records[-1]
    assert rec.message == "bot.membership_change"
    assert rec.group_telegram_id == 42
    assert rec.player_telegram_id is None
    assert rec.admin_telegram_id is None
    assert rec.added is False


def test_log_template_error_emits_error_record(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="cjsb"):
        observability.log_template_error(
            template_name="greeting",
            expected_tokens=("username", "rank"),
            provided_context={"username": "alice"},
        )

    rec = caplog.records[-1]
    assert rec.levelno == logging.ERROR
    assert rec.message == "template.substitution_error"
    assert rec.template_name == "greeting"
    assert rec.expected_tokens == ["username", "rank"]
    assert rec.provided_context == {"username": "alice"}


def test_log_error_sets_scope_extras_and_tag_and_emits_record(caplog: pytest.LogCaptureFixture) -> None:
    err = ValueError("boom")

    with patch("observability.sentry_sdk.new_scope") as new_scope:
        scope = MagicMock()
        new_scope.return_value.__enter__.return_value = scope
        with caplog.at_level(logging.ERROR, logger="cjsb"):
            observability.log_error("op.failed", err, operation="test_op", group_id=7)

    scope.set_extra.assert_any_call("operation", "test_op")
    scope.set_extra.assert_any_call("group_id", 7)
    scope.set_tag.assert_called_with("event", "op.failed")

    rec = caplog.records[-1]
    assert rec.levelno == logging.ERROR
    assert rec.message == "op.failed"
    assert rec.exc_info is not None
    assert rec.exc_info[1] is err


def test_flush_sentry_delegates() -> None:
    with patch("observability.sentry_sdk.flush") as flush:
        observability.flush_sentry(timeout=3.0)

    flush.assert_called_once_with(timeout=3.0)
