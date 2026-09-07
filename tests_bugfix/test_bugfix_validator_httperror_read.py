"""tests_bugfix/test_bugfix_validator_httperror_read.py — A5: e.read() inside
the HTTPError handler must not propagate raw.

Bug: ``ValidatorAgent.validate`` wraps the LLM call in
``except urllib.error.HTTPError as e: body = e.read()...``.  An exception raised
*inside* that handler — from ``e.read()`` itself (a transport hiccup while
draining the error body: socket timeout, connection reset mid-read) —
propagates raw; the sibling ``except Exception`` on the next line cannot catch
it, because sibling except clauses do not nest.  So a transport hiccup during
error-body reading produces an unhandled traceback instead of a validator
verdict.

Fix: wrap the ``e.read()`` call in its own try, falling back to a placeholder
body, so the HTTPError handler always produces a verdict.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.validator_agent import ValidatorAgent


def _make_agent():
    """A ValidatorAgent that needs no network — only validate's except branches."""
    va = ValidatorAgent.__new__(ValidatorAgent)
    va.stream = False
    va.timeout = 1
    va.api_format = "openai"
    va.ssl_context = None
    va.model = "stub"
    va.max_iter = 1
    va.temperature = 0.1
    va.max_hints = 3
    va.prompt_store = None
    va.num_ctx = 0
    va.base_url = "http://localhost:1337/v1"
    va.api_key = "jan"
    return va


def _http_error_with_body(code: int, body: bytes):
    """Build an HTTPError whose .read() returns *body*."""
    err = urllib.error.HTTPError(
        f"http://x/{code}", code, "Server Error", {}, None,
    )
    err.read = lambda *a, **k: body  # type: ignore[assignment]
    return err


class TestHTTPErrorReadWrapped:
    def test_httperror_read_failure_still_returns_verdict(self, monkeypatch):
        """If e.read() raises, the handler must not propagate — it must return
        a verdict the same way a normal HTTPError does."""
        va = _make_agent()

        class _BrokenReadError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 500, "Server Error", {}, None)

            def read(self, *a, **k):
                raise OSError("connection reset while reading error body")

        def _raise_http_error(*a, **k):
            raise _BrokenReadError()

        monkeypatch.setattr(
            "tools.validator_agent.request_completion", _raise_http_error,
        )

        # The sibling except Exception must not be reached; the verdict must
        # come back as a dict with _api_error, not a raw traceback.
        result = va.validate({"iteration": 1})
        assert isinstance(result, dict)
        assert result.get("_api_error") is True
        assert result["status"] == "needs_fix"
        assert "500" in result["feedback"]

    def test_normal_httperror_still_reports_body(self, monkeypatch):
        """The happy path of the handler must keep working."""
        va = _make_agent()

        def _raise_http_error(*a, **k):
            raise _http_error_with_body(503, b"backend overloaded")

        monkeypatch.setattr(
            "tools.validator_agent.request_completion", _raise_http_error,
        )

        result = va.validate({"iteration": 1})
        assert result["_api_error"] is True
        assert "backend overloaded" in result["feedback"]