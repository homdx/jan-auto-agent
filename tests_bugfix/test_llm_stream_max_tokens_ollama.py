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


class TestGate1PresenceThroughBuildChatRequest:
    """T40: the presence check builds its request through build_chat_request,
    so its configured `_presence_max_tokens` must reach Ollama as
    options.num_predict — not as a top-level max_tokens it ignores."""

    def test_presence_max_tokens_reaches_ollama_as_num_predict(self, tmp_path):
        import configparser
        import textwrap

        from tools.auto.architect import CandidateTask, CitedLocation
        from tools.auto.gate1_filter import Gate1Filter

        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "utils.py").write_text(
            textwrap.dedent("""\
                def parse_config(raw):
                    return raw
            """),
            encoding="utf-8",
        )

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:11434",
                "api_key": "ollama",
                "model": "llama3.1:8b",
                "api_format": "ollama",
            },
            "gate1": {
                "temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
                # A distinct presence profile makes the presence budget differ
                # from the shared [gate1] one, so this test fails if the shared
                # value is what reaches Ollama.
                "presence_llm_profile": "gate1_llm",
            },
            "gate1_llm": {
                "base_url": "http://localhost:11434",
                "api_key": "ollama",
                "model": "llama3.1:8b",
                "api_format": "ollama",
                "max_tokens": "777",
            },
            "loop": {"timeout_seconds": "10"},
        })
        filt = Gate1Filter(
            config=cfg, base_url="http://localhost:11434", api_key="ollama",
            model="llama3.1:8b", api_format="ollama", verify_ssl=False,
        )
        assert filt._presence_max_tokens == 777
        assert filt._max_tokens == 64
        candidate = CandidateTask(
            title="Add input validation",
            instruction="validate that raw is a dict",
            target_files=["tools/utils.py"],
            acceptance_check="true",
            cited_location=CitedLocation(
                file="tools/utils.py", symbol="parse_config", new_file=False,
            ),
        )

        captured: dict = {}

        def _fake_completion(**kwargs):
            captured.update(kwargs["payload"])
            return ('{"verdict": "confirmed", "evidence": "return raw", '
                    '"reason": "still missing validation"}')

        with patch("tools.llm_stream.request_completion",
                   side_effect=_fake_completion):
            accepted, _rejected = filt.filter([candidate], tmp_path)

        assert len(accepted) == 1
        assert captured["options"]["num_predict"] == 777
        assert "max_tokens" not in captured


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
