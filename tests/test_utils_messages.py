"""Tests for ``utils.messages.safe_format``."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from utils import messages as messages_module
from utils.messages import FALLBACK_MESSAGE, safe_format


def test_safe_format_returns_substituted_string() -> None:
    result = safe_format("Hello {name}", {"name": "World"}, "GREETING")
    assert result == "Hello World"


def test_safe_format_returns_fallback_on_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    log_mock = MagicMock()
    monkeypatch.setattr(messages_module, "log_template_error", log_mock)

    result = safe_format("Hello {name}", {"other": "x"}, "GREETING")

    assert result == FALLBACK_MESSAGE
    log_mock.assert_called_once()
    kwargs = log_mock.call_args.kwargs
    assert kwargs["template_name"] == "GREETING"
    assert kwargs["expected_tokens"] == ("name",)
    assert kwargs["provided_context"] == {"other": "x"}


def test_safe_format_handles_template_without_placeholders() -> None:
    assert safe_format("no placeholders", {}, "STATIC") == "no placeholders"
