"""tests/test_llm_stream_think_effort.py — AUTO-THINKDEPTH-1.

Covers the global `[api] think_effort_enabled = true` + `[api]
think_effort = <depth>` switch:

  1. build_chat_request() forwards a reasoning-depth hint only when
     think=True AND think_effort is given — never when think is False/None,
     and never when think_effort_enabled is off (think_effort=None, the
     default with no code changes elsewhere).
  2. openai-format: depth rides inside the same `reasoning` object as the
     existing think=False suppression, sharing its unsupported-URL cache.
  3. ollama-format: depth is a string in the top-level `think` field
     (replacing the plain boolean), with its OWN unsupported-URL cache —
     rejecting the depth string degrades to the plain boolean, not to
     omitting thinking control altogether.
  4. Fallback: an HTTP 400 rejecting the depth value strips it and retries
     ONCE immediately, logs a warning, and remembers the endpoint so
     future build_chat_request() calls degrade automatically.
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
def _reset_caches():
    llm_stream_mod._REASONING_UNSUPPORTED_URLS.clear()
    llm_stream_mod._THINK_DEPTH_UNSUPPORTED_URLS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_URLS.clear()
    llm_stream_mod._THINK_DEPTH_UNSUPPORTED_URLS.clear()


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

_REJECT_THINK_DEPTH_400 = json.dumps({
    "error": {"message": 'invalid type for "think": expected boolean, got string'}
})

_REJECT_REASONING_EFFORT_400 = json.dumps({
    "error": {
        "code": 400,
        "message": 'Invalid JSON payload received. Unknown name "reasoning": Cannot find field.',
        "status": "INVALID_ARGUMENT",
    }
})


class TestBuildChatRequestThinkEffort:

    def test_openai_think_true_with_effort_sends_reasoning_effort(self):
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="medium",
        )
        assert payload.get("reasoning") == {"effort": "medium"}

    def test_openai_think_true_without_effort_sends_nothing(self):
        """think_effort_enabled off (default) — behaviour today: think=True
        alone sends no reasoning field at all."""
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True,
        )
        assert "reasoning" not in payload

    def test_openai_think_false_ignores_effort_keeps_suppression(self):
        """Depth only applies when think is True — think=False keeps its
        existing suppress-reasoning behaviour untouched even if an effort
        string is (incorrectly) passed alongside it."""
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False, think_effort="high",
        )
        assert payload.get("reasoning") == {"effort": "low", "exclude": True}

    def test_openai_think_none_ignores_effort(self):
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=None, think_effort="high",
        )
        assert "reasoning" not in payload

    def test_ollama_think_true_with_effort_sends_string_depth(self):
        _, _, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert payload.get("think") == "high"

    def test_ollama_think_true_without_effort_sends_plain_bool(self):
        _, _, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True,
        )
        assert payload.get("think") is True

    def test_ollama_think_false_ignores_effort(self):
        _, _, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False, think_effort="high",
        )
        assert payload.get("think") is False

    def test_ollama_marked_unsupported_url_degrades_to_bool(self):
        url, _, payload_before = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert payload_before["think"] == "high"

        llm_stream_mod.mark_think_depth_unsupported(url)

        _, _, payload_after = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert payload_after["think"] is True

    def test_openai_marked_unsupported_url_omits_effort_too(self):
        """The openai branch reuses the SAME cache as think=False
        suppression — a rejection in either direction blocks both."""
        url, _, _ = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        llm_stream_mod.mark_reasoning_field_unsupported(url)

        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert "reasoning" not in payload


class TestThinkEffort400Fallback:

    def test_ollama_400_strips_depth_and_retries_with_bool(self, caplog):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINK_DEPTH_400, {}),
            ("ok", _OK_BODY_OLLAMA),
        ])
        sleep = MagicMock()
        try:
            with caplog.at_level(logging.WARNING):
                result = request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [], "think": "high"},
                    timeout=5, stream=False, api_format="ollama",
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert handler_cls.received_bodies[0]["think"] == "high"
        assert handler_cls.received_bodies[1]["think"] is True
        sleep.assert_not_called()
        assert any("think" in r.message.lower() for r in caplog.records)

    def test_ollama_endpoint_remembered_after_400(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINK_DEPTH_400, {}),
            ("ok", _OK_BODY_OLLAMA),
        ])
        url = f"http://127.0.0.1:{port}/api/chat"
        try:
            request_completion(
                url, {"Content-Type": "application/json"},
                {"model": "m", "messages": [], "think": "high"},
                timeout=5, stream=False, api_format="ollama",
            )
        finally:
            server.shutdown()

        _, _, next_payload = build_chat_request(
            base_url=f"http://127.0.0.1:{port}", api_key="k", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        import sys
        print("DEBUG cache:", llm_stream_mod._THINK_DEPTH_UNSUPPORTED_URLS, file=sys.stderr)
        print("DEBUG url:", f"http://127.0.0.1:{port}/api/chat", file=sys.stderr)
        assert next_payload["think"] is True

    def test_ollama_only_retries_once_second_400_raises(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINK_DEPTH_400, {}),
            ("error", 400, "still broken, unrelated reason", {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [], "think": "high"},
                    timeout=5, stream=False, api_format="ollama",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 2

    def test_ollama_plain_bool_think_is_never_touched_by_this_fallback(self):
        """A plain boolean 'think' (no depth string, i.e. the existing
        pre-feature behaviour) hitting an unrelated 400 must not trigger
        this fallback — _has_think_depth_field requires a STRING value."""
        server, port, handler_cls = _serve_script([
            ("error", 400, json.dumps({"error": "bad api key"}), {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [], "think": True},
                    timeout=5, stream=False, api_format="ollama",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1

    def test_openai_reasoning_effort_400_falls_back_via_existing_cache(self):
        """Depth on the openai branch reuses the existing reasoning-field
        fallback (AUTO-REASONING-1) — no new code path, just confirming
        end to end that a rejected effort value strips 'reasoning' and
        retries once, same as the think=False suppression case."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_REASONING_EFFORT_400, {}),
            ("ok", _OK_BODY),
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "m", "messages": [], "reasoning": {"effort": "high"}},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert "reasoning" in handler_cls.received_bodies[0]
        assert "reasoning" not in handler_cls.received_bodies[1]
