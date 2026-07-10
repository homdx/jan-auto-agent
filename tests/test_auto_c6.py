"""tests/test_auto_c6.py — AUTO-C6: exhaustion → knowledge + investigation ticket.

ACs (from the Jira story):
  * A permanently-failing task does not stall the whole run (tested via the
    controller path — handler returns an ExhaustionOutcome, not an exception).
  * A ticket with the knowledge is created:
      - .agent/tasks/<id>/knowledge.md is written with round feedback.
      - .agent/tickets/TICKET-<id>.json is written with the correct schema.
  * Task status is (re-)asserted as BLOCKED.
  * knowledge.md contains task title, instruction, acceptance check, and all
    per-round feedback text.
  * Ticket JSON matches the expected schema fields and values.
  * run.log records the event.
  * Handles missing / empty outer_result.knowledge() gracefully.
  * make_exhaustion_handler factory returns a ready ExhaustionHandler.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.exhaustion_handler import (
    ExhaustionHandler,
    ExhaustionOutcome,
    TICKET_TYPE_INVESTIGATION,
    TICKET_STATUS_OPEN,
    make_exhaustion_handler,
)
from tools.auto.state import StateStore, make_task, STATUS_BLOCKED, STATUS_TODO


# ── fakes ────────────────────────────────────────────────────────────────────

@dataclass
class FakeOuterResult:
    """Minimal stand-in for OuterLoopResult."""
    task_id:     str = "AUTO-T1"
    passed:      bool = False
    exhausted:   bool = True
    rounds_used: int = 10
    _knowledge:  str = ""
    feedback_files: list = field(default_factory=list)

    def knowledge(self) -> str:
        return self._knowledge


# ── helpers ──────────────────────────────────────────────────────────────────

def _state(tmp_path: Path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("test goal", tmp_path)
    st.upsert_task(
        make_task(
            id="AUTO-T1",
            title="Fix the retry logic",
            instruction="Rewrite the retry loop to handle transient errors.",
            target_files=["tools/retry.py"],
            acceptance_check="pytest tests/test_retry.py -q",
        )
    )
    return st


TASK = {
    "id":               "AUTO-T1",
    "title":            "Fix the retry logic",
    "instruction":      "Rewrite the retry loop to handle transient errors.",
    "target_files":     ["tools/retry.py"],
    "acceptance_check": "pytest tests/test_retry.py -q",
}


# ── core AC tests ─────────────────────────────────────────────────────────────

class TestKnowledgeNote:
    def test_knowledge_file_created(self, tmp_path):
        st  = _state(tmp_path)
        out = FakeOuterResult(_knowledge="Round 1 failed: timeout\nRound 2 failed: assert error")
        ExhaustionHandler(st).handle(TASK, out)
        kpath = tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md"
        assert kpath.exists(), "knowledge.md not created"

    def test_knowledge_contains_title(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        text = (tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md").read_text()
        assert "Fix the retry logic" in text

    def test_knowledge_contains_instruction(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        text = (tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md").read_text()
        assert "Rewrite the retry loop" in text

    def test_knowledge_contains_acceptance_check(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        text = (tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md").read_text()
        assert "pytest tests/test_retry.py" in text

    def test_knowledge_contains_round_feedback(self, tmp_path):
        st  = _state(tmp_path)
        feedback = "Round 1: assertion failed — got None\nRound 2: timeout after 30s"
        ExhaustionHandler(st).handle(TASK, FakeOuterResult(_knowledge=feedback))
        text = (tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md").read_text()
        assert "assertion failed" in text
        assert "timeout after 30s" in text

    def test_knowledge_mentions_rounds_used(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult(rounds_used=7))
        text = (tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md").read_text()
        assert "7" in text

    def test_knowledge_graceful_when_no_feedback(self, tmp_path):
        """Empty knowledge() must not crash; file still written."""
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult(_knowledge=""))
        kpath = tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md"
        assert kpath.exists()
        assert len(kpath.read_text()) > 0

    def test_knowledge_graceful_when_outer_result_is_none(self, tmp_path):
        """None outer_result must not crash; file still written."""
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, None)
        kpath = tmp_path / ".agent" / "tasks" / "AUTO-T1" / "knowledge.md"
        assert kpath.exists()


class TestTicket:
    def _ticket(self, tmp_path: Path) -> dict:
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult(_knowledge="error X"))
        tpath = tmp_path / ".agent" / "tickets" / "TICKET-AUTO-T1.json"
        return json.loads(tpath.read_text())

    def test_ticket_file_created(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        tpath = tmp_path / ".agent" / "tickets" / "TICKET-AUTO-T1.json"
        assert tpath.exists(), "ticket JSON not created"

    def test_ticket_id(self, tmp_path):
        assert self._ticket(tmp_path)["id"] == "TICKET-AUTO-T1"

    def test_ticket_type(self, tmp_path):
        assert self._ticket(tmp_path)["type"] == TICKET_TYPE_INVESTIGATION

    def test_ticket_status_open(self, tmp_path):
        assert self._ticket(tmp_path)["status"] == TICKET_STATUS_OPEN

    def test_ticket_linked_task(self, tmp_path):
        assert self._ticket(tmp_path)["linked_task"] == "AUTO-T1"

    def test_ticket_title_contains_task_title(self, tmp_path):
        assert "Fix the retry logic" in self._ticket(tmp_path)["title"]

    def test_ticket_body_contains_knowledge(self, tmp_path):
        assert "error X" in self._ticket(tmp_path)["body"]

    def test_ticket_has_created_at(self, tmp_path):
        t = self._ticket(tmp_path)
        assert "created_at" in t and t["created_at"]

    def test_ticket_is_valid_json(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        tpath = tmp_path / ".agent" / "tickets" / "TICKET-AUTO-T1.json"
        data = json.loads(tpath.read_text())   # raises on bad JSON
        assert isinstance(data, dict)

    def test_ticket_path_in_outcome(self, tmp_path):
        st  = _state(tmp_path)
        outcome = ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        assert outcome.ticket_path.exists()
        assert outcome.ticket_path.name == "TICKET-AUTO-T1.json"


class TestTaskStatus:
    def test_task_marked_blocked(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        assert st.get_task("AUTO-T1")["status"] == STATUS_BLOCKED

    def test_blocked_is_idempotent(self, tmp_path):
        """Calling handle twice must not crash (BLOCKED → BLOCKED is fine)."""
        st  = _state(tmp_path)
        st.set_task_status("AUTO-T1", STATUS_BLOCKED)   # pre-set by OuterLoop
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        assert st.get_task("AUTO-T1")["status"] == STATUS_BLOCKED


class TestRunLog:
    def test_log_written(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "AUTO-T1" in log
        assert "BLOCKED" in log

    def test_log_mentions_ticket(self, tmp_path):
        st  = _state(tmp_path)
        ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "TICKET-AUTO-T1" in log


class TestOutcome:
    def test_outcome_fields(self, tmp_path):
        st  = _state(tmp_path)
        outcome = ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        assert isinstance(outcome, ExhaustionOutcome)
        assert outcome.task_id    == "AUTO-T1"
        assert outcome.ticket_id  == "TICKET-AUTO-T1"
        assert outcome.knowledge_path.name == "knowledge.md"
        assert outcome.ticket_path.exists()

    def test_outcome_summary_str(self, tmp_path):
        st  = _state(tmp_path)
        outcome = ExhaustionHandler(st).handle(TASK, FakeOuterResult())
        s = outcome.summary()
        assert "AUTO-T1" in s and "BLOCKED" in s

    def test_does_not_raise(self, tmp_path):
        """Handler must never raise regardless of input."""
        st  = _state(tmp_path)
        # Should not raise even with a broken outer_result
        class Broken:
            task_id = "AUTO-T1"
            passed = False
            exhausted = True
            rounds_used = 10
            feedback_files = []
            def knowledge(self): raise RuntimeError("disk full")

        outcome = ExhaustionHandler(st).handle(TASK, Broken())
        assert outcome.knowledge_path.exists()


class TestIndependentTasksContinue:
    """A blocked task must not prevent independent tasks from being processed.

    The handler returns normally; the controller decides whether to halt
    dependents.  We verify the handler itself doesn't interfere with the
    state of a sibling task.
    """

    def test_sibling_task_unaffected(self, tmp_path):
        st = StateStore(tmp_path / ".agent")
        st.initialise("goal", tmp_path)
        st.upsert_task(make_task(id="AUTO-T1", title="failing", instruction="x",
                                 target_files=["a.py"]))
        st.upsert_task(make_task(id="AUTO-T2", title="sibling", instruction="y",
                                 target_files=["b.py"]))

        ExhaustionHandler(st).handle(TASK, FakeOuterResult())

        # AUTO-T2 must be untouched
        assert st.get_task("AUTO-T2")["status"] == STATUS_TODO


class TestFactory:
    def test_make_exhaustion_handler(self, tmp_path):
        st  = _state(tmp_path)
        handler = make_exhaustion_handler(st)
        assert isinstance(handler, ExhaustionHandler)

    def test_factory_produces_working_handler(self, tmp_path):
        st  = _state(tmp_path)
        outcome = make_exhaustion_handler(st).handle(TASK, FakeOuterResult())
        assert outcome.knowledge_path.exists()
        assert outcome.ticket_path.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
