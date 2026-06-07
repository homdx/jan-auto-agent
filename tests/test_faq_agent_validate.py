"""
tests/test_faq_agent_validate.py

Regression coverage for the three additions to FaqAgent:
  1. _ensure_model()  — Ollama pull before inference; no-op for openai format.
  2. _validate_answer() — second-pass grounding check.
  3. answer() flow     — pull → find → not-found OR find → validate → result.

All tests mock urllib and request_completion so no real network is needed.
"""

import sys
import json
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

# ── project path setup ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# We import the module under test.  llm_stream is a real module in the archive
# copy; stub it so the tests run without a live model.
import importlib, types

# Build a minimal llm_stream stub if the real one is importable; otherwise
# create a placeholder so FaqAgent can always be imported.
try:
    from tools.llm_stream import strip_think          # noqa: F401 – real import OK
    _HAS_REAL_LLMSTREAM = True
except ModuleNotFoundError:
    import re as _re
    # Minimal faithful copy of the real strip_think — just enough for tests
    _THINK_RE = _re.compile(r"<think>.*?</think>", _re.DOTALL | _re.IGNORECASE)

    def _stub_strip_think(text: str) -> str:
        if not text:
            return text
        out = _THINK_RE.sub("", text)
        if "</think>" in out:
            out = out.rsplit("</think>", 1)[-1]
        elif "<think>" in out:
            out = out.split("<think>", 1)[0]
        out = out.replace("<think>", "").replace("</think>", "")
        return out.strip()

    stub = types.ModuleType("tools.llm_stream")
    stub.strip_think          = _stub_strip_think
    stub.ollama_chat_url      = lambda b: f"{b}/api/chat"
    stub.request_completion   = lambda *a, **kw: ""
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.llm_stream"] = stub
    _HAS_REAL_LLMSTREAM = False

# Import the module under test (uses the stub or the real one transparently)
import importlib.util, os

_FAQ_AGENT_PATH = PROJECT_ROOT / "faq_agent.py"

# Prefer the local copy of faq_agent.py (the modified one under test).
# Register it in sys.modules under "faq_agent_mod" so patch() can resolve
# the dotted path "faq_agent_mod.request_completion".
if _FAQ_AGENT_PATH.exists():
    spec    = importlib.util.spec_from_file_location("faq_agent_mod", _FAQ_AGENT_PATH)
    faq_mod = importlib.util.module_from_spec(spec)
    sys.modules["faq_agent_mod"] = faq_mod   # ← must be registered BEFORE exec
    spec.loader.exec_module(faq_mod)
    FaqAgent         = faq_mod.FaqAgent
    NOT_FOUND_MARKER = faq_mod.NOT_FOUND_MARKER
else:
    from tools.faq_agent import FaqAgent, NOT_FOUND_MARKER  # type: ignore
    import tools.faq_agent as faq_mod


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_agent(
    tmpdir: Path,
    *,
    api_format: str = "ollama",
    validate: bool = False,
    extra_kb: str | None = None,
) -> FaqAgent:
    """Build a FaqAgent pointed at *tmpdir* with optional KB content."""
    kb = tmpdir / "knowledge"
    kb.mkdir(exist_ok=True)
    if extra_kb is not None:
        (kb / "faq.txt").write_text(extra_kb)

    agent = FaqAgent(
        model="test-model",
        base_url="http://localhost:11434",
        api_key="test",
        api_format=api_format,
        timeout=10,
    )
    agent.knowledge_dir        = kb
    agent.validate_answer_enabled = validate
    agent.validate_temperature = 0.0
    agent.validate_max_tokens  = 64
    agent.validate_system      = faq_mod._DEFAULT_VALIDATE_SYSTEM
    return agent


# ════════════════════════════════════════════════════════════════════════════
# 1.  _ensure_model
# ════════════════════════════════════════════════════════════════════════════

class TestEnsureModel:
    """_ensure_model() hits /api/pull for ollama; is a no-op for openai."""

    def test_ollama_posts_to_pull_endpoint(self, tmp_path):
        agent = _make_agent(tmp_path, api_format="ollama")

        fake_resp = MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__  = MagicMock(return_value=False)
        fake_resp.read      = MagicMock(return_value=b'{"status":"success"}')

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            agent._ensure_model()

        assert mock_open.called, "_ensure_model must call urlopen for ollama"
        req = mock_open.call_args[0][0]
        assert "/api/pull" in req.full_url
        body = json.loads(req.data.decode())
        assert body["name"] == "test-model"
        assert body["stream"] is False

    def test_openai_format_skips_pull(self, tmp_path):
        agent = _make_agent(tmp_path, api_format="openai")
        with patch("urllib.request.urlopen") as mock_open:
            agent._ensure_model()
        mock_open.assert_not_called()

    def test_pull_error_is_swallowed(self, tmp_path):
        """A network error during pull must not propagate — it's a best-effort
        pre-flight check, not a hard gate."""
        agent = _make_agent(tmp_path, api_format="ollama")
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            # Must not raise
            agent._ensure_model()

    def test_pull_url_avoids_double_api(self, tmp_path):
        """base_url ending in /api must produce /api/pull, not /api/api/pull."""
        agent = _make_agent(tmp_path, api_format="ollama")
        agent.base_url = "http://localhost:11434/api"

        fake_resp = MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__  = MagicMock(return_value=False)
        fake_resp.read      = MagicMock(return_value=b"{}")

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            agent._ensure_model()

        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:11434/api/pull"
        assert "api/api" not in req.full_url


# ════════════════════════════════════════════════════════════════════════════
# 2.  _validate_answer
# ════════════════════════════════════════════════════════════════════════════

_RC_PATH = "faq_agent_mod.request_completion" if _FAQ_AGENT_PATH.exists() else "tools.faq_agent.request_completion"


class TestValidateAnswer:
    """_validate_answer() returns True/False based on model verdict."""

    def _patch_rc(self, verdict: str):
        return patch(_RC_PATH, return_value=verdict)

    def test_valid_verdict_returns_true(self, tmp_path):
        agent = _make_agent(tmp_path)
        with self._patch_rc("VALID"):
            result = agent._validate_answer("q?", "The answer.", "ctx")
        assert result is True

    def test_invalid_verdict_returns_false(self, tmp_path):
        agent = _make_agent(tmp_path)
        with self._patch_rc("INVALID: answer not in KB"):
            result = agent._validate_answer("q?", "hallucinated answer", "ctx")
        assert result is False

    def test_verdict_case_insensitive(self, tmp_path):
        agent = _make_agent(tmp_path)
        with self._patch_rc("valid"):
            assert agent._validate_answer("q?", "a", "c") is True
        with self._patch_rc("invalid: wrong"):
            assert agent._validate_answer("q?", "a", "c") is False

    def test_validation_error_fails_open(self, tmp_path):
        """If the validation API call raises, the answer is treated as valid
        (fail-open) so a transient error does not silently drop a good answer."""
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, side_effect=RuntimeError("timeout")):
            result = agent._validate_answer("q?", "a", "c")
        assert result is True

    def test_think_tags_stripped_before_verdict_check(self, tmp_path):
        agent = _make_agent(tmp_path)
        # Model wraps its answer in <think> tags (e.g. qwen3)
        with self._patch_rc("<think>reasoning</think>VALID"):
            assert agent._validate_answer("q?", "a", "c") is True
        with self._patch_rc("<think>reasoning</think>INVALID: wrong"):
            assert agent._validate_answer("q?", "a", "c") is False


# ════════════════════════════════════════════════════════════════════════════
# 3.  answer() — full flow
# ════════════════════════════════════════════════════════════════════════════

class TestAnswerFlow:
    """Integration-level tests for the rewritten answer() method."""

    _KB_CONTENT = "Q: How do I reset my password?\nA: Go to Settings → Reset password."

    def _patch_rc(self, side_effects):
        """side_effects is a list of return values; each call to request_completion
        pops the next one."""
        return patch(_RC_PATH, side_effect=side_effects)

    # ── step 1: pull model is always attempted ──────────────────────────────

    def test_ensure_model_called_before_inference(self, tmp_path):
        agent = _make_agent(tmp_path, api_format="ollama", extra_kb=self._KB_CONTENT)

        pull_calls: list = []

        def _fake_ensure(self_=None):   # bound-method substitute
            pull_calls.append(True)

        agent._ensure_model = _fake_ensure

        with patch(_RC_PATH, return_value="Go to Settings → Reset password."):
            agent.answer("How do I reset my password?", stream=False)

        assert pull_calls, "_ensure_model must be called before inference"

    # ── step 2: empty KB returns NOT_FOUND immediately ──────────────────────

    def test_empty_kb_returns_not_found_no_model_call(self, tmp_path):
        agent = _make_agent(tmp_path, api_format="openai")  # no KB content
        agent._ensure_model = MagicMock()

        with patch(_RC_PATH) as mock_rc:
            result = agent.answer("anything?", stream=False)

        assert result == NOT_FOUND_MARKER
        mock_rc.assert_not_called()

    # ── step 4: model says NOT FOUND → return NOT_FOUND, skip validation ───

    def test_model_not_found_skips_validation(self, tmp_path):
        agent = _make_agent(
            tmp_path, validate=True, extra_kb=self._KB_CONTENT
        )
        agent._ensure_model = MagicMock()
        validate_spy = MagicMock(return_value=True)
        agent._validate_answer = validate_spy

        with patch(_RC_PATH, return_value="NOT FOUND"):
            result = agent.answer("unrelated question?", stream=False)

        assert result == NOT_FOUND_MARKER
        validate_spy.assert_not_called()

    # ── step 5a: validate_answer disabled → answer returned without check ───

    def test_validation_disabled_skips_second_call(self, tmp_path):
        agent = _make_agent(
            tmp_path, validate=False, extra_kb=self._KB_CONTENT
        )
        agent._ensure_model = MagicMock()

        with patch(_RC_PATH, return_value="Go to Settings → Reset password.") as mock_rc:
            result = agent.answer("How do I reset my password?", stream=False)

        assert result == "Go to Settings → Reset password."
        # Only one API call: the inference — no validation call
        assert mock_rc.call_count == 1

    # ── step 5b: validate_answer enabled, answer passes ────────────────────

    def test_valid_answer_returned_when_validation_passes(self, tmp_path):
        agent = _make_agent(
            tmp_path, validate=True, extra_kb=self._KB_CONTENT
        )
        agent._ensure_model = MagicMock()

        # Call 1: inference → answer text
        # Call 2: validation → VALID
        with self._patch_rc(["Go to Settings → Reset password.", "VALID"]):
            result = agent.answer("How do I reset my password?", stream=False)

        assert result == "Go to Settings → Reset password."

    # ── step 5c: validate_answer enabled, answer fails ─────────────────────

    def test_invalid_answer_returns_not_found(self, tmp_path):
        agent = _make_agent(
            tmp_path, validate=True, extra_kb=self._KB_CONTENT
        )
        agent._ensure_model = MagicMock()

        with self._patch_rc(["Hallucinated answer.", "INVALID: not in KB"]):
            result = agent.answer("How do I reset my password?", stream=False)

        assert result == NOT_FOUND_MARKER

    # ── inference error → NOT_FOUND ─────────────────────────────────────────

    def test_inference_error_returns_not_found(self, tmp_path):
        agent = _make_agent(
            tmp_path, validate=True, extra_kb=self._KB_CONTENT
        )
        agent._ensure_model = MagicMock()

        with patch(_RC_PATH, side_effect=RuntimeError("connection refused")):
            result = agent.answer("anything?", stream=False)

        assert result == NOT_FOUND_MARKER

    # ── custom not_found_marker is respected ───────────────────────────────

    def test_custom_not_found_marker_propagates(self, tmp_path):
        agent = _make_agent(tmp_path, extra_kb=self._KB_CONTENT)
        agent._ensure_model = MagicMock()
        agent.not_found_marker = "NOPE"
        agent.NOT_FOUND        = "NOPE"

        with patch(_RC_PATH, return_value="NOPE"):
            result = agent.answer("q?", stream=False)

        assert result == "NOPE"


# ════════════════════════════════════════════════════════════════════════════
# 4.  Ini / config wiring
# ════════════════════════════════════════════════════════════════════════════

class TestIniConfig:
    """Verify that agents.ini keys are wired up correctly in __init__."""

    def _cfg(self, section_body: str):
        import configparser, io
        cfg = configparser.ConfigParser()
        cfg.read_string(f"[faq_agent]\n{section_body}")
        return cfg

    def test_validate_answer_defaults_to_false(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10, config=self._cfg(""))
        assert agent.validate_answer_enabled is False

    def test_validate_answer_true_when_set(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10,
                         config=self._cfg("validate_answer = true"))
        assert agent.validate_answer_enabled is True

    def test_validate_temperature_parsed(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10,
                         config=self._cfg("validate_temperature = 0.1"))
        assert agent.validate_temperature == pytest.approx(0.1)

    def test_validate_max_tokens_parsed(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10,
                         config=self._cfg("validate_max_tokens = 128"))
        assert agent.validate_max_tokens == 128

    def test_custom_validate_system_parsed(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10,
                         config=self._cfg("validate_system = my custom prompt"))
        assert agent.validate_system == "my custom prompt"

    def test_no_config_sets_safe_defaults(self):
        agent = FaqAgent(model="m", base_url="u", api_key="k",
                         api_format="openai", timeout=10, config=None)
        assert agent.validate_answer_enabled is False
        assert agent.validate_temperature    == 0.0
        assert agent.validate_max_tokens     == 64


# ════════════════════════════════════════════════════════════════════════════
# 5.  Recursive knowledge loading
# ════════════════════════════════════════════════════════════════════════════

class TestRecursiveKnowledge:
    """_load_knowledge() must walk sub-folders, not just the top level."""

    def _make_kb(self, tmp_path: Path) -> tuple[Path, FaqAgent]:
        kb = tmp_path / "knowledge"
        kb.mkdir()
        agent = FaqAgent(
            model="m", base_url="http://localhost:11434",
            api_key="k", api_format="ollama", timeout=10,
        )
        agent.knowledge_dir = kb
        return kb, agent

    def test_flat_file_found(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        (kb / "top.txt").write_text("Q: foo\nA: bar")
        files = agent.list_knowledge_files()
        assert "top.txt" in files

    def test_nested_file_found(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        sub = kb / "billing"
        sub.mkdir()
        (sub / "invoices.txt").write_text("Q: invoice\nA: check billing portal")
        files = agent.list_knowledge_files()
        # Relative path includes the sub-folder name
        assert any("invoices.txt" in f for f in files), f"got: {files}"
        assert any("billing" in f for f in files), f"got: {files}"

    def test_deeply_nested_file_found(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        deep = kb / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.md").write_text("Q: deep\nA: yes")
        files = agent.list_knowledge_files()
        assert any("deep.md" in f for f in files), f"got: {files}"

    def test_top_and_nested_both_loaded(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        (kb / "root.txt").write_text("root content")
        sub = kb / "sub"
        sub.mkdir()
        (sub / "child.txt").write_text("child content")
        files = agent.list_knowledge_files()
        assert len(files) == 2

    def test_non_matching_extension_ignored(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        sub = kb / "sub"
        sub.mkdir()
        (sub / "ignored.json").write_text("{}")
        (sub / "kept.txt").write_text("kept")
        files = agent.list_knowledge_files()
        assert files == [str(Path("sub") / "kept.txt")]

    def test_context_includes_subfolder_label(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        (kb / "products").mkdir()
        (kb / "products" / "pricing.txt").write_text("Price is $10/mo")
        docs = agent._load_knowledge()
        name, content = docs[0]
        # The section header shown to the model should include the sub-path
        assert "pricing.txt" in name
        assert "products" in name

    def test_answer_uses_nested_content(self, tmp_path):
        kb, agent = self._make_kb(tmp_path)
        agent._ensure_model = MagicMock()
        agent.validate_answer_enabled = False
        (kb / "auth").mkdir()
        (kb / "auth" / "login.txt").write_text(
            "Q: How do I log in?\nA: Use your email and password on the login page."
        )
        rc_path = (
            "faq_agent_mod.request_completion"
            if _FAQ_AGENT_PATH.exists()
            else "tools.faq_agent.request_completion"
        )
        with patch(rc_path, return_value="Use your email and password on the login page."):
            result = agent.answer("How do I log in?", stream=False)
        assert result == "Use your email and password on the login page."


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

# ════════════════════════════════════════════════════════════════════════════
# 6.  _extract_keywords
# ════════════════════════════════════════════════════════════════════════════

class TestExtractKeywords:
    """Architect pass: LLM returns JSON keyword list; fallback on failure."""

    def test_parses_json_array(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='["node", "exporter", "install"]'):
            kw = agent._extract_keywords("How install node exporter?")
        assert kw == ["node", "exporter", "install"]

    def test_strips_markdown_fences(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='```json\n["foo","bar"]\n```'):
            kw = agent._extract_keywords("foo bar?")
        assert kw == ["foo", "bar"]

    def test_strips_think_tags(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='<think>reasoning</think>["a","b"]'):
            kw = agent._extract_keywords("a b?")
        assert kw == ["a", "b"]

    def test_falls_back_on_json_error(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value="not json at all"):
            kw = agent._extract_keywords("How install node exporter")
        # Fallback: words from question minus stop-words
        assert "install" in kw
        assert "node" in kw
        assert "exporter" in kw

    def test_falls_back_on_api_error(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, side_effect=RuntimeError("timeout")):
            kw = agent._extract_keywords("How install node exporter")
        assert isinstance(kw, list)
        assert len(kw) > 0


# ════════════════════════════════════════════════════════════════════════════
# 7.  _rank_candidates
# ════════════════════════════════════════════════════════════════════════════

class TestRankCandidates:
    """Files with more keyword hits come first."""

    def test_best_match_first(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [
            ("unrelated/other.txt",         "something completely different"),
            ("elk/node-exporter.txt",        "install node exporter on linux"),
            ("network/firewall.txt",         "firewall rules"),
        ]
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        assert ranked[0][0] == "elk/node-exporter.txt"

    def test_zero_score_files_still_included(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [("a.txt", "apples"), ("b.txt", "bananas")]
        ranked = agent._rank_candidates(docs, ["mango"])
        # Both score 0 — order preserved from input (stable sort)
        assert len(ranked) == 2

    def test_path_match_counts(self, tmp_path):
        """Keyword match in the file path should score even with blank content."""
        agent = _make_agent(tmp_path)
        docs = [
            ("node-exporter/README.txt", ""),
            ("other/README.txt",         "node exporter install"),
        ]
        # First file: 2 path hits (node, exporter); second file: 3 content hits
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        # Second file has more total hits
        assert ranked[0][0] == "other/README.txt"

    def test_content_and_path_hits_sum(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [
            ("node/exporter.txt", "install steps here"),   # 3 hits (node+exporter+install)
            ("other.txt",         "node info"),             # 1 hit
        ]
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        assert ranked[0][0] == "node/exporter.txt"


# ════════════════════════════════════════════════════════════════════════════
# 8.  answer() smart-search flow
# ════════════════════════════════════════════════════════════════════════════

class TestSmartSearchFlow:
    _KB  = "Q: How install node exporter?\nA: Run: apt install prometheus-node-exporter"
    _ANS = "Run: apt install prometheus-node-exporter"

    def _agent(self, tmp_path, *, validate=False) -> FaqAgent:
        kb = tmp_path / "knowledge"
        (kb / "elk").mkdir(parents=True)
        (kb / "elk" / "node-exporter.txt").write_text(self._KB)
        agent = FaqAgent(
            model="m", base_url="http://localhost:11434",
            api_key="k", api_format="ollama", timeout=10,
        )
        agent.knowledge_dir          = kb
        agent.smart_search           = True
        agent.validate_answer_enabled = validate
        agent._ensure_model          = MagicMock()
        return agent

    def test_smart_search_finds_nested_file(self, tmp_path):
        agent = self._agent(tmp_path)
        # Call 1: keyword extraction  → keywords
        # Call 2: per-candidate answer → answer text
        with patch(_RC_PATH, side_effect=['["node","exporter","install"]', self._ANS]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_skips_not_found_candidate(self, tmp_path):
        """If best candidate returns NOT FOUND, fall through to fallback."""
        kb = tmp_path / "knowledge"
        kb.mkdir()
        (kb / "node-exporter.txt").write_text(self._KB)
        (kb / "other.txt").write_text("unrelated content")
        agent = FaqAgent(
            model="m", base_url="http://localhost:11434",
            api_key="k", api_format="ollama", timeout=10,
        )
        agent.knowledge_dir = kb
        agent.smart_search  = True
        agent._ensure_model = MagicMock()

        # "other.txt" scores 0 against keywords ["node","exporter"] and is
        # never tried in Stage 1.  Only node-exporter.txt is tried; when it
        # returns NOT FOUND the agent falls straight to the full-KB fallback.
        with patch(_RC_PATH, side_effect=[
            '["node","exporter"]',   # keyword extraction
            "NOT FOUND",             # candidate (node-exporter.txt) fails
            self._ANS,               # fallback full-KB call succeeds
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_fallback_when_all_candidates_fail(self, tmp_path):
        agent = self._agent(tmp_path)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            "NOT FOUND",                       # candidate fails
            self._ANS,                         # fallback succeeds
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_disabled_uses_legacy(self, tmp_path):
        agent = self._agent(tmp_path)
        agent.smart_search = False
        # Only ONE rc call (no keyword extraction, no per-candidate loop)
        with patch(_RC_PATH, return_value=self._ANS) as mock_rc:
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS
        assert mock_rc.call_count == 1

    def test_keyword_extraction_failure_falls_back_gracefully(self, tmp_path):
        agent = self._agent(tmp_path)
        with patch(_RC_PATH, side_effect=[
            RuntimeError("LLM down"),  # keyword extraction fails → word-split fallback
            self._ANS,                  # per-candidate answer
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_with_validation_pass(self, tmp_path):
        agent = self._agent(tmp_path, validate=True)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            self._ANS,                         # candidate answer
            "VALID",                           # validation
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_with_validation_fail_then_fallback(self, tmp_path):
        agent = self._agent(tmp_path, validate=True)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            "Hallucinated answer.",            # candidate answer
            "INVALID: not grounded",           # validation fails
            self._ANS,                         # fallback answer
            "VALID",                           # fallback validation
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS
# ════════════════════════════════════════════════════════════════════════════
# 6.  _extract_keywords
# ════════════════════════════════════════════════════════════════════════════

class TestExtractKeywords:
    """Architect pass: LLM returns JSON keyword list; fallback on failure."""

    def test_parses_json_array(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='["node", "exporter", "install"]'):
            kw = agent._extract_keywords("How install node exporter?")
        assert kw == ["node", "exporter", "install"]

    def test_strips_markdown_fences(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='```json\n["foo","bar"]\n```'):
            kw = agent._extract_keywords("foo bar?")
        assert kw == ["foo", "bar"]

    def test_strips_think_tags(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value='<think>reasoning</think>["a","b"]'):
            kw = agent._extract_keywords("a b?")
        assert kw == ["a", "b"]

    def test_falls_back_on_json_error(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, return_value="not json at all"):
            kw = agent._extract_keywords("How install node exporter")
        # Fallback: words from question minus stop-words
        assert "install" in kw
        assert "node" in kw
        assert "exporter" in kw

    def test_falls_back_on_api_error(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch(_RC_PATH, side_effect=RuntimeError("timeout")):
            kw = agent._extract_keywords("How install node exporter")
        assert isinstance(kw, list)
        assert len(kw) > 0


# ════════════════════════════════════════════════════════════════════════════
# 7.  _rank_candidates
# ════════════════════════════════════════════════════════════════════════════

class TestRankCandidates:
    """Files with more keyword hits come first."""

    def test_best_match_first(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [
            ("unrelated/other.txt",         "something completely different"),
            ("elk/node-exporter.txt",        "install node exporter on linux"),
            ("network/firewall.txt",         "firewall rules"),
        ]
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        assert ranked[0][0] == "elk/node-exporter.txt"

    def test_zero_score_files_still_included(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [("a.txt", "apples"), ("b.txt", "bananas")]
        ranked = agent._rank_candidates(docs, ["mango"])
        # Both score 0 — order preserved from input (stable sort)
        assert len(ranked) == 2

    def test_path_match_counts(self, tmp_path):
        """Keyword match in the file path should score even with blank content."""
        agent = _make_agent(tmp_path)
        docs = [
            ("node-exporter/README.txt", ""),
            ("other/README.txt",         "node exporter install"),
        ]
        # First file: 2 path hits (node, exporter); second file: 3 content hits
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        # Second file has more total hits
        assert ranked[0][0] == "other/README.txt"

    def test_content_and_path_hits_sum(self, tmp_path):
        agent = _make_agent(tmp_path)
        docs = [
            ("node/exporter.txt", "install steps here"),   # 3 hits (node+exporter+install)
            ("other.txt",         "node info"),             # 1 hit
        ]
        ranked = agent._rank_candidates(docs, ["node", "exporter", "install"])
        assert ranked[0][0] == "node/exporter.txt"


# ════════════════════════════════════════════════════════════════════════════
# 8.  answer() smart-search flow
# ════════════════════════════════════════════════════════════════════════════

class TestSmartSearchFlow:
    _KB  = "Q: How install node exporter?\nA: Run: apt install prometheus-node-exporter"
    _ANS = "Run: apt install prometheus-node-exporter"

    def _agent(self, tmp_path, *, validate=False) -> FaqAgent:
        kb = tmp_path / "knowledge"
        (kb / "elk").mkdir(parents=True)
        (kb / "elk" / "node-exporter.txt").write_text(self._KB)
        agent = FaqAgent(
            model="m", base_url="http://localhost:11434",
            api_key="k", api_format="ollama", timeout=10,
        )
        agent.knowledge_dir          = kb
        agent.smart_search           = True
        agent.validate_answer_enabled = validate
        agent._ensure_model          = MagicMock()
        return agent

    def test_smart_search_finds_nested_file(self, tmp_path):
        agent = self._agent(tmp_path)
        # Call 1: keyword extraction  → keywords
        # Call 2: per-candidate answer → answer text
        with patch(_RC_PATH, side_effect=['["node","exporter","install"]', self._ANS]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_skips_not_found_candidate(self, tmp_path):
        """If best candidate returns NOT FOUND, fall through to fallback."""
        kb = tmp_path / "knowledge"
        kb.mkdir()
        (kb / "node-exporter.txt").write_text(self._KB)
        (kb / "other.txt").write_text("unrelated content")
        agent = FaqAgent(
            model="m", base_url="http://localhost:11434",
            api_key="k", api_format="ollama", timeout=10,
        )
        agent.knowledge_dir = kb
        agent.smart_search  = True
        agent._ensure_model = MagicMock()

        # "other.txt" scores 0 against keywords ["node","exporter"] and is
        # never tried in Stage 1.  Only node-exporter.txt is tried; when it
        # returns NOT FOUND the agent falls straight to the full-KB fallback.
        with patch(_RC_PATH, side_effect=[
            '["node","exporter"]',   # keyword extraction
            "NOT FOUND",             # candidate (node-exporter.txt) fails
            self._ANS,               # fallback full-KB call succeeds
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_fallback_when_all_candidates_fail(self, tmp_path):
        agent = self._agent(tmp_path)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            "NOT FOUND",                       # candidate fails
            self._ANS,                         # fallback succeeds
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_disabled_uses_legacy(self, tmp_path):
        agent = self._agent(tmp_path)
        agent.smart_search = False
        # Only ONE rc call (no keyword extraction, no per-candidate loop)
        with patch(_RC_PATH, return_value=self._ANS) as mock_rc:
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS
        assert mock_rc.call_count == 1

    def test_keyword_extraction_failure_falls_back_gracefully(self, tmp_path):
        agent = self._agent(tmp_path)
        with patch(_RC_PATH, side_effect=[
            RuntimeError("LLM down"),  # keyword extraction fails → word-split fallback
            self._ANS,                  # per-candidate answer
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_with_validation_pass(self, tmp_path):
        agent = self._agent(tmp_path, validate=True)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            self._ANS,                         # candidate answer
            "VALID",                           # validation
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS

    def test_smart_search_with_validation_fail_then_fallback(self, tmp_path):
        agent = self._agent(tmp_path, validate=True)
        with patch(_RC_PATH, side_effect=[
            '["node","exporter","install"]',  # keywords
            "Hallucinated answer.",            # candidate answer
            "INVALID: not grounded",           # validation fails
            self._ANS,                         # fallback answer
            "VALID",                           # fallback validation
        ]):
            result = agent.answer("How install node exporter?", stream=False)
        assert result == self._ANS
