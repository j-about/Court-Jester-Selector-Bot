"""Structured logging and Sentry integration.

Configures Sentry with an event and breadcrumb scrubber that redacts the
Telegram bot token, and exposes a small set of audit-log helpers that emit
semantically named events to the ``cjsb`` logger.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.asyncpg import AsyncPGIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from config import Settings

__all__ = [
    "configure_sentry",
    "flush_sentry",
    "log_audit_event",
    "log_bot_membership_change",
    "log_draw_execution",
    "log_error",
    "log_group_approval",
    "log_template_error",
    "log_weight_change",
]

logger = logging.getLogger("cjsb")

_SCRUBBED = "[scrubbed:tg_bot_token]"


def _make_scrubber(bot_token: str) -> tuple[Callable[[Any, Any], Any], Callable[[Any, Any], Any]]:
    """Return Sentry ``before_send`` / ``before_breadcrumb`` callbacks.

    The returned callbacks recursively replace occurrences of ``bot_token``
    inside event and breadcrumb payloads with a fixed placeholder. When
    ``bot_token`` is empty both callbacks are identity functions.
    """
    if not bot_token:

        def _identity(obj: Any, _hint: Any) -> Any:
            return obj

        return _identity, _identity

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(bot_token, _SCRUBBED)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, tuple):
            return tuple(scrub(v) for v in value)
        return value

    def before_send(event: Any, _hint: Any) -> Any:
        return scrub(event)

    def before_breadcrumb(crumb: Any, _hint: Any) -> Any:
        return scrub(crumb)

    return before_send, before_breadcrumb


def configure_sentry(settings: Settings) -> None:
    """Initialize Sentry with logging, SQLAlchemy, and AsyncPG integrations."""
    before_send, before_breadcrumb = _make_scrubber(settings.TG_BOT_TOKEN)
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        send_default_pii=False,
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            SqlalchemyIntegration(),
            AsyncPGIntegration(),
        ],
        before_send=before_send,
        before_breadcrumb=before_breadcrumb,
    )


def flush_sentry(timeout: float = 2.0) -> None:
    """Flush any buffered Sentry events, waiting up to ``timeout`` seconds."""
    sentry_sdk.flush(timeout=timeout)


def log_error(event: str, error: Exception, **context: Any) -> None:
    """Log an exception to the bot logger with ``event`` and contextual extras."""
    with sentry_sdk.new_scope() as scope:
        for k, v in context.items():
            scope.set_extra(k, v)
        scope.set_tag("event", event)
        logger.error(event, exc_info=error, extra=context)


def log_audit_event(
    event: str,
    *,
    group_telegram_id: int | None = None,
    player_telegram_id: int | None = None,
    admin_telegram_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Emit an INFO audit log entry with the standard identity fields."""
    logger.info(
        event,
        extra={
            "group_telegram_id": group_telegram_id,
            "player_telegram_id": player_telegram_id,
            "admin_telegram_id": admin_telegram_id,
            **kwargs,
        },
    )


def log_group_approval(admin_id: int, group_id: int, approved: bool) -> None:
    """Record an admin's approve or reject decision for a group."""
    log_audit_event(
        "group.approval",
        admin_telegram_id=admin_id,
        group_telegram_id=group_id,
        approved=approved,
    )


def log_weight_change(
    admin_id: int,
    player_id: int,
    old_weight: int,
    new_weight: int,
) -> None:
    """Record a player weight change performed by an admin."""
    log_audit_event(
        "player.weight_change",
        admin_telegram_id=admin_id,
        player_telegram_id=player_id,
        old_weight=old_weight,
        new_weight=new_weight,
    )


def log_draw_execution(
    group_id: int, player_id: int, draw_date: date, draw_timezone: str
) -> None:
    """Record the outcome of a daily draw.

    Captures the configured ``draw_timezone`` and the UTC instant of the
    decision alongside the civil ``draw_date`` so that rollover boundaries
    remain traceable in audit logs regardless of the deployer's locale.
    """
    log_audit_event(
        "draw.execution",
        group_telegram_id=group_id,
        player_telegram_id=player_id,
        draw_date=draw_date.isoformat(),
        draw_timezone=draw_timezone,
        decided_at=datetime.now(UTC).isoformat(),
    )


def log_bot_membership_change(group_id: int, added: bool) -> None:
    """Record the bot joining or leaving a group."""
    log_audit_event(
        "bot.membership_change",
        group_telegram_id=group_id,
        added=added,
    )


def log_template_error(
    template_name: str,
    expected_tokens: list[str] | tuple[str, ...],
    provided_context: dict[str, Any],
) -> None:
    """Record a message-template substitution failure with the missing tokens."""
    logger.error(
        "template.substitution_error",
        extra={
            "template_name": template_name,
            "expected_tokens": list(expected_tokens),
            "provided_context": provided_context,
        },
    )
