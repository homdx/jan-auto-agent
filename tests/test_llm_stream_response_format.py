"""tests/test_llm_stream_response_format.py — AUTO-JSONMODE-1.

Covers the global `[api] response_format = true` switch:

  1. build_chat_request() sends the engine-level JSON-mode field
     (OpenAI `response_format: {"type": "json_object"}` /
     Ollama `format: "json"`) only when the flag is True.
  2. Nothing changes when the flag is False (today's best-effort
     parsing/salvage behaviour is untouched).
  3. If the endpoint rejects the field with HTTP 400, request_completion
     strips it and retries ONCE, immediately (payload-shape fix, not a
     transient error — mirrors AUTO-REASONING-1), logs a warning, and
     remembers the endpoint (AUTO-JSONMODE-1) so every subsequent
     build_chat_request() call against that same URL stops sending the
     field for the rest of the process.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from unittest.mock import MagicMock

import pytest

from tools.llm_stream import request_completion, build_chat_request
import tools.llm_stream as llm_stream_mod


@pytest.fixture(autouse=True)
def _reset_response_format_cache():
    """AUTO-JSONMODE-1's unsupported-URL memory is process-lifetime global
    state — reset it before/after every test so tests don't leak into each
    other regardless of run order."""
    llm_stream_mod._RESPONSE_FORMAT_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._RESPONSE_FORMAT_UNSUPPORTED_KEYS.clear()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
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
_OK_BODY_OLLAMA = {"message": {"content": '{"ok": true}'}}

# A strict-schema provider's rejection of response_format, shaped like the
# real Gemini "Unknown name" 400 the reasoning-field fallback already
# handles.
_REJECT_RESPONSE_FORMAT_400 = json.dumps({
    "error": {
        "code": 400,
        "message": 'Invalid JSON payload received. Unknown name "response_format": Cannot find field.',
        "status": "INVALID_ARGUMENT",
    }
})

_REJECT_FORMAT_400 = json.dumps({
    "error": {"message": "invalid field \"format\": not supported by this model"}
})


class TestBuildChatRequestResponseFormat:

    def test_openai_flag_true_sends_response_format(self):
        url, headers, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=True,
        )
        assert payload.get("response_format") == {"type": "json_object"}

    def test_openai_flag_false_omits_response_format(self):
        url, headers, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=False,
        )
        assert "response_format" not in payload

    def test_openai_flag_defaults_to_false(self):
        """No caller passes response_format at all — existing call sites
        (before this feature) must keep behaving exactly as before."""
        url, headers, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u",
        )
        assert "response_format" not in payload

    def test_ollama_flag_true_sends_format_json(self):
        url, headers, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=True,
        )
        assert payload.get("format") == "json"

    def test_ollama_flag_false_omits_format(self):
        url, headers, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=False,
        )
        assert "format" not in payload

    def test_marked_unsupported_url_is_skipped_even_when_flag_true(self):
        """Once mark_response_format_unsupported() has fired for a URL
        (real 400 seen earlier in the run), build_chat_request() must stop
        sending the field for that exact URL even though the caller's
        [api] response_format flag is still True — matches
        reasoning_field_is_supported's behaviour."""
        url, _, payload_before = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=True,
        )
        assert "response_format" in payload_before

        llm_stream_mod.mark_response_format_unsupported(url, "m")

        _, _, payload_after = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=True,
        )
        assert "response_format" not in payload_after


class TestResponseFormat400Fallback:

    def test_openai_400_strips_response_format_and_retries_once(self, caplog):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_RESPONSE_FORMAT_400, {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            with caplog.at_level(logging.WARNING):
                result = request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "gemini-x", "messages": [],
                     "response_format": {"type": "json_object"}},
                    timeout=5, stream=False, api_format="openai",
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert "response_format" in handler_cls.received_bodies[0]
        assert "response_format" not in handler_cls.received_bodies[1]
        # Payload-shape fix, not a rate limit — no sleep.
        sleep.assert_not_called()
        # Loud warning, not a silent swallow.
        assert any("response_format" in r.message or "JSON" in r.message
                   for r in caplog.records)

    def test_ollama_400_strips_format_and_retries_once(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_FORMAT_400, {}),
            ("ok", _OK_BODY_OLLAMA),
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                {"model": "m", "messages": [], "format": "json"},
                timeout=5, stream=False, api_format="ollama",
            )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert handler_cls.received_bodies[0].get("format") == "json"
        assert "format" not in handler_cls.received_bodies[1]

    def test_endpoint_remembered_after_400_skips_field_on_next_build(self):
        """After request_completion sees the 400 and calls
        mark_response_format_unsupported(url), a FRESH build_chat_request()
        call against that same URL — as a real caller's next loop
        iteration would make — must not send the field again."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_RESPONSE_FORMAT_400, {}),
            ("ok", _OK_BODY),
        ])
        url = f"http://127.0.0.1:{port}/chat/completions"
        try:
            request_completion(
                url, {"Content-Type": "application/json"},
                {"model": "m", "messages": [],
                 "response_format": {"type": "json_object"}},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()

        # Simulate the caller's next build_chat_request() call — same
        # base_url, flag still True — after the endpoint has been marked.
        _, _, next_payload = build_chat_request(
            base_url=f"http://127.0.0.1:{port}", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", response_format=True,
        )
        assert "response_format" not in next_payload

    def test_only_retries_once_second_400_raises(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_RESPONSE_FORMAT_400, {}),
            ("error", 400, "still broken, unrelated reason", {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [],
                     "response_format": {"type": "json_object"}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 2

    def test_400_without_response_format_in_payload_is_not_touched(self):
        """A 400 whose OUTGOING payload never had response_format/format
        must not be treated as a JSON-mode rejection — raises normally."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_RESPONSE_FORMAT_400, {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": []},  # no response_format key
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1

    def test_unrelated_400_is_not_treated_as_response_format_rejection(self):
        """A 400 that happens to have response_format in the payload but
        whose error text is unrelated (e.g. bad api key) must raise
        immediately, not loop trying to strip an innocent field."""
        server, port, handler_cls = _serve_script([
            ("error", 400, json.dumps({"error": "invalid api key"}), {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [],
                     "response_format": {"type": "json_object"}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1
