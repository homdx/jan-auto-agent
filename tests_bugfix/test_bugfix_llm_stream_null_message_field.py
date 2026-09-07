"""tests_bugfix/test_bugfix_llm_stream_null_message_field.py

``_extract_content`` (llm_stream.py:259) guards three malformed-reply cases:
``content: null`` (the BUGFIX at the top of the docstring), an empty ``choices``
list, and a *missing* ``message`` key (converted from KeyError to a
descriptive ValueError).

The one case it does not guard is ``message: null`` — present, but not an
object:

    ollama : {"message": null, "done": true}
    openai : {"choices": [{"message": null}]}

``raw["message"]["content"]`` then raises ``TypeError: 'NoneType' object is
not subscriptable``. KeyError IS caught and converted, so a missing key gets a
descriptive ValueError and lands in every caller's retry ladder
(validator_agent.py's AUTO-BUG pattern, gate1_filter's closed-candidate
path). TypeError is not KeyError, so it escapes the guard entirely and aborts
the run instead of taking that ladder — even though the payload is just as
malformed as the missing-key case.

This is the hot path for every non-streaming LLM response in the pipeline.

Fix under test: check isinstance(raw.get("message"), dict) before indexing and
route the failure into the same ValueError the missing-key case already raises.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import _extract_content, request_completion

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


class TestNullMessageFieldRaisesValueError:
    def test_ollama_null_message_is_value_error_not_type_error(self):
        """THE BUG: message: null must produce the same ValueError a missing
        key already produces, so the caller's retry ladder sees it."""
        with pytest.raises(ValueError) as exc_info:
            _extract_content({"message": None, "done": True}, api_format="ollama")
        assert "message" in str(exc_info.value)

    def test_openai_null_message_is_value_error_not_type_error(self):
        with pytest.raises(ValueError) as exc_info:
            _extract_content(
                {"choices": [{"message": None}]}, api_format="openai"
            )
        assert "message" in str(exc_info.value)

    def test_ollama_null_message_is_not_a_type_error(self):
        with pytest.raises(Exception) as exc_info:
            _extract_content({"message": None, "done": True}, api_format="ollama")
        assert not isinstance(exc_info.value, TypeError), (
            "a null message must be reported as a malformed response, not as a "
            "TypeError from indexing None"
        )

    def test_openai_null_message_is_not_a_type_error(self):
        with pytest.raises(Exception) as exc_info:
            _extract_content({"choices": [{"message": None}]}, api_format="openai")
        assert not isinstance(exc_info.value, TypeError)


class TestExistingGuardsUnaffected:
    """The three cases _extract_content already handled must not regress."""

    def test_ollama_null_content_still_returns_empty(self):
        assert _extract_content(
            {"message": {"role": "assistant", "content": None}}, "ollama"
        ) == ""

    def test_openai_null_content_still_returns_empty(self):
        assert _extract_content(
            {"choices": [{"message": {"content": None}}]}, "openai"
        ) == ""

    def test_ollama_missing_message_still_raises_value_error(self):
        with pytest.raises(ValueError, match="missing expected key"):
            _extract_content({"done": True}, api_format="ollama")

    def test_openai_missing_message_still_raises_value_error(self):
        with pytest.raises(ValueError, match="missing expected key"):
            _extract_content({"choices": [{"role": "assistant"}]}, api_format="openai")

    def test_openai_empty_choices_still_raises_value_error(self):
        with pytest.raises(ValueError, match="no choices"):
            _extract_content({"choices": []}, api_format="openai")

    def test_ollama_normal_content_still_works(self):
        assert _extract_content(
            {"message": {"role": "assistant", "content": " hello "}}, "ollama"
        ) == "hello"

    def test_openai_normal_content_still_works(self):
        assert _extract_content(
            {"choices": [{"message": {"content": " world "}}]}, "openai"
        ) == "world"


# ── Full-path check: the ValueError must survive the real request ───────────


class _NonStreamHandler(http.server.BaseHTTPRequestHandler):
    body: dict = {}

    def do_POST(self):
        payload = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve_once(body):
    handler_cls = type("Handler", (_NonStreamHandler,), {"body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


class TestNullMessageThroughRequestCompletion:
    """The whole point of the fix is what the caller sees: a ValueError that
    the existing retry/verdict ladders already handle, not a raw TypeError
    that aborts the run."""

    def test_ollama_format_null_message_surfaces_value_error(self):
        server, port = _serve_once({"message": None, "done": True})
        try:
            with pytest.raises(ValueError):
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="ollama",
                    error_retries=0,
                )
        finally:
            server.shutdown()

    def test_openai_format_null_message_surfaces_value_error(self):
        server, port = _serve_once({"choices": [{"message": None}]})
        try:
            with pytest.raises(ValueError):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=0,
                )
        finally:
            server.shutdown()
