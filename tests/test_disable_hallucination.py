"""
Regression test: FaqAgent must NOT hallucinate when the knowledge base contains
only the *opposite* operation from what was asked.

Scenario (from the bug report):
  knowledge/angie/debug_mode.txt  →  documents how to ENABLE debug mode
  Question: "How disable debug log Angie Pro"
  Expected: NOT FOUND  (file only covers enabling, not disabling)
  Was:      hallucinated disable steps invented from the enable steps
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util, types

# Stub llm_stream so no real network is needed.
try:
    from tools.llm_stream import strip_think
    _HAS_REAL = True
except ModuleNotFoundError:
    import re as _re
    _THINK_RE = _re.compile(r"<think>.*?</think>", _re.DOTALL | _re.IGNORECASE)
    def _stub_strip_think(t):
        return _THINK_RE.sub("", t).strip()
    stub = types.ModuleType("tools.llm_stream")
    stub.strip_think = _stub_strip_think
    stub.ollama_chat_url = lambda b: f"{b}/api/chat"
    stub.request_completion = lambda *a, **kw: ""
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.llm_stream"] = stub
    _HAS_REAL = False

_FAQ_PATH = PROJECT_ROOT / "tools" / "faq_agent.py"
if _FAQ_PATH.exists():
    spec    = importlib.util.spec_from_file_location("faq_mod_regr", _FAQ_PATH)
    faq_mod = importlib.util.module_from_spec(spec)
    sys.modules["faq_mod_regr"] = faq_mod
    spec.loader.exec_module(faq_mod)
    FaqAgent = faq_mod.FaqAgent
    NOT_FOUND_MARKER = faq_mod.NOT_FOUND_MARKER
else:
    from tools.faq_agent import FaqAgent, NOT_FOUND_MARKER
    import tools.faq_agent as faq_mod

_RC_PATH = "faq_mod_regr.request_completion" if _FAQ_PATH.exists() else "tools.faq_agent.request_completion"

# The actual content of knowledge/angie/debug_mode.txt
_ENABLE_ONLY_CONTENT = """\
Angie Pro enable debug verbose log mode:
sudo ln -fs angie-debug /usr/sbin/angie
sudo angie -t && sudo service angie upgrade
"""


def _make_agent(tmp_path: Path, *, validate: bool, smart: bool) -> FaqAgent:
    kb = tmp_path / "knowledge" / "angie"
    kb.mkdir(parents=True)
    (kb / "debug_mode.txt").write_text(_ENABLE_ONLY_CONTENT)

    agent = FaqAgent(
        model="test-model",
        base_url="https://api.company.com",
        api_key="test",
        api_format="ollama",
        timeout=10,
    )
    agent.knowledge_dir = tmp_path / "knowledge"
    agent.validate_answer_enabled = validate
    agent.validate_temperature = 0.0
    agent.validate_max_tokens = 64
    agent.validate_system = faq_mod._DEFAULT_VALIDATE_SYSTEM
    agent.system_prompt = faq_mod._DEFAULT_SYSTEM
    agent.smart_search = smart
    agent._ensure_model = MagicMock()
    return agent


class TestNoHallucinationOnOppositeIntent:
    """The agent must return NOT FOUND when the KB only covers the opposite action."""

    def test_disable_question_returns_not_found_legacy(self, tmp_path):
        """Legacy mode: single-call path must return NOT FOUND, not a fabricated answer."""
        agent = _make_agent(tmp_path, validate=False, smart=False)
        # Simulate a model that hallucinated a disable procedure
        hallucinated = (
            "To disable debug log mode for Angie Pro, remove the debug symlink:\n"
            "sudo ln -fs angie /usr/sbin/angie\n"
            "sudo angie -t && sudo service angie restart"
        )
        with patch(_RC_PATH, side_effect=[hallucinated, "INVALID: knowledge only describes enabling"]):
            agent.validate_answer_enabled = True
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert result == NOT_FOUND_MARKER, (
            f"Expected NOT FOUND when KB only covers enabling, got: {result!r}"
        )

    def test_disable_question_returns_not_found_smart(self, tmp_path):
        """Smart-search mode: candidate must be rejected as INVALID, then fallback also fails."""
        agent = _make_agent(tmp_path, validate=True, smart=True)
        hallucinated = (
            "To disable debug log mode for Angie Pro, remove the debug symlink:\n"
            "sudo ln -fs angie /usr/sbin/angie"
        )
        with patch(_RC_PATH, side_effect=[
            '["disable","debug","log","angie","pro"]',  # keyword extraction
            hallucinated,                                # Stage-1 candidate answer
            "INVALID: knowledge only covers enabling",  # validation → rejected
            hallucinated,                                # Stage-2 fallback
            "INVALID: knowledge only covers enabling",  # fallback validation
        ]):
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert result == NOT_FOUND_MARKER

    def test_enable_question_still_works(self, tmp_path):
        """Sanity check: the enable question must still return the correct answer."""
        agent = _make_agent(tmp_path, validate=True, smart=False)
        correct_ans = (
            "sudo ln -fs angie-debug /usr/sbin/angie\n"
            "sudo angie -t && sudo service angie upgrade"
        )
        with patch(_RC_PATH, side_effect=[correct_ans, "VALID"]):
            result = agent.answer("How enable debug log Angie Pro", stream=False)
        assert result == correct_ans

    def test_source_field_set_on_success(self, tmp_path):
        """last_source must be set to the matched filename when an answer is found."""
        agent = _make_agent(tmp_path, validate=True, smart=True)
        correct_ans = "sudo ln -fs angie-debug /usr/sbin/angie"
        with patch(_RC_PATH, side_effect=[
            '["enable","debug","log","angie","pro"]',
            correct_ans,
            "VALID",
        ]):
            result = agent.answer("How enable debug log Angie Pro", stream=False)
        assert result == correct_ans
        assert agent.last_source is not None
        assert "debug_mode" in agent.last_source

    def test_source_field_none_on_not_found(self, tmp_path):
        """last_source must remain None when no answer is found."""
        agent = _make_agent(tmp_path, validate=True, smart=True)
        with patch(_RC_PATH, side_effect=[
            '["disable","debug","log","angie","pro"]',
            "NOT FOUND",
            "NOT FOUND",
        ]):
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert result == NOT_FOUND_MARKER
        assert agent.last_source is None


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
