"""A4: a null `message` in an LLM response must not raise TypeError.

Real failure mode
-----------------
`_extract_content()` indexed `raw["message"]["content"]` (ollama) and
`choices[0]["message"]["content"]` (openai) after only guarding for a
*missing* key. A payload that carries the key with an explicit JSON `null`
value --

    {"message": null, "done": false}
    {"choices": [{"message": null}]}

-- is a TypeError (`'NoneType' object is not subscriptable`), which is NOT
a KeyError, so the surrounding `except KeyError` guard did not catch it.
The failure escaped `_extract_content` and out of the hot path every LLM
response in the pipeline goes through, aborting the run instead of
triggering the retry ladder that already handles every other parse
failure.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import _extract_content, request_completion

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

_NULL_MESSAGE_OPENAI = {"choices": [{"message": None}]}
_NULL_MESSAGE_OLLAMA = {"message": None, "done": False}


class TestExtractContentNullMessage:

    @pytest.mark.parametrize("api_format,raw", [
        ("ollama", _NULL_MESSAGE_OLLAMA),
        ("openai", _NULL_MESSAGE_OPENAI),
    ])
    def test_null_message_raises_valueerror(self, api_format, raw):
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format)
        # The point of the guard: not a raw TypeError leaking out.
        assert not isinstance(excinfo.value, TypeError)

    @pytest.mark.parametrize("api_format,raw", [
        ("ollama", _NULL_MESSAGE_OLLAMA),
        ("openai", _NULL_MESSAGE_OPENAI),
    ])
    def test_null_message_is_not_typeerror(self, api_format, raw):
        with pytest.raises(ValueError):
            _extract_content(raw, api_format)

    def test_null_choice_object_raises_valueerror(self):
        with pytest.raises(ValueError):
            _extract_content({"choices": [None]}, "openai")

    def test_missing_message_key_still_raises_valueerror(self):
        """The pre-existing guard must keep working -- don't regress it."""
        with pytest.raises(ValueError):
            _extract_content({"done": False}, "ollama")
        with pytest.raises(ValueError):
            _extract_content({"choices": [{}]}, "openai")

    def test_null_content_still_degrades_to_empty(self):
        """Null content was already handled -- it must still be."""
        assert _extract_content(
            {"message": {"content": None}}, "ollama"
        ) == ""
        assert _extract_content(
            {"choices": [{"message": {"content": None}}]}, "openai"
        ) == ""

    def test_well_formed_responses_unaffected(self):
        assert _extract_content(
            {"message": {"role": "assistant", "content": "hi"}}, "ollama"
        ) == "hi"
        assert _extract_content(
            {"choices": [{"message": {"content": "hi"}}]}, "openai"
        ) == "hi"


# ─────────────────────────────────────────────────────────────────────────────
# End to end: the call must die with a clean, catchable parse error rather
# than a raw TypeError leaking out of the hot path.
# ─────────────────────────────────────────────────────────────────────────────

class _OnceHandler(http.server.BaseHTTPRequestHandler):
    body = _NULL_MESSAGE_OPENAI
    request_count = 0

    def do_POST(self):
        type(self).request_count += 1
        payload = json.dumps(type(self).body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve_once(body):
    handler_cls = type("H", (_OnceHandler,), {"body": body, "request_count": 0})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port, handler_cls


def _call(port, **kwargs):
    return request_completion(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        {"Content-Type": "application/json"},
        {"model": "x", "messages": []},
        timeout=5, stream=False, api_format="openai",
        **kwargs,
    )


def test_null_message_surfaces_clean_valueerror_not_typeerror():
    """The exact regression: a raw TypeError must not escape the hot path."""
    server, port, handler_cls = _serve_once(_NULL_MESSAGE_OPENAI)
    try:
        with pytest.raises(ValueError) as excinfo:
            _call(port, error_retries=0)
    finally:
        server.shutdown()
    assert not isinstance(excinfo.value, TypeError)
    assert handler_cls.request_count == 1


def test_null_message_names_the_raw_shape():
    """The diagnostic must point at the payload, like every sibling guard."""
    server, port, _ = _serve_once(_NULL_MESSAGE_OPENAI)
    try:
        with pytest.raises(ValueError) as excinfo:
            _call(port, error_retries=0)
    finally:
        server.shutdown()
    assert "message" in str(excinfo.value)


def test_null_message_does_not_burn_the_retry_budget():
    """A deterministic payload-shape failure must fail fast -- the transport
    retry ladder is for transient errors, and the default budget is
    error_retries=60 x 10s. This mirrors how every other ValueError raised by
    _extract_content already behaves."""
    server, port, handler_cls = _serve_once(_NULL_MESSAGE_OPENAI)
    sleep = MagicMock()
    try:
        with pytest.raises(ValueError):
            _call(port, error_retries=60, _sleep_fn=sleep)
    finally:
        server.shutdown()
    assert handler_cls.request_count == 1
    sleep.assert_not_called()


def test_null_message_ollama_also_clean():
    server, port, _ = _serve_once(_NULL_MESSAGE_OLLAMA)
    try:
        with pytest.raises(ValueError) as excinfo:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="ollama",
                error_retries=0,
            )
    finally:
        server.shutdown()
    assert not isinstance(excinfo.value, TypeError)
