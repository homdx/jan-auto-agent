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

from tools.llm_stream import request_completion, build_chat_request
import tools.llm_stream as llm_stream_mod


@pytest.fixture(autouse=True)
def _reset_reasoning_cache():
    """AUTO-REASONING-2's unsupported-URL memory is process-lifetime global
    state — reset it before/after every test so tests don't leak into each
    other regardless of run order."""
    llm_stream_mod._REASONING_UNSUPPORTED_URLS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_URLS.clear()


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


class TestReasoningUnsupportedProcessCache:
    """AUTO-REASONING-2: after the FIRST 400 for a given endpoint, every
    subsequent build_chat_request() call to that same URL must skip the
    `reasoning` field outright — no repeat round trip for a call the
    process already knows will fail."""

    def test_build_chat_request_skips_field_after_marked_unsupported(self):
        url = "https://example.test/v1/chat/completions"
        _, _, payload_before = build_chat_request(
            base_url="https://example.test/v1", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "reasoning" in payload_before

        llm_stream_mod.mark_reasoning_field_unsupported(url)

        _, _, payload_after = build_chat_request(
            base_url="https://example.test/v1", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "reasoning" not in payload_after

    def test_end_to_end_second_call_never_sends_reasoning_after_first_400(self):
        """Full integration: build_chat_request() -> request_completion()
        twice in a row against the same endpoint. First call pays the one
        retry; second call's OUTGOING payload never has 'reasoning' at
        all — build_chat_request() itself omits it, no 400 involved."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _GEMINI_400_BODY, {}),  # call 1, attempt 1: rejected
            ("ok", _OK_BODY),                        # call 1, attempt 2: succeeds
            ("ok", _OK_BODY),                        # call 2: succeeds first try
        ])
        try:
            base_url = f"http://127.0.0.1:{port}/v1beta/openai"
            url1, headers1, payload1 = build_chat_request(
                base_url=base_url, api_key="k", model="gemini-x",
                api_format="openai", temperature=0.1, max_tokens=100,
                system="s", user_msg="u", think=False,
            )
            assert "reasoning" in payload1
            request_completion(url1, headers1, payload1, timeout=5,
                                stream=False, api_format="openai")

            # Second call: build_chat_request must now omit the field.
            url2, headers2, payload2 = build_chat_request(
                base_url=base_url, api_key="k", model="gemini-x",
                api_format="openai", temperature=0.1, max_tokens=100,
                system="s", user_msg="u", think=False,
            )
            assert "reasoning" not in payload2
            request_completion(url2, headers2, payload2, timeout=5,
                                stream=False, api_format="openai")
        finally:
            server.shutdown()

        assert handler_cls.request_count == 3  # 2 (call1) + 1 (call2)
        assert "reasoning" not in handler_cls.received_bodies[2]

    def test_reasoning_field_is_supported_true_for_untried_url(self):
        assert llm_stream_mod.reasoning_field_is_supported(
            "https://never-seen.example/v1/chat/completions") is True

    def test_marking_one_url_does_not_affect_another(self):
        llm_stream_mod.mark_reasoning_field_unsupported("https://a.example/v1/chat/completions")
        assert llm_stream_mod.reasoning_field_is_supported(
            "https://a.example/v1/chat/completions") is False
        assert llm_stream_mod.reasoning_field_is_supported(
            "https://b.example/v1/chat/completions") is True

    def test_think_true_never_sends_reasoning_regardless_of_cache_state(self):
        """think=True never sent the field in the first place — the cache
        only ever affects the think=False path."""
        _, _, payload = build_chat_request(
            base_url="https://example.test/v1", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=True,
        )
        assert "reasoning" not in payload

    def test_ollama_format_unaffected_by_the_cache(self):
        """The cache/field only exist on the openai branch; ollama's own
        top-level 'think' field is a completely different mechanism."""
        _, _, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="k", model="m",
            api_format="ollama", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "reasoning" not in payload
        assert payload["think"] is False
