"""Fault-tolerant message-template formatting."""
from __future__ import annotations

from typing import Any

from observability import log_template_error

__all__ = ["FALLBACK_MESSAGE", "safe_format"]

FALLBACK_MESSAGE = "An internal error occurred."


def safe_format(template: str, context: dict[str, Any], template_name: str) -> str:
    """Substitute ``context`` into ``template`` via ``str.format_map``.

    On ``KeyError`` from a missing placeholder, logs a template error
    tagged with ``template_name`` and returns ``FALLBACK_MESSAGE``.
    """
    try:
        return template.format_map(context)
    except KeyError as exc:
        missing_key = str(exc).strip("'")
        log_template_error(
            template_name=template_name,
            expected_tokens=(missing_key,),
            provided_context=context,
        )
        return FALLBACK_MESSAGE
