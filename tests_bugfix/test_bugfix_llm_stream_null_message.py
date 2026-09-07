"""A4: a payload whose `message` is JSON `null` must not escape as TypeError.

`_extract_content` already handled a *missing* `message` key (raising a
descriptive ValueError) and a *null* `content` field, but the index
`raw["message"]["content"]` assumes the value it just looked up is a dict.
A gateway that returns HTTP 200 with `"message": null` (or
`"choices": [{"message": null}]`) produced

    TypeError: 'NoneType' object is not subscriptable

which is NOT a KeyError, so it escaped the existing guard entirely. That is
the hot path for every LLM response in the pipeline, so one malformed
payload from a provider aborted the run instead of surfacing a distinguishable
parse failure.

The fix keeps the established convention for this function: raise a
descriptive ValueError (the same class the missing-key and empty-choices
cases already raise). Like those, it surfaces immediately rather than
being retried - the response arrived and was unusable, so retrying the
same request would just repeat the same payload for
error_retries x error_retry_wait_sec before raising the same error
(see TestUnusableButWellFormedJsonIsNotRetried in
test_bugfix_llm_stream_nonjson_body.py).
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestExtractContentNullMessage:
    def test_ollama_null_message_raises_valueerror_not_typeerror(self):
        with pytest.raises(ValueError, match="message") as excinfo:
            _extract_content(
                {"message": None, "done": True}, api_format="ollama",
            )
        assert not isinstance(excinfo.value, TypeError)

    def test_openai_null_message_raises_valueerror_not_typeerror(self):
        with pytest.raises(ValueError, match="message") as excinfo:
            _extract_content(
                {"choices": [{"message": None}]}, api_format="openai",
            )
        assert not isinstance(excinfo.value, TypeError)

    def test_ollama_null_message_reports_raw_shape(self):
        """The message should let an operator see what actually arrived."""
        with pytest.raises(ValueError) as excinfo:
            _extract_content({"message": None, "done": True}, api_format="ollama")
        msg = str(excinfo.value)
        assert "None" in msg or "null" in msg or "NoneType" in msg or "dict" in msg

    def test_missing_message_key_still_raises_valueerror(self):
        """The pre-existing guard must keep working."""
        with pytest.raises(ValueError, match="missing expected key"):
            _extract_content({"done": True}, api_format="ollama")

    def test_empty_choices_still_raises_valueerror(self):
        with pytest.raises(ValueError, match="no choices"):
            _extract_content({"choices": []}, api_format="openai")

    def test_valid_payloads_unaffected(self):
        assert _extract_content(
            {"message": {"role": "assistant", "content": "hi"}},
            api_format="ollama",
        ) == "hi"
        assert _extract_content(
            {"choices": [{"message": {"content": "yo"}}]}, api_format="openai",
        ) == "yo"

    def test_null_content_still_degrades_to_empty(self):
        """content: null (with a real message dict) is the documented empty
        reply, not a malformed payload."""
        assert _extract_content(
            {"message": {"content": None}}, api_format="ollama",
        ) == ""
        assert _extract_content(
            {"choices": [{"message": {"content": None}}]}, api_format="openai",
        ) == ""


class TestNullMessageEndToEnd:
    def test_request_completion_raises_valueerror_not_typeerror(self):
        """HTTP 200 + `"message": null` must surface as the same descriptive
        ValueError class as the other parse failures — not as a raw
        TypeError from inside the hot path."""
        server, port = _serve_once({"message": None, "done": True})
        try:
            with pytest.raises(ValueError) as excinfo:
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="ollama",
                    error_retries=0, error_retry_wait_sec=0,
                )
        finally:
            server.shutdown()
        assert not isinstance(excinfo.value, TypeError)
        assert "message" in str(excinfo.value)

    def test_openai_format_end_to_end(self):
        server, port = _serve_once({"choices": [{"message": None}]})
        try:
            with pytest.raises(ValueError) as excinfo:
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=0, error_retry_wait_sec=0,
                )
        finally:
            server.shutdown()
        assert not isinstance(excinfo.value, TypeError)
        assert "message" in str(excinfo.value)

    def test_good_response_still_returns_text(self):
        server, port = _serve_once(
            {"message": {"role": "assistant", "content": "fine"}, "done": True}
        )
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="ollama",
                error_retries=0, error_retry_wait_sec=0,
            )
        finally:
            server.shutdown()
        assert result == "fine"
