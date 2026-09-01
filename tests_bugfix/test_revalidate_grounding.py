"""
Tests for the dedicated grounding/intent revalidation pass.

Scenario from the bug report: knowledge/angie/debug_mode.txt documents only ONE
direction of an operation (e.g. how to ENABLE debug mode). When the user asks
for the OPPOSITE (how to DISABLE), the generator may fabricate or invert steps.

Chosen behaviour:
  * DIRECT   → KB answers as asked            → answer unchanged
  * INDIRECT → KB is related/opposite         → KB info returned WITH a caveat
  * NONE     → KB has nothing relevant        → NOT FOUND
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.faq_agent import FaqAgent, NOT_FOUND_MARKER

_RC = "tools.faq_agent.request_completion"

_ENABLE_ONLY = (
    "Angie Pro enable debug verbose log mode:\n"
    "sudo ln -fs angie-debug /usr/sbin/angie\n"
    "sudo angie -t && sudo service angie upgrade\n"
)


def _make_agent(tmp_path: Path, *, smart: bool, validate: bool = False) -> FaqAgent:
    kb = tmp_path / "knowledge" / "angie"
    kb.mkdir(parents=True)
    (kb / "debug_mode.txt").write_text(_ENABLE_ONLY)

    agent = FaqAgent(
        model="m", base_url="http://localhost:11434",
        api_key="k", api_format="ollama", timeout=10,
    )
    agent.knowledge_dir = tmp_path / "knowledge"
    agent.smart_search = smart
    agent.validate_answer_enabled = validate
    agent.revalidate_grounding_enabled = True
    agent._ensure_model = MagicMock()
    return agent


# ── Legacy path ──────────────────────────────────────────────────────────────

class TestRevalidateLegacy:
    def test_direct_keeps_answer(self, tmp_path):
        agent = _make_agent(tmp_path, smart=False)
        good = "sudo ln -fs angie-debug /usr/sbin/angie"
        with patch(_RC, side_effect=[good, "DIRECT"]):
            assert agent.answer("How enable debug log Angie Pro", stream=False) == good

    def test_indirect_returns_caveated_kb_info(self, tmp_path):
        agent = _make_agent(tmp_path, smart=False)
        fabricated = "To disable, run: sudo rm /usr/sbin/angie-debug"   # NOT in KB
        caveat = (
            "INDIRECT\n"
            "Note: the knowledge base does not document disabling debug mode; "
            "it only covers enabling it.\n"
            "To enable: sudo ln -fs angie-debug /usr/sbin/angie"
        )
        with patch(_RC, side_effect=[fabricated, caveat]):
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert result != fabricated                 # fabricated steps discarded
        assert "does not document disabling" in result
        assert "ln -fs angie-debug" in result       # real KB info surfaced
        assert "rm /usr/sbin/angie-debug" not in result

    def test_none_returns_not_found(self, tmp_path):
        agent = _make_agent(tmp_path, smart=False)
        off_topic = "Angie supports HTTP/3 since version 1.2."
        with patch(_RC, side_effect=[off_topic, "NONE"]):
            assert agent.answer("What is the capital of France", stream=False) == NOT_FOUND_MARKER

    def test_fail_open_keeps_answer(self, tmp_path):
        agent = _make_agent(tmp_path, smart=False)
        good = "sudo ln -fs angie-debug /usr/sbin/angie"
        with patch(_RC, side_effect=[good, RuntimeError("revalidation timeout")]):
            assert agent.answer("How enable debug log Angie Pro", stream=False) == good


# ── Smart-search path ─────────────────────────────────────────────────────────

class TestRevalidateSmart:
    def test_indirect_in_smart_path(self, tmp_path):
        agent = _make_agent(tmp_path, smart=True)
        fabricated = "sudo rm /usr/sbin/angie-debug"
        caveat = (
            "INDIRECT\n"
            "Note: the KB only documents enabling debug mode, not disabling.\n"
            "sudo ln -fs angie-debug /usr/sbin/angie"
        )
        with patch(_RC, side_effect=[
            '["disable","debug","angie","log"]',  # keyword extraction
            fabricated,                            # candidate answer
            caveat,                                # grounding revalidation
        ]):
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert "only documents enabling" in result
        assert "ln -fs angie-debug" in result

    def test_none_in_smart_path_falls_through(self, tmp_path):
        """A single candidate rejected as NONE → Stage-1 exhausted → Stage-2
        fallback also revalidates → NOT FOUND."""
        agent = _make_agent(tmp_path, smart=True)
        with patch(_RC, side_effect=[
            '["disable","debug","angie","log"]',  # keywords
            "sudo rm /usr/sbin/angie-debug",      # Stage-1 candidate
            "NONE",                               # Stage-1 revalidation → reject
            "sudo rm /usr/sbin/angie-debug",      # Stage-2 fallback answer
            "NONE",                               # Stage-2 revalidation → reject
        ]):
            result = agent.answer("How disable debug log Angie Pro", stream=False)
        assert result == NOT_FOUND_MARKER


# ── Disabled by default ───────────────────────────────────────────────────────

class TestRevalidateDisabled:
    def test_no_extra_call_when_disabled(self, tmp_path):
        agent = _make_agent(tmp_path, smart=False)
        agent.revalidate_grounding_enabled = False
        good = "sudo ln -fs angie-debug /usr/sbin/angie"
        with patch(_RC, side_effect=[good]) as rc:   # only ONE call, no revalidation
            assert agent.answer("How enable debug log Angie Pro", stream=False) == good
        assert rc.call_count == 1


# ── Cross-language flexibility (regression) ───────────────────────────────────

_RU_NODE_EXPORTER = (
    "Джоба для установки node_exporter на сервер.\n"
    "Запустить следующую джобу Jenkins\n"
    "https://jenkins.com/job/node_exporter/\n"
)


def _make_agent_ru(tmp_path: Path, *, smart: bool) -> FaqAgent:
    kb = tmp_path / "knowledge" / "elk"
    kb.mkdir(parents=True)
    (kb / "node_exporter.txt").write_text(_RU_NODE_EXPORTER)
    agent = FaqAgent(model="m", base_url="http://localhost:11434",
                     api_key="k", api_format="ollama", timeout=10)
    agent.knowledge_dir = tmp_path / "knowledge"
    agent.smart_search = smart
    agent.validate_answer_enabled = False
    agent.revalidate_grounding_enabled = True
    agent._ensure_model = MagicMock()
    return agent


class TestCrossLanguage:
    """English question + Russian KB → must still answer (DIRECT), not NOT FOUND.
    A cross-language match is a MEANING match; revalidation must return DIRECT."""

    def test_english_question_russian_kb_legacy(self, tmp_path):
        agent = _make_agent_ru(tmp_path, smart=False)
        translated = "Run the following Jenkins job: https://jenkins.com/job/node_exporter/"
        with patch(_RC, side_effect=[translated, "DIRECT"]):
            result = agent.answer("How install node_exporter", stream=False)
        assert result == translated
        assert result != NOT_FOUND_MARKER

    def test_english_question_russian_kb_smart(self, tmp_path):
        agent = _make_agent_ru(tmp_path, smart=True)
        translated = "Run the following Jenkins job: https://jenkins.com/job/node_exporter/"
        with patch(_RC, side_effect=[
            '["node_exporter","install","prometheus"]',  # keyword extraction
            translated,                                   # candidate answer (translated)
            "DIRECT",                                     # revalidation: meaning matches
        ]):
            result = agent.answer("How install node_exporter", stream=False)
        assert result == translated
