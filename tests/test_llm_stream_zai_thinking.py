"""tests/test_llm_stream_zai_thinking.py — AUTO-ZAITHINK-1.

Field report: a real GLM-5.2 endpoint (via a router, api_format=openai)
ignored BOTH existing thinking-control conventions this codebase already
sends — OpenRouter's nested `reasoning: {"effort": ..., "exclude": ...}`
and the OpenAI/Gemini-compat flat `reasoning_effort` string — because
neither is Z.ai's documented shape. The model defaulted to its own "Max"
reasoning effort, exhausted a modest max_tokens budget entirely on
`<think>`, and left `content` genuinely empty on every attempt (Architect
`review_one_cluster`/`_parse_candidates` logged "JSON decode failed:
Expecting value: line 1 column 1 (char 0), Raw text: " repeatedly).

This covers the fix: build_chat_request() ADDITIONALLY sends Z.ai's own
top-level `thinking: {"type": "enabled" | "disabled"}` object whenever
`think` is not None, alongside (never instead of) the other two fields —
a provider that doesn't recognise `thinking` is expected to silently
ignore it, same assumption the other fields already rely on. If an
endpoint rejects it outright with HTTP 400, request_completion strips
ONLY that field and retries once, then remembers the (url, model) pair
for the rest of the process (AUTO-ZAITHINK-1 cache), mirroring the
mechanics of the other four capability caches in this module.
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

# AUTO-XDIST-PORT-RACE-1: this module binds a real http.server on an
# OS-assigned ephemeral port. Under pytest-xdist with 2+ workers, two
# DIFFERENT worker processes can genuinely bind/release overlapping
# ports at the same wall-clock moment -- confirmed live as the cause
# of an intermittent failure in this exact file, full-suite-only,
# never standalone. xdist_group pins every test in this module (and
# the 8 sibling files that also bind real servers, sharing this same
# group name) to the SAME worker, so none of them can ever race
# another for a port across concurrent workers.
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")



@pytest.fixture(autouse=True)
def _reset_zai_thinking_cache():
    """AUTO-ZAITHINK-1's unsupported-(url, model) memory is process-lifetime
    global state — reset it before/after every test so tests don't leak
    into each other regardless of run order."""
    llm_stream_mod._ZAI_THINKING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._ZAI_THINKING_UNSUPPORTED_KEYS.clear()


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

# Modeled on the real GLM-5.2-via-router symptom: an unrecognised
# top-level field rejected outright rather than silently ignored.
_REJECT_THINKING_400 = json.dumps({
    "error": {
        "code": 400,
        "message": 'Invalid JSON payload received. Unknown name "thinking": Cannot find field.',
        "status": "INVALID_ARGUMENT",
    }
})

# AUTO-NVIDIA-UNSUPPORTED-1: field report — a real NVIDIA NIM endpoint
# (integrate.api.nvidia.com, meta/llama-3.3-70b-instruct AND
# nvidia/nemotron-3-ultra-550b-a55b) rejects the `thinking` field with
# this exact phrasing on every single call, permanently, for both the
# Architect (plan_llm_profile) and Gate 1 (presence_llm_profile). Unlike
# _REJECT_THINKING_400 above, the message says "Unsupported parameter(s)"
# — one word, no space — which none of the five detection heuristics in
# this module matched (they all checked for "not supported", with a
# space, as a substring). Confirmed live: architect review_one_cluster
# logged "HTTP 400 ... Unsupported parameter(s): `thinking`" on both
# clusters of a 2-cluster repo, 0 grounded candidates were produced, and
# the run silently completed in a few seconds having done zero work —
# the strip-and-retry never fired because the match failed, so the same
# 400 repeated on every call for the rest of the process.
_REJECT_THINKING_400_NVIDIA_STYLE = json.dumps({
    "error": {
        "message": "Validation: Unsupported parameter(s): `thinking`",
        "type": "Bad Request",
        "code": 400,
    }
})


class TestBuildChatRequestZaiThinking:

    def test_openai_think_true_sends_thinking_enabled(self):
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True,
        )
        assert payload.get("thinking") == {"type": "enabled"}

    def test_openai_think_false_sends_thinking_disabled(self):
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert payload.get("thinking") == {"type": "disabled"}

    def test_openai_think_none_sends_nothing(self):
        """No caller passes think at all — existing call sites (before
        this feature) must keep behaving exactly as before."""
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u",
        )
        assert "thinking" not in payload

    def test_sent_alongside_reasoning_field_not_instead_of(self):
        """think=False must still send BOTH the existing OpenRouter
        `reasoning` suppression object AND the new Z.ai `thinking` object
        — a provider speaking either convention gets suppression."""
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert payload.get("reasoning") == {"effort": "low", "exclude": True}
        assert payload.get("thinking") == {"type": "disabled"}

    def test_sent_alongside_reasoning_effort_not_instead_of(self):
        _, _, payload = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert payload.get("reasoning_effort") == "high"
        assert payload.get("thinking") == {"type": "enabled"}

    def test_ollama_format_never_sends_thinking_field(self):
        """Z.ai's `thinking` object is an openai-branch-only convention —
        Ollama already has its own native top-level `think` field
        (AUTO-THINKDEPTH-1), untouched by this feature."""
        _, _, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="ollama", model="m",
            api_format="ollama", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=True, think_effort="high",
        )
        assert "thinking" not in payload
        assert payload["think"] == "high"

    def test_marked_unsupported_url_is_skipped_even_when_think_set(self):
        url, _, payload_before = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "thinking" in payload_before

        llm_stream_mod.mark_zai_thinking_unsupported(url, "m")

        _, _, payload_after = build_chat_request(
            base_url="https://api.example.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "thinking" not in payload_after
        # The other field must still be sent — only `thinking` was marked.
        assert payload_after.get("reasoning") == {"effort": "low", "exclude": True}


class TestZaiThinkingModelIndependence:
    """GATE1-PROVIDER-1: keyed by (url, model), not url alone — a router
    endpoint (freerouter.eu.cc, kenari.id, bynara.id, ...) that fronts
    several models must give each model its own independent verdict, in
    both directions."""

    def test_marking_one_model_does_not_affect_sibling_model_same_url(self):
        url = "https://router.example/v1/chat/completions"
        llm_stream_mod.mark_zai_thinking_unsupported(url, "model-a")
        assert llm_stream_mod.zai_thinking_is_supported(url, "model-a") is False
        assert llm_stream_mod.zai_thinking_is_supported(url, "model-b") is True

    def test_marking_one_url_does_not_affect_same_model_elsewhere(self):
        model = "same-model-name"
        llm_stream_mod.mark_zai_thinking_unsupported(
            "https://provider-a.example/v1/chat/completions", model)
        assert llm_stream_mod.zai_thinking_is_supported(
            "https://provider-a.example/v1/chat/completions", model) is False
        assert llm_stream_mod.zai_thinking_is_supported(
            "https://provider-b.example/v1/chat/completions", model) is True

    def test_build_chat_request_only_skips_for_the_marked_model(self):
        url = "https://router.example/v1/chat/completions"
        llm_stream_mod.mark_zai_thinking_unsupported(url, "model-a")

        _, _, payload_a = build_chat_request(
            base_url="https://router.example/v1", api_key="k", model="model-a",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "thinking" not in payload_a

        _, _, payload_b = build_chat_request(
            base_url="https://router.example/v1", api_key="k", model="model-b",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert payload_b.get("thinking") == {"type": "disabled"}


class TestZaiThinking400Fallback:

    def test_400_strips_thinking_and_retries_once(self, caplog):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            with caplog.at_level(logging.WARNING):
                result = request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "glm-5.2", "messages": [],
                     "thinking": {"type": "disabled"}},
                    timeout=5, stream=False, api_format="openai",
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert "thinking" in handler_cls.received_bodies[0]
        assert "thinking" not in handler_cls.received_bodies[1]
        # Payload-shape fix, not a rate limit — no sleep.
        sleep.assert_not_called()
        assert any("thinking" in r.message for r in caplog.records)

    def test_stripping_thinking_does_not_remove_reasoning_field(self):
        """Only `thinking` is stripped on its own rejection — a `reasoning`
        field sent in the SAME payload must survive untouched."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
            ("ok", _OK_BODY),
        ])
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "glm-5.2", "messages": [],
                 "reasoning": {"effort": "low", "exclude": True},
                 "thinking": {"type": "disabled"}},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()

        assert "thinking" not in handler_cls.received_bodies[1]
        assert handler_cls.received_bodies[1].get("reasoning") == {
            "effort": "low", "exclude": True,
        }

    def test_endpoint_remembered_after_400_skips_field_on_next_build(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
            ("ok", _OK_BODY),
        ])
        url = f"http://127.0.0.1:{port}/chat/completions"
        try:
            request_completion(
                url, {"Content-Type": "application/json"},
                {"model": "m", "messages": [], "thinking": {"type": "disabled"}},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()

        _, _, next_payload = build_chat_request(
            base_url=f"http://127.0.0.1:{port}", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert "thinking" not in next_payload

    def test_endpoint_remembered_only_for_the_rejecting_model(self):
        """Full integration: model A's 400 (real HTTP round trip) must not
        cause model B's very next call, against the same router URL, to
        have `thinking` stripped pre-emptively."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
            ("ok", _OK_BODY),
        ])
        try:
            request_completion(
                f"http://127.0.0.1:{port}/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "model-a", "messages": [],
                 "thinking": {"type": "disabled"}},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()

        _, _, payload_b = build_chat_request(
            base_url=f"http://127.0.0.1:{port}", api_key="k", model="model-b",
            api_format="openai", temperature=0.0, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert payload_b.get("thinking") == {"type": "disabled"}

    def test_only_retries_once_second_400_raises(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
            ("error", 400, "still broken, unrelated reason", {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [], "thinking": {"type": "disabled"}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 2

    def test_400_without_thinking_field_in_payload_is_not_touched(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400, {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": []},  # no thinking key
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1

    def test_nvidia_style_unsupported_phrasing_strips_and_retries(self, caplog):
        """AUTO-NVIDIA-UNSUPPORTED-1 regression: "Unsupported parameter(s):
        `thinking`" (no space between "un" and "supported") must match the
        same way "not supported" already does — this exact phrasing is
        what broke every architect/gate1 call against a real NVIDIA NIM
        endpoint until the detection keyword list included it."""
        server, port, handler_cls = _serve_script([
            ("error", 400, _REJECT_THINKING_400_NVIDIA_STYLE, {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            with caplog.at_level(logging.WARNING):
                result = request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "meta/llama-3.3-70b-instruct", "messages": [],
                     "thinking": {"type": "disabled"}},
                    timeout=5, stream=False, api_format="openai",
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()

        assert result == '{"ok": true}'
        assert handler_cls.request_count == 2
        assert "thinking" in handler_cls.received_bodies[0]
        assert "thinking" not in handler_cls.received_bodies[1]
        sleep.assert_not_called()

    def test_unrelated_400_is_not_treated_as_thinking_rejection(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, '{"error":{"message":"bad request: missing model"}}', {}),
        ])
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "m", "messages": [], "thinking": {"type": "disabled"}},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1
