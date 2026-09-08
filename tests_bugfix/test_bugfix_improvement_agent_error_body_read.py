"""A5 (review fix): a failing e.read() inside ImprovementAgent's HTTPError
handler must not escape process().

Real failure mode
-----------------
`ImprovementAgent.process()` catches `urllib.error.HTTPError` and reads the
error body with `e.read()`. If `e.read()` itself raises (connection reset
while the body was being sent, body already consumed, socket closed), that
exception propagates straight out of the `except HTTPError` block -- and
the sibling `except Exception` below CANNOT catch it, because sibling
handlers do not nest. So the error path was less robust than the success
path: a transport hiccup while reading an error body produced an unhandled
traceback instead of the result dict main.py's improvement pass expects.

The identical defect was fixed at the named site in validator_agent.py,
but the same module family carried the same crash class here with no test
anywhere for it.
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

from tools.improvement_agent import ImprovementAgent


def _context():
    return {
        "target_block": "def f():\n    pass\n",
        "imports": [],
        "related_code": [],
        "context_lines": [],
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


def _agent():
    return ImprovementAgent(
        model="test-model",
        base_url="http://localhost:99999/v1",
        api_key="test",
        timeout=5,
    )


class TestErrorBodyReadFailure:

    @pytest.mark.parametrize("read_exc", [
        OSError("connection reset by peer while reading error body"),
        io.UnsupportedOperation("stream is read-only"),
        ConnectionError("socket closed"),
    ])
    def test_broken_error_body_read_returns_a_result_dict(self, read_exc):
        """The read failure must be swallowed into the normal error shape,
        not raised out of process()."""
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_http_error(b'{"error": "boom"}', read_side_effect=read_exc),
        ):
            result = _agent().process("improve", _context())

        assert isinstance(result, dict)
        assert result["improved_code"] == ""
        assert result["issues"] == []
        assert "HTTP 500" in result["explanation"]

    def test_broken_error_body_read_names_the_read_failure(self):
        """The fallback body must say why, not silently claim an empty body."""
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_http_error(b"ignored", read_side_effect=OSError("boom reading body")),
        ):
            result = _agent().process("improve", _context())
        assert "boom reading body" in result["explanation"]

    def test_broken_error_body_read_does_not_trigger_the_generic_handler(self):
        """The generic `except Exception` branch reports a transport failure
        ("Request to the LLM failed"); the HTTPError branch reports the
        status. If the read failure leaked into the sibling handler the
        operator would see the wrong diagnosis for an HTTP-level failure."""
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_http_error(b"ignored", read_side_effect=OSError("boom reading body")),
        ):
            result = _agent().process("improve", _context())
        assert "Request to the LLM failed" not in result["explanation"]
        assert "HTTP 500 from API" in result["explanation"]


class TestExistingBehaviourUnaffected:

    def test_readable_error_body_still_reports_its_content(self):
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_http_error(b'{"error":{"code":"free_quota_rpm"}}'),
        ):
            result = _agent().process("improve", _context())
        assert "free_quota_rpm" in result["explanation"]
        assert "HTTP 500" in result["explanation"]

    def test_generic_exception_still_returns_the_transport_error_shape(self):
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=TimeoutError("timed out"),
        ):
            result = _agent().process("improve", _context())
        assert result["improved_code"] == ""
        assert "Request to the LLM failed" in result["explanation"]

    def test_successful_response_is_parsed(self):
        reply = json.dumps({"issues": [], "improved_code": "def f(): pass", "changes": []})
        with patch(
            "tools.improvement_agent.request_completion",
            return_value=reply,
        ):
            result = _agent().process("improve", _context())
        assert result["improved_code"] == "def f(): pass"
        assert "explanation" not in result

    def test_non_dict_reply_degrades_to_the_malformed_shape(self):
        with patch(
            "tools.improvement_agent.request_completion",
            return_value="[]",
        ):
            result = _agent().process("improve", _context())
        assert result["improved_code"] == ""
        assert "could not be parsed as JSON" in result["explanation"]
