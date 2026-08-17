"""tests/test_llm_stream_reasoning_field_fallback.py — AUTO-REASONING-1.

Field report: a real run against Gemini's openai-compat endpoint
(generativelanguage.googleapis.com/v1beta/openai/chat/completions) failed
every single call with:

    HTTP 400 ... "Invalid JSON payload received. Unknown name \"reasoning\":
    Cannot find field."

`build_chat_request`'s think=False path sends an OpenRouter-style
``reasoning: {"effort": "low", "exclude": true}`` field, documented as
"expected to be silently ignored by a provider that doesn't recognise it"
— true for OpenRouter/kenari-style aggregators, false for a strict
OpenAI-schema validator like Gemini's. 400 is not in `_is_retryable_status`,
so this failed every call closed with no recovery.

This covers the fix: on exactly this 400 shape, `request_completion` strips
the `reasoning` key and retries ONCE, immediately, without touching
`error_retries`/backoff — this is a payload-shape fix, not a transient
error.
"""

from __future__ import annotations

import http.server
import json
import threading
from unittest.mock import MagicMock

import pytest

from tools.llm_stream import request_completion


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Serves a scripted sequence of responses and records each request's
    JSON body, so tests can assert on what was actually sent (not just
    what came back)."""
    script: list = []
    request_count = 0
    received_bodies: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            type(self).received_bodies.append(json.loads(raw.decode("utf-8")))
        except Exception:
            type(self).received_bodies.append(None)

        idx = min(type(self).request_count, len(type(self).script) - 1)
        type(self).request_count += 1
        entry = type(self).script[idx]
        if entry[0] == "error":
            _, code, body, extra_headers = entry
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            payload = body.encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            _, body = entry
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve_script(script):
    handler_cls = type("Handler", (_RecordingHandler,),
                        {"script": script, "request_count": 0, "received_bodies": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, handler_cls


_OK_BODY = {"choices": [{"message": {"content": '{"ok": true}'}}]}

_GEMINI_400_BODY = json.dumps({
    "error": {
        "code": 400,
        "message": 'Invalid JSON payload received. Unknown name "reasoning": Cannot find field.',
        "status": "INVALID_ARGUMENT",
    }
})


class TestReasoningFieldFallback:

    def test_gemini_400_strips_reasoning_and_retries_once(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "gemini-x", "messages": [],
                 "reasoning": {"effort": "low", "exclude": True}},
                timeout=5, stream=False, api_format="openai",
                _sleep_fn=sleep,
            )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        # First request had the field; retried request must not.
        assert "reasoning" in handler_cls.received_bodies[0]
        assert "reasoning" not in handler_cls.received_bodies[1]
        # This is a payload-shape fix, not a rate limit — no sleep, no
        # error_retries budget spent.
        sleep.assert_not_called()

    def test_fallback_does_not_consume_error_retries_budget(self):
        """error_retries=0 (fail-fast opt-out) must still allow this ONE
        payload-shape retry — it's orthogonal to the rate-limit retry
        budget, not a use of it."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),
            ("ok", _OK_BODY),
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "gemini-x", "messages": [],
                 "reasoning": {"effort": "low", "exclude": True}},
                timeout=5, stream=False, api_format="openai",
                error_retries=0,
            )
        finally:
            server.shutdown()
        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2

    def test_only_retries_once_second_400_raises(self):
        """If the provider keeps rejecting even without 'reasoning', this
        must not loop forever — the strip only ever happens once."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),
            ("error", 400, "still broken, unrelated reason", {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "gemini-x", "messages": [],
                     "reasoning": {"effort": "low", "exclude": True}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 2

    def test_400_without_reasoning_field_in_payload_is_not_touched(self):
        """A 400 that mentions 'reasoning' in its error text but whose
        OUTGOING payload never had a 'reasoning' key must not loop or
        misbehave — nothing to strip, so it raises normally like any
        other non-retryable 400."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "gemini-x", "messages": []},  # no reasoning key
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1

    def test_unrelated_400_is_not_treated_as_reasoning_rejection(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, '{"error":{"message":"bad request: missing model"}}', {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "gemini-x", "messages": [],
                     "reasoning": {"effort": "low", "exclude": True}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1

    def test_on_retry_callback_fires_for_the_reasoning_fallback(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),
            ("ok", _OK_BODY),
        ])
        messages = []
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1beta/openai/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "gemini-x", "messages": [],
                 "reasoning": {"effort": "low", "exclude": True}},
                timeout=5, stream=False, api_format="openai",
                on_retry=messages.append,
            )
        finally:
            server.shutdown()
        assert len(messages) == 1
        assert "reasoning" in messages[0]
