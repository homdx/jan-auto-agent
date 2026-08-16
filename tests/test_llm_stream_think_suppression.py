"""tests/test_llm_stream_think_suppression.py — AUTO-THINK-1: think=False
must actually suppress reasoning for OpenAI-format requests too.

Story background
-----------------
Field report: --validate-plan against a real kenari.id (OpenAI-compatible,
free-tier) endpoint running deepseek-v4-flash:free produced intermittent
empty/truncated verdicts:

    Gate1._parse_presence_response [...]: JSON decode failed (Expecting
        value: line 1 column 1 (char 0)) — failing closed  raw=
    Gate1._parse_presence_response [...]: JSON decode failed (Unterminated
        string starting at: line 1 column 26 (char 25)) — failing closed
        raw={"verdict": "confirmed", "reason

The second line is the smoking gun: a genuine, otherwise-valid answer cut
off mid-string by the token cap — not a network or JSON-parsing bug. Root
cause: build_chat_request()'s docstring and its openai-format branch only
ever forwarded `think` into the payload for api_format="ollama" ("Ignored
for non-Ollama formats"), while every caller that sets it — Gate1Filter,
Coder, ClusterReviewer, TaskRewriter — defaults its own [section]
think = false out of the box, with a comment claiming this "disables that
reasoning ... regardless of which model is active". Against an
OpenAI-compatible remote gateway fronting a reasoning model, that promise
was silently broken: the model kept reasoning, consuming most or all of a
tight max_tokens budget (Gate 1 wants a tiny, deterministic JSON verdict —
512 tokens by default) before any usable answer was written.

No live network access; build_chat_request() is a pure function (no I/O),
and the end-to-end tests use a real local http.server the same way
test_llm_stream_empty_choices.py and test_llm_stream_retry.py do.
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

from tools.llm_stream import build_chat_request, request_completion


# ─────────────────────────────────────────────────────────────────────────────
# build_chat_request() — pure payload construction
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenAIFormatThinkSuppression:

    def test_think_false_adds_reasoning_object(self):
        _url, _headers, payload = build_chat_request(
            base_url="https://kenari.id/v1", api_key="k",
            model="deepseek-v4-flash:free", api_format="openai",
            temperature=0.0, max_tokens=512, system="s", user_msg="u",
            think=False,
        )
        assert payload["reasoning"] == {"effort": "low", "exclude": True}

    def test_think_true_adds_nothing_extra(self):
        _url, _headers, payload = build_chat_request(
            base_url="https://kenari.id/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=512,
            system="s", user_msg="u", think=True,
        )
        assert "reasoning" not in payload

    def test_think_none_default_unchanged_from_before(self):
        """Backward compatibility: a caller that never passes think= (the
        vast majority of ad-hoc/direct build_chat_request() callers, if
        any exist outside the four known ones) gets byte-identical
        payload shape to before this fix."""
        _url, _headers, payload = build_chat_request(
            base_url="https://api.openai.com/v1", api_key="k", model="m",
            api_format="openai", temperature=0.4, max_tokens=400,
            system="s", user_msg="u",
        )
        assert "reasoning" not in payload
        assert set(payload.keys()) == {"model", "temperature", "max_tokens", "messages"}

    def test_never_sends_the_field_known_to_hard_400_on_groq(self):
        """chat_template_kwargs is deliberately excluded — this function
        has no per-server memory to recover from a provider rejecting an
        unrecognised field, unlike the sibling project's stateful client."""
        _url, _headers, payload = build_chat_request(
            base_url="https://api.groq.com/openai/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=512,
            system="s", user_msg="u", think=False,
        )
        assert "chat_template_kwargs" not in payload
        assert "reasoning_effort" not in payload

    def test_reasoning_effort_value_is_not_sent_standalone(self):
        """Only the unified `reasoning` object is sent, not a flat
        top-level `reasoning_effort` -- its accepted enum values vary
        per-provider (native OpenAI wants low/medium/high, Groq wants
        none/default) and this stateless function can't adapt to that."""
        _url, _headers, payload = build_chat_request(
            base_url="https://kenari.id/v1", api_key="k", model="m",
            api_format="openai", temperature=0.0, max_tokens=512,
            system="s", user_msg="u", think=False,
        )
        assert "reasoning_effort" not in payload


class TestOllamaFormatUnaffected:
    """The pre-existing Ollama branch must behave exactly as before."""

    def test_think_false_still_sets_top_level_think_field(self):
        _url, _headers, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="k", model="m",
            api_format="ollama", temperature=0.0, max_tokens=512,
            system="s", user_msg="u", think=False,
        )
        assert payload["think"] is False
        assert "reasoning" not in payload

    def test_think_true_still_sets_top_level_think_field(self):
        _url, _headers, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="k", model="m",
            api_format="ollama", temperature=0.0, max_tokens=512,
            system="s", user_msg="u", think=True,
        )
        assert payload["think"] is True

    def test_think_none_omits_the_field_entirely(self):
        _url, _headers, payload = build_chat_request(
            base_url="http://localhost:11434", api_key="k", model="m",
            api_format="ollama", temperature=0.0, max_tokens=512,
            system="s", user_msg="u",
        )
        assert "think" not in payload
        assert "reasoning" not in payload


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: the payload actually sent over the wire, and the fix reaching
# a real caller's default config
# ─────────────────────────────────────────────────────────────────────────────


class TestGate1FilterSendsReasoningSuppressionByDefault:
    """Gate1Filter defaults [gate1] think to False out of the box (no
    agents.ini override needed) — this proves the fix actually reaches
    the constructed payload for the exact caller in the field report, not
    just the pure build_chat_request() function in isolation.

    Mocked at the tools.llm_stream.request_completion boundary, matching
    every other Gate1Filter test in this codebase (see test_auto_b3.py) —
    not a real socket. An earlier real-http.server version of this test
    was correct but flaky specifically under the full test suite's
    combined parallel load (thousands of real sockets/threads across all
    xdist workers competing at once): it passed reliably alone and even
    alongside the other real-transport files in this directory, and only
    ever failed as part of a full run, with "no request reached the
    server" — a resource-contention symptom, not a logic bug. Gate1Filter
    is a payload *contents* concern here, not a transport concern (that's
    covered separately, with real sockets, in TestNormalPathUnaffected and
    test_llm_stream_retry.py), so mocking the transport boundary is both
    more correct in scope and immune to that flake.
    """

    def test_default_gate1_config_sends_reasoning_object_for_openai_format(self):
        import configparser
        from unittest.mock import patch

        from tools.auto.architect import CandidateTask, CitedLocation
        from tools.auto.gate1_filter import Gate1Filter

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "remote", "verify_ssl": "false"},
            "api_remote": {
                "base_url": "https://kenari.id/v1",
                "api_key": "k", "model": "deepseek-v4-flash:free",
                "api_format": "openai",
            },
            # [gate1] deliberately left with NO explicit think key — the
            # fallback=False default is exactly what's active for every
            # real user unless they override it.
            "gate1": {"temperature": "0.0", "max_tokens": "512"},
        })
        filt = Gate1Filter(
            config=cfg, base_url="https://kenari.id/v1",
            api_key="k", model="deepseek-v4-flash:free",
            api_format="openai", verify_ssl=False,
        )
        candidate = CandidateTask(
            title="t", instruction="i", target_files=["a.py"],
            acceptance_check="true",
            cited_location=CitedLocation(file="a.py", symbol="foo"),
        )

        captured = {}

        def _fake_request_completion(*, url, headers, payload, timeout, **kwargs):
            captured["payload"] = payload
            return '{"verdict": "confirmed", "reason": "still present"}'

        with patch("tools.llm_stream.request_completion", side_effect=_fake_request_completion):
            filt._check_presence(candidate, "def foo(): pass")

        assert "payload" in captured, "request_completion was never called"
        assert captured["payload"].get("reasoning") == {"effort": "low", "exclude": True}


class TestNormalPathUnaffected:

    def test_non_streaming_openai_call_without_think_unaffected(self):
        """Sanity: a plain call with no think= at all still round-trips
        exactly as before this fix."""
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.body = json.loads(self.rfile.read(length).decode("utf-8"))
                type(self).captured = self.body
                resp = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *a):
                pass

        handler_cls = type("H", (_Handler,), {"captured": None})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url, headers, payload = build_chat_request(
                base_url=f"http://127.0.0.1:{port}", api_key="k", model="m",
                api_format="openai", temperature=0.4, max_tokens=100,
                system="s", user_msg="u",
            )
            result = request_completion(
                url, headers, payload, timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "ok"
        assert "reasoning" not in handler_cls.captured
