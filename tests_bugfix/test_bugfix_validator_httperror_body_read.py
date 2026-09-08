"""tests_bugfix/test_bugfix_validator_httperror_body_read.py

ValidatorAgent.validate() has two error handlers for the LLM call:

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")   # 203-209
        ...
        return _err
    except Exception as e:
        ...
        return _err                                          # 210-214

Sibling handlers do not nest: an exception raised INSIDE the HTTPError handler
is not offered to the sibling `except Exception` below it — it propagates out
of the whole method.

`e.read()` is that in-handler call. It is a second, unguarded network read on
the same socket, so the same transport hiccup that produced the HTTPError can
raise again while the error body is being drained (connection reset mid-body,
an OS-level error on a closed fd). That raw OSError / UnicodeDecodeError then
escaped validate() entirely, and a transport problem in the ERROR path produced
an unhandled traceback instead of the validator verdict every other failure
already returns. The error path was strictly less robust than the success path:
the sibling handler exists precisely to turn a broken call into
{"status": "needs_fix", "_api_error": True}, but it could never see this case.

Fix under test: wrap the body read in its own try, fall back to a placeholder
body, and let the existing HTTPError verdict stand.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.validator_agent import ValidatorAgent

PAYLOAD = {
    "task": "add a helper",
    "iteration": 1,
    "target_block": "def f(): pass",
    "imports": "",
    "related_code": {},
    "missing_refs": [],
}


def _http_error(
    read_side_effect=None,
    body_bytes=b'{"error": "context overflow"}',
) -> urllib.error.HTTPError:
    """An HTTPError whose body read fails the way a dying socket does.

    The response body is a file-like object; giving it a MagicMock whose
    read() raises keeps the traceback honest — this is `e.read()` really
    raising, not a hand-built exception. With no side effect the body reads
    like a real server response, for the sanity tests below.
    """
    resp = MagicMock()
    if read_side_effect is None:
        resp.read.return_value = body_bytes
    else:
        resp.read.side_effect = read_side_effect
    return urllib.error.HTTPError(
        url="http://127.0.0.1:1337/v1/chat/completions",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=resp,
    )


def _agent(**kwargs) -> ValidatorAgent:
    return ValidatorAgent(base_url="http://127.0.0.1:1337/v1", timeout=5, **kwargs)


class TestBodyReadFailureStillYieldsAVerdict:
    def test_oserror_from_body_read_returns_needs_fix(self):
        """THE BUG: an OSError raised by e.read() inside the HTTPError handler
        used to escape validate() — the sibling `except Exception` cannot catch
        it, since sibling handlers do not nest."""
        agent = _agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(OSError("Connection reset by peer")),
        ):
            result = agent.validate(PAYLOAD)

        assert isinstance(result, dict), (
            "a body-read failure must return the same verdict dict every other "
            "API failure returns, not propagate a raw OSError"
        )
        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True
        assert "503" in result["feedback"]

    def test_unicode_error_from_body_read_returns_needs_fix(self):
        """A decode failure of the error body is the same class of in-handler
        exception."""
        agent = _agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")),
        ):
            result = agent.validate(PAYLOAD)

        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True

    def test_failure_survives_the_full_call(self):
        """The verdict must be the method's return value — the caller
        (main.py's run_pipeline) distinguishes _api_error from a needs_fix
        round, so nothing may escape as an exception."""
        agent = _agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(OSError("closed fd")),
        ):
            result = agent.validate(PAYLOAD)

        assert set(result) >= {"status", "feedback", "_api_error"}
        assert "_unparseable" not in result, (
            "a transport failure is an API error, not a malformed reply"
        )


class TestNormalHttpErrorUnaffected:
    """Sanity: the HTTPError path must keep reporting the real body."""

    def test_http_error_reports_the_actual_body(self):
        agent = _agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(None),
        ):
            result = agent.validate(PAYLOAD)

        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True
        assert "503" in result["feedback"]
        assert "context overflow" in result["feedback"].lower()

    def test_generic_exception_path_unchanged(self):
        agent = _agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=TimeoutError("timed out"),
        ):
            result = agent.validate(PAYLOAD)

        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True
        assert "timed out" in result["feedback"]

    def test_stream_path_body_read_failure_too(self):
        """The HTTPError handler is shared by the stream and non-stream call
        sites, so the guard must cover both."""
        agent = _agent(stream=True)
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_http_error(OSError("reset")),
        ):
            result = agent.validate(PAYLOAD)

        assert result["status"] == "needs_fix"
        assert result["_api_error"] is True
