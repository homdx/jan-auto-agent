"""tests/test_fix11_main_show_and_improve_gate.py — AUTO-FIX-11.

Bug found by cross-referencing tools/prompt_parser.py's actual intent enum
against the gate in main.py's run_pipeline():

    if parsed.intent in ("optimize", "fix", "improve", "explain"):
        ...
        improvement = self.improvement_agent.process(...)
    else:
        improvement = {"explanation": "", "issues": [], "improved_code": "", "changes": []}

ParsedPrompt.intent can only ever be "show", "improve", "explain",
"show_and_improve", or "show_imports" (see prompt_parser.py's
_parse_via_regex). The literal strings "optimize" and "fix" are never
produced as an intent — they're just verbs that map into "improve" — so
those two tuple entries can never match anything. Meanwhile
"show_and_improve" IS a real, frequently-produced value: it's the parser's
own documented "Default fallback condition", returned whenever a prompt
combines a show-type verb with an improve-type verb (e.g. "show and
improve X", "show me how to optimize Y"). It was missing from the tuple,
so ImprovementAgent silently never ran for it, even though the validator
loop a few lines above (`if parsed.intent not in ("show", "show_imports")`)
correctly treats show_and_improve as needing full agent processing. The
user got the empty placeholder ("No explicit issues identified." / "No
modification entries logged.") instead of a real improvement result, with
no error or indication anything had gone wrong.

Confirmed end-to-end before the fix: building a real Orchestrator, mocking
validator_agent/search_agent/improvement_agent, and running run_pipeline()
with a "show and improve ..." prompt showed improvement_agent.process was
called 0 times. This test locks in the fix using the same approach.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_mod  # noqa: E402
from tools.prompt_parser import parse_prompt  # noqa: E402


@pytest.fixture
def orch(tmp_path, monkeypatch):
    """A real Orchestrator (all-default config — no agents.ini on disk),
    with every LLM-calling agent mocked so no network call is ever made."""
    monkeypatch.chdir(tmp_path)
    o = main_mod.Orchestrator(config_path="nonexistent-agents.ini")
    o.validator_agent.validate = MagicMock(return_value={"status": "approved", "feedback": ""})
    o.search_agent.run = MagicMock(
        return_value={"found": {}, "not_found": [], "searched_files": []}
    )
    o.improvement_agent.process = MagicMock(
        return_value={
            "explanation": "stub explanation",
            "issues": ["stub issue"],
            "improved_code": "def calculate_total(items): return sum(items)  # improved",
            "changes": ["stub change"],
        }
    )
    return o


def _write_target(tmp_path: Path) -> None:
    (tmp_path / "orders.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n"
    )


class TestParserProducesShowAndImprove:
    """Sanity check on the premise: this phrasing really yields show_and_improve."""

    def test_intent_is_show_and_improve(self, tmp_path):
        _write_target(tmp_path)
        source = (tmp_path / "orders.py").read_text()
        parsed = parse_prompt(
            "show and improve the calculate_total function in orders.py",
            source=source,
        )
        assert parsed.intent == "show_and_improve"


class TestImprovementAgentRunsForShowAndImprove:
    def test_process_is_called(self, orch, tmp_path):
        _write_target(tmp_path)
        orch.run_pipeline(
            "show and improve the calculate_total function in orders.py",
            base_dir=str(tmp_path),
        )
        assert orch.improvement_agent.process.called
        assert orch.improvement_agent.process.call_count == 1

    def test_called_with_show_and_improve_intent(self, orch, tmp_path):
        _write_target(tmp_path)
        orch.run_pipeline(
            "show and improve the calculate_total function in orders.py",
            base_dir=str(tmp_path),
        )
        called_intent = orch.improvement_agent.process.call_args[0][0]
        assert called_intent == "show_and_improve"


class TestOtherIntentsStillGateCorrectly:
    """The fix must not accidentally start running ImprovementAgent for
    intents that were correctly excluded before."""

    def test_plain_show_does_not_call_improvement_agent(self, orch, tmp_path):
        (tmp_path / "helper.py").write_text("def helper():\n    pass\n")
        orch.run_pipeline("show def helper in helper.py", base_dir=str(tmp_path))
        assert not orch.improvement_agent.process.called

    def test_plain_improve_still_calls_improvement_agent(self, orch, tmp_path):
        _write_target(tmp_path)
        orch.run_pipeline("improve calculate_total in orders.py", base_dir=str(tmp_path))
        assert orch.improvement_agent.process.called

    def test_explain_still_calls_improvement_agent(self, orch, tmp_path):
        _write_target(tmp_path)
        orch.run_pipeline("explain calculate_total in orders.py", base_dir=str(tmp_path))
        assert orch.improvement_agent.process.called
