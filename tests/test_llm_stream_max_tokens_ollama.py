"""tests/test_llm_stream_max_tokens_ollama.py

Bug found during audit of tools/llm_stream.py (0% dedicated test coverage
before this file — the module's ~42% statement coverage was entirely
incidental, from other tests mocking request_completion() rather than
exercising _build_payload() directly).

_build_payload() translates a payload dict for the target API format.
For api_format="ollama" it correctly moved "temperature" into
options{} — but never touched "max_tokens" at all, so it stayed as a
meaningless TOP-LEVEL field. Ollama's /api/chat endpoint does not
recognize a top-level "max_tokens" (it silently ignores unknown fields);
the Ollama-native equivalent is options.num_predict, which
build_chat_request() (a separate payload-construction helper used by
Coder/Gate1Filter/Architect/TaskRewriter) already handles correctly.

request_completion() is called directly (bypassing build_chat_request)
by several agents that build their own OpenAI-shaped payload dict:
  - tools/improvement_agent.py ImprovementAgent.process()
  - tools/actions.py OrchestratorActions._edit_file_content() (when
    [file_editor] max_tokens is configured)
  - tools/faq_agent.py FAQAgent, at several call sites (keyword
    extraction, answer validation, INDIRECT-mode answer generation, the
    main QA answer)

Every one of them had its configured max_tokens cap silently ignored
whenever the active profile's api_format is "ollama" — which is this
project's own SHIPPED DEFAULT (agents.ini's [api_local]: api_format =
ollama). Confirmed end-to-end below via ImprovementAgent with a mocked
HTTP layer: the actual request body sent to the (fake) server had no
num_predict at all before this fix.

Fix: _build_payload() now pops "max_tokens" and moves it into
options["num_predict"] for ollama, mirroring exactly how it already
handles "temperature" and "num_ctx" — falsy/0 values are treated as "use
server default" and never forwarded, consistent with the existing num_ctx
handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.llm_stream import _build_payload  # noqa: E402


class TestBuildPayloadMaxTokensForOllama:
    def test_max_tokens_moves_to_options_num_predict(self):
        body = _build_payload(
            {"model": "x", "messages": [], "temperature": 0.3, "max_tokens": 2000},
            api_format="ollama", stream=False,
        )
        assert body["options"]["num_predict"] == 2000
        assert "max_tokens" not in body

    def test_falsy_max_tokens_is_not_forwarded(self):
        # Same "0 means use server default" convention num_ctx already uses.
        body = _build_payload(
            {"model": "x", "messages": [], "temperature": 0.3, "max_tokens": 0},
            api_format="ollama", stream=False,
        )
        assert "num_predict" not in body.get("options", {})
        assert "max_tokens" not in body

    def test_absent_max_tokens_key_is_unaffected(self):
        body = _build_payload(
            {"model": "x", "messages": [], "temperature": 0.3},
            api_format="ollama", stream=False,
        )
        assert "num_predict" not in body.get("options", {})

    def test_max_tokens_and_num_ctx_together(self):
        body = _build_payload(
            {"model": "x", "messages": [], "temperature": 0.3,
             "max_tokens": 500, "num_ctx": 4096},
            api_format="ollama", stream=False,
        )
        assert body["options"] == {
            "temperature": 0.3, "num_predict": 500, "num_ctx": 4096,
        }

    def test_openai_format_leaves_max_tokens_top_level(self):
        # Must not regress the correct, pre-existing openai-format behaviour.
        body = _build_payload(
            {"model": "x", "messages": [], "temperature": 0.3, "max_tokens": 2000},
            api_format="openai", stream=False,
        )
        assert body["max_tokens"] == 2000
        assert "options" not in body


class TestEndToEndThroughRealCallers:
    """Confirm the fix actually reaches the wire for callers that build
    their own OpenAI-shaped payload and call request_completion directly
    (as opposed to build_chat_request, which was already correct)."""

    @staticmethod
    def _fake_urlopen_capturing(captured: dict, reply_content: str):
        def _fake(req, timeout=None, context=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            resp.read.return_value = json.dumps(
                {"message": {"content": reply_content}}
            ).encode("utf-8")
            return resp
        return _fake

    def test_improvement_agent_max_tokens_reaches_ollama_as_num_predict(self):
        from tools.improvement_agent import ImprovementAgent

        ia = ImprovementAgent(api_format="ollama")
        ia.max_tokens = 1234
        captured: dict = {}
        reply = '{"explanation":"ok","issues":[],"improved_code":"","changes":[]}'

        with patch("urllib.request.urlopen",
                   side_effect=self._fake_urlopen_capturing(captured, reply)):
            ia.process("improve", {
                "target_block": "x", "imports": [], "related_code": {},
                "context_lines": "",
            })

        assert captured["body"]["options"]["num_predict"] == 1234
        assert "max_tokens" not in captured["body"]
