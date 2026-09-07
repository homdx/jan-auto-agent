"""A5: an HTTPError whose body can't be read must still yield a verdict.

ValidatorAgent.validate() catches urllib.error.HTTPError to turn an HTTP
failure into ``{"status": "needs_fix", ..., "_api_error": True}`` — the
_api_error sentinel prompt_evaluator uses to exclude the call from
scoring, and main.py uses to decide whether to retry.

But the handler immediately called ``e.read()`` with no guard of its own.
``e.read()`` can itself raise: the underlying transport can die while
downloading the error body (a socket reset after the status line, a
truncated body, a closed connection). That exception propagated RAW out of
the ``except urllib.error.HTTPError`` block — sibling handlers do not nest,
so the ``except Exception`` one two lines below could NOT catch it, even
though it would have been the perfect place to handle this.

Net effect: the error path was less robust than the success path. A
transport hiccup during error-body reading produced an unhandled traceback
instead of a validator verdict, aborting the validation pass.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.validator_agent as va
from tools.validator_agent import ValidatorAgent

_PAYLOAD = {
    "task": "add a function",
    "iteration": 1,
    "target_block": "def f(): pass",
    "imports": [],
    "related_code": [],
    "missing_refs": [],
}


class _ExplodingBody:
    """A response body whose read() raises, as a dying socket does."""

    def __init__(self, exc):
        self._exc = exc

    def read(self, *a):
        raise self._exc

    def close(self):
        pass


def _httperror_with_bad_body(code=500, exc=None):
    """A real urllib.error.HTTPError whose body read raises *exc*."""
    return urllib.error.HTTPError(
        url="http://127.0.0.1:1/v1/chat/completions",
        code=code,
        msg="Internal Server Error",
        hdrs={},
        fp=_ExplodingBody(exc if exc is not None else OSError("connection reset")),
    )


def _raise(err):
    """A stand-in for request_completion that raises *err*."""
    def _fn(*a, **k):
        raise err
    return _fn


def _make_agent():
    return ValidatorAgent(
        max_iter=3, base_url="http://127.0.0.1:1/v1",
        api_key="jan", timeout=5, stream=False, api_format="openai",
    )


class TestHttpErrorBodyReadFailure:
    @pytest.mark.parametrize("exc", [
        OSError("connection reset by peer"),
        ConnectionError("socket closed"),
        TimeoutError("timed out reading body"),
        ValueError("incomplete read"),
    ])
    def test_returns_verdict_instead_of_raising(self, monkeypatch, exc):
        monkeypatch.setattr(va, "request_completion", _raise(_httperror_with_bad_body(500, exc)))
        result = _make_agent().validate(_PAYLOAD)

        assert isinstance(result, dict)
        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True

    def test_does_not_leak_the_transport_exception(self, monkeypatch):
        """The transport failure is a detail of reading the error body — the
        verdict must report the HTTP status, not the socket error."""
        monkeypatch.setattr(va, "request_completion", _raise(_httperror_with_bad_body(503)))
        result = _make_agent().validate(_PAYLOAD)
        assert "503" in result["feedback"]

    def test_feedback_still_names_the_api_error(self, monkeypatch):
        monkeypatch.setattr(va, "request_completion", _raise(_httperror_with_bad_body(429)))
        result = _make_agent().validate(_PAYLOAD)
        assert "HTTP 429" in result["feedback"]
        assert result["_api_error"] is True

    def test_falls_back_to_a_placeholder_body(self, monkeypatch):
        """Without a body the feedback must still say something honest rather
        than leaving an empty string or raising."""
        monkeypatch.setattr(va, "request_completion", _raise(_httperror_with_bad_body(500)))
        result = _make_agent().validate(_PAYLOAD)
        assert result["feedback"].strip()
        assert "unreadable" in result["feedback"].lower()

    def test_streaming_path_is_guarded_too(self, monkeypatch):
        monkeypatch.setattr(va, "request_completion", _raise(_httperror_with_bad_body(500)))
        agent = ValidatorAgent(
            max_iter=3, base_url="http://127.0.0.1:1/v1", api_key="jan",
            timeout=5, stream=True, api_format="ollama",
        )
        result = agent.validate(_PAYLOAD)
        assert result["_api_error"] is True


class TestNormalHttpErrorStillReportsBody:
    def test_readable_body_is_reported(self, monkeypatch):
        """The pre-existing behaviour: a readable error body lands in the
        feedback so the operator sees what the gateway said."""
        class _Body:
            def read(self, *a):
                return b'{"error": "free_quota_rpm"}'

            def close(self):
                pass

        err = urllib.error.HTTPError(
            url="http://127.0.0.1:1/v1/chat/completions", code=429,
            msg="Too Many Requests", hdrs={}, fp=_Body(),
        )
        monkeypatch.setattr(va, "request_completion", _raise(err))
        result = _make_agent().validate(_PAYLOAD)
        assert "free_quota_rpm" in result["feedback"]
        assert result["_api_error"] is True

    def test_plain_exception_still_returns_verdict(self, monkeypatch):
        def boom(*a, **k):
            raise TimeoutError("dial timed out")

        monkeypatch.setattr(va, "request_completion", boom)
        result = _make_agent().validate(_PAYLOAD)
        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True
