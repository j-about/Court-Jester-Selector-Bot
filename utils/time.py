"""Timezone-aware clock helpers.

``current_draw_date`` is the single source of truth for the calendar day
used as the draw's idempotency key. Reading it through the configured
``DRAW_TIMEZONE`` is what lets a deployer place the daily rollover at
their players' local midnight rather than the process's UTC wall clock.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import Settings

__all__ = ["current_draw_date"]


def current_draw_date(settings: Settings) -> date:
    """Return the calendar date in the deployer's configured timezone."""
    return datetime.now(ZoneInfo(settings.DRAW_TIMEZONE)).date()
