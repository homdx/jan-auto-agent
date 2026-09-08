"""A5: an exception while reading an HTTPError body must not escape.

Real failure mode
-----------------
`ValidatorAgent.validate()` catches `urllib.error.HTTPError` and reads the
error body with `e.read()`. If `e.read()` itself raises (connection reset
while the body was being sent, body already consumed, socket closed), that
exception propagates straight out of the `except HTTPError` block -- and
the sibling `except Exception` two lines below CANNOT catch it, because
sibling handlers do not nest. The error path was therefore less robust than
the success path: a transport hiccup while reading an error body produced
an unhandled traceback instead of the `_api_error` verdict the rest of the
pipeline (main.py's run_pipeline retry/backoff, prompt_evaluator's
_api_error exclusion) already expects.
"""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.validator_agent import ValidatorAgent


def _payload():
    return {
        "task": "make it work",
        "iteration": 1,
        "target_block": "def f():\n    pass\n",
        "imports": [],
        "related_code": [],
        "missing_refs": [],
    }


def _http_error(body: bytes, code: int = 500, read_side_effect=None):
    """Build a real urllib.error.HTTPError the way urlopen() raises it."""
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    err = urllib.error.HTTPError(
        "http://127.0.0.1:9/chat/completions", code, "server error",
        headers, io.BytesIO(body),
    )
    if read_side_effect is not None:
        err.read = MagicMock(side_effect=read_side_effect)
    return err


class TestErrorBodyReadFailure:

    @pytest.mark.parametrize("read_exc", [
        OSError("connection reset by peer while reading error body"),
        io.UnsupportedOperation("stream is read-only"),
        ConnectionError("socket closed"),
    ])
    def test_broken_error_body_read_returns_api_error_verdict(self, read_exc):
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(b'{"error": "boom"}', read_side_effect=read_exc),
        ):
            result = ValidatorAgent().validate(_payload())

        assert result["_api_error"] is True
        assert result["status"] == "needs_fix"
        assert "HTTP 500" in result["feedback"]


    def test_broken_error_body_read_names_the_read_failure(self):
        """The fallback body must say why, not silently claim an empty body."""
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(b"ignored", read_side_effect=OSError("boom reading body")),
        ):
            result = ValidatorAgent().validate(_payload())
        assert "boom reading body" in result["feedback"]


class TestExistingBehaviourUnaffected:

    def test_readable_error_body_still_reports_its_content(self):
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(b'{"error":{"code":"free_quota_rpm"}}'),
        ):
            result = ValidatorAgent().validate(_payload())
        assert result["_api_error"] is True
        assert "free_quota_rpm" in result["feedback"]
        assert "HTTP 500" in result["feedback"]

    def test_generic_exception_still_returns_api_error(self):
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=TimeoutError("timed out"),
        ):
            result = ValidatorAgent().validate(_payload())
        assert result["_api_error"] is True
        assert result["status"] == "needs_fix"

    def test_successful_response_is_parsed(self):
        reply = json.dumps({"status": "approved", "feedback": "ok"})
        with patch(
            "tools.validator_agent.request_completion",
            return_value=reply,
        ):
            result = ValidatorAgent().validate(_payload())
        assert result["status"] == "approved"
        assert "_api_error" not in result
