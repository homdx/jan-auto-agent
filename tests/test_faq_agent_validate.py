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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
