"""tests/test_auto_g4.py — AUTO-G4: Exhaustion → knowledge + ticket wiring (integration).

Story ACs verified here
-----------------------
AUTO-G4 — Exhaustion → knowledge + ticket wiring (3 pts)
  AC1 — A permanently-failing task produces a knowledge note on disk at
         ``.agent/tasks/<id>/knowledge.md`` containing task info + feedback.
  AC2 — An investigation ticket is written to
         ``.agent/tickets/TICKET-<id>.json`` with the correct schema fields.
  AC3 — The exhausted task is set to STATUS_BLOCKED in plan.json.
  AC4 — The run continues with independent tasks after exhaustion (does not
         stall); only tasks with a blocked dependency are themselves blocked.
  AC5 — run.log records both the exhaustion event and the ticket id.
  AC6 — Resume safety: re-running after exhaustion does not duplicate the
         ticket (idempotent on re-entry).

How this differs from C6 and D1 test suites
--------------------------------------------
* test_auto_c6.py — unit-tests ExhaustionHandler in isolation (fake state).
* test_auto_d1.py — unit-tests TicketStore CRUD in isolation.
* test_auto_g4.py (this file) — end-to-end integration: real StateStore, real
  TicketStore, real ExhaustionHandler wired through controller._run_task_loop(),
  fake outer_loop only.

All tests are offline; no real LLM or git subprocess is required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import (
    StateStore,
    STATUS_BLOCKED,
    STATUS_DONE,
)
from tools.auto.ticket_store import make_ticket_store


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_task(
    task_id: str,
    title: str = "",
    *,
    deps: list[str] | None = None,
    acceptance_check: str = "true",
) -> dict:
    return {
        "id":               task_id,
        "title":            title or f"Task {task_id}",
        "instruction":      f"Fix {task_id}",
        "target_files":     [],
        "acceptance_check": acceptance_check,
        "status":           "todo",
        "dependencies":     deps or [],
        "attempt":          0,
        "round":            0,
        "cited_locations":  [],
    }


def _passed_result(task_id: str) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=1, last_feedback="")]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=1,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _exhausted_result(
    task_id: str,
    rounds: int = 10,
    feedback_files: list[str] | None = None,
) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=5, last_feedback="still broken")]
    return OuterLoopResult(
        task_id=task_id,
        passed=False,
        rounds_used=rounds,
        exhausted=True,
        feedback_files=feedback_files or [],
        inner_results=inner,
    )


def _make_controller(
    tmp_path: Path,
    tasks: list[dict],
    *,
    task_cap: int = 0,
) -> AutoController:
    """Build a minimal controller with real StateStore; no real git."""
    base = tmp_path / "repo"
    base.mkdir()

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal        = "test"
    ctrl.base_dir    = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir   = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"

    import time
    ctrl._time_fn    = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits      = RunLimits(max_tasks_per_run=task_cap)

    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    for t in tasks:
        ctrl.state.upsert_task(t)

    ctrl.git              = None   # no git needed for G4
    ctrl.run_trace        = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream   = MagicMock()
    ctrl.auto_tuner       = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )

    return ctrl


def _ticket_store(ctrl: AutoController):
    return make_ticket_store(ctrl.agent_dir)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Knowledge note written to disk
# ─────────────────────────────────────────────────────────────────────────────

class TestG4KnowledgeNote:
    def test_knowledge_file_created(self, tmp_path):
        """AC1: .agent/tasks/<id>/knowledge.md is written after exhaustion."""
        task = _make_task("T-EX", "Fix the thing")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-EX")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        knowledge_path = ctrl.agent_dir / "tasks" / "T-EX" / "knowledge.md"
        assert knowledge_path.exists(), "knowledge.md not found"

    def test_knowledge_contains_task_title(self, tmp_path):
        """AC1: knowledge note includes the task title."""
        task = _make_task("T-KN", "Improve the parser")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-KN")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        content = (ctrl.agent_dir / "tasks" / "T-KN" / "knowledge.md").read_text()
        assert "Improve the parser" in content

    def test_knowledge_contains_task_instruction(self, tmp_path):
        """AC1: knowledge note includes the instruction."""
        task = _make_task("T-KI", "Handle edge case")
        task["instruction"] = "Rewrite the edge-case branch to use early return."
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-KI")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        content = (ctrl.agent_dir / "tasks" / "T-KI" / "knowledge.md").read_text()
        assert "early return" in content

    def test_knowledge_contains_acceptance_check(self, tmp_path):
        """AC1: knowledge note includes the acceptance check command."""
        task = _make_task("T-KA", "Pass tests", acceptance_check="pytest tests/ -q")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-KA")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        content = (ctrl.agent_dir / "tasks" / "T-KA" / "knowledge.md").read_text()
        assert "pytest tests/ -q" in content

    def test_knowledge_contains_round_feedback(self, tmp_path):
        """AC1: feedback text from feedback_files appears in knowledge note."""
        task = _make_task("T-KF", "Feedback task")
        ctrl = _make_controller(tmp_path, [task])

        # Write a real feedback file the OuterLoopResult can read
        feedback_dir = ctrl.agent_dir / "tasks" / "T-KF"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        fb_path = feedback_dir / "round_1_feedback.txt"
        fb_path.write_text("TypeError: unsupported operand type\n")

        result = _exhausted_result("T-KF", feedback_files=[str(fb_path)])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = result

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        content = (feedback_dir / "knowledge.md").read_text()
        assert "TypeError: unsupported operand type" in content


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Investigation ticket written with correct schema
# ─────────────────────────────────────────────────────────────────────────────

class TestG4Ticket:
    def _run_exhausted(self, tmp_path: Path, task_id: str, title: str = ""):
        task = _make_task(task_id, title or f"Task {task_id}")
        ctrl = _make_controller(tmp_path, [task])
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result(task_id)
        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()
        return ctrl

    def test_ticket_file_exists(self, tmp_path):
        """AC2: .agent/tickets/TICKET-<id>.json is created."""
        ctrl = self._run_exhausted(tmp_path, "T-TK")
        ticket_path = ctrl.agent_dir / "tickets" / "TICKET-T-TK.json"
        assert ticket_path.exists(), "Ticket file not created"

    def test_ticket_schema_fields(self, tmp_path):
        """AC2: ticket JSON has all required schema fields."""
        ctrl = self._run_exhausted(tmp_path, "T-SCHEMA")
        raw = (ctrl.agent_dir / "tickets" / "TICKET-T-SCHEMA.json").read_text()
        ticket = json.loads(raw)

        for field in ("id", "type", "status", "linked_task",
                      "title", "body", "created_at", "updated_at"):
            assert field in ticket, f"Missing ticket field: {field}"

    def test_ticket_id_format(self, tmp_path):
        """AC2: ticket id is TICKET-<task_id>."""
        ctrl = self._run_exhausted(tmp_path, "T-ID")
        ts = _ticket_store(ctrl)
        ticket = ts.get("TICKET-T-ID")
        assert ticket is not None
        assert ticket["id"] == "TICKET-T-ID"

    def test_ticket_type_is_investigation(self, tmp_path):
        """AC2: ticket type is 'investigation'."""
        ctrl = self._run_exhausted(tmp_path, "T-TYPE")
        ticket = _ticket_store(ctrl).get("TICKET-T-TYPE")
        assert ticket["type"] == "investigation"

    def test_ticket_status_is_open(self, tmp_path):
        """AC2: ticket status is 'open' on creation."""
        ctrl = self._run_exhausted(tmp_path, "T-STAT")
        ticket = _ticket_store(ctrl).get("TICKET-T-STAT")
        assert ticket["status"] == "open"

    def test_ticket_linked_task(self, tmp_path):
        """AC2: ticket linked_task matches the exhausted task id."""
        ctrl = self._run_exhausted(tmp_path, "T-LINK")
        ticket = _ticket_store(ctrl).get("TICKET-T-LINK")
        assert ticket["linked_task"] == "T-LINK"

    def test_ticket_title_contains_task_title(self, tmp_path):
        """AC2: ticket title prefixed with 'Deferred:' includes the task title."""
        ctrl = self._run_exhausted(tmp_path, "T-TTITLE", "Optimise the hot path")
        ticket = _ticket_store(ctrl).get("TICKET-T-TTITLE")
        assert "Deferred" in ticket["title"]
        assert "Optimise the hot path" in ticket["title"]

    def test_ticket_body_is_non_empty(self, tmp_path):
        """AC2: ticket body is non-empty (contains knowledge text)."""
        ctrl = self._run_exhausted(tmp_path, "T-BODY")
        ticket = _ticket_store(ctrl).get("TICKET-T-BODY")
        assert ticket["body"].strip(), "Ticket body is empty"

    def test_ticket_readable_via_ticket_store(self, tmp_path):
        """AC2: ticket is retrievable via TicketStore.get() after the run."""
        ctrl = self._run_exhausted(tmp_path, "T-READ")
        ts = _ticket_store(ctrl)
        assert ts.exists("TICKET-T-READ")
        ticket = ts.get("TICKET-T-READ")
        assert ticket is not None


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Exhausted task is set to STATUS_BLOCKED
# ─────────────────────────────────────────────────────────────────────────────

class TestG4TaskBlocked:
    def test_exhausted_task_status_is_blocked(self, tmp_path):
        """AC3: plan.json records STATUS_BLOCKED for the exhausted task."""
        task = _make_task("T-BLK")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-BLK")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        t = ctrl.state.get_task("T-BLK")
        assert t["status"] == STATUS_BLOCKED

    def test_passed_task_not_blocked(self, tmp_path):
        """AC3: a passing task is DONE, not BLOCKED."""
        tasks = [_make_task("T-PASS"), _make_task("T-FAIL")]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _passed_result("T-PASS"),
            _exhausted_result("T-FAIL"),
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        assert ctrl.state.get_task("T-PASS")["status"] == STATUS_DONE
        assert ctrl.state.get_task("T-FAIL")["status"] == STATUS_BLOCKED


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Run continues; dependent tasks are blocked, independents run
# ─────────────────────────────────────────────────────────────────────────────

class TestG4RunContinues:
    def test_independent_task_runs_after_exhaustion(self, tmp_path):
        """AC4: independent task executes after an exhausted one."""
        tasks = [_make_task("T-FAIL"), _make_task("T-INDEP")]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _exhausted_result("T-FAIL"),
            _passed_result("T-INDEP"),
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 1
        assert fake_outer.run_task.call_count == 2
        assert ctrl.state.get_task("T-INDEP")["status"] == STATUS_DONE

    def test_multiple_exhausted_then_pass(self, tmp_path):
        """AC4: two exhausted tasks followed by two passing tasks — both passing tasks complete."""
        tasks = [
            _make_task("F-1"), _make_task("F-2"),
            _make_task("P-1"), _make_task("P-2"),
        ]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _exhausted_result("F-1"),
            _exhausted_result("F-2"),
            _passed_result("P-1"),
            _passed_result("P-2"),
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            _, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 2
        assert ctrl.state.get_task("F-1")["status"] == STATUS_BLOCKED
        assert ctrl.state.get_task("F-2")["status"] == STATUS_BLOCKED
        assert ctrl.state.get_task("P-1")["status"] == STATUS_DONE
        assert ctrl.state.get_task("P-2")["status"] == STATUS_DONE

    def test_dependent_task_blocked_when_dep_exhausted(self, tmp_path):
        """AC4: a task whose dep is BLOCKED is itself skipped (set BLOCKED)."""
        parent = _make_task("T-PAR")
        child  = _make_task("T-CHD", deps=["T-PAR"])
        ctrl   = _make_controller(tmp_path, [parent, child])

        fake_outer = MagicMock()
        # Parent exhausts; child should be blocked by dependency guard
        fake_outer.run_task.return_value = _exhausted_result("T-PAR")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 0
        assert ctrl.state.get_task("T-PAR")["status"] == STATUS_BLOCKED
        assert ctrl.state.get_task("T-CHD")["status"] == STATUS_BLOCKED
        # outer_loop only called once (for parent); child never executed
        assert fake_outer.run_task.call_count == 1

    def test_sibling_independent_runs_when_dep_exhausted(self, tmp_path):
        """AC4: sibling with no dep runs even when another branch is blocked."""
        parent  = _make_task("T-PAR2")
        child   = _make_task("T-CHD2", deps=["T-PAR2"])
        sibling = _make_task("T-SIB2")
        ctrl    = _make_controller(tmp_path, [parent, child, sibling])

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _exhausted_result("T-PAR2"),
            _passed_result("T-SIB2"),
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            _, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 1
        assert ctrl.state.get_task("T-PAR2")["status"] == STATUS_BLOCKED
        assert ctrl.state.get_task("T-CHD2")["status"] == STATUS_BLOCKED
        assert ctrl.state.get_task("T-SIB2")["status"] == STATUS_DONE

    def test_exhausted_tasks_not_counted_as_done(self, tmp_path):
        """AC4: tasks_done counter only increments for passed tasks."""
        tasks = [_make_task(f"T-{i}") for i in range(4)]
        ctrl  = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _exhausted_result("T-0"),
            _passed_result("T-1"),
            _exhausted_result("T-2"),
            _passed_result("T-3"),
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            _, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — run.log records the event
# ─────────────────────────────────────────────────────────────────────────────

class TestG4Logging:
    def test_log_contains_ticket_id(self, tmp_path):
        """AC5: run.log records the ticket id after exhaustion."""
        task = _make_task("T-LOG")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-LOG")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        log = (ctrl.agent_dir / "run.log").read_text()
        assert "TICKET-T-LOG" in log

    def test_log_contains_task_id(self, tmp_path):
        """AC5: run.log records the exhausted task id."""
        task = _make_task("T-LOGID")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-LOGID")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        log = (ctrl.agent_dir / "run.log").read_text()
        assert "T-LOGID" in log

    def test_run_trace_log_task_blocked_called(self, tmp_path):
        """AC5: run_trace.log_task_blocked() is called for the exhausted task."""
        task = _make_task("T-TRC")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-TRC")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_blocked.assert_called_once()
        ctrl.run_trace.log_task_done.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Idempotence on resume (no duplicate ticket)
# ─────────────────────────────────────────────────────────────────────────────

class TestG4Idempotence:
    def test_second_run_does_not_duplicate_ticket(self, tmp_path):
        """AC6: running exhaustion handler twice for the same task id is safe."""
        task = _make_task("T-IDEM")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_result("T-IDEM")

        # First run
        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"):
            ctrl._run_task_loop()

        ts = _ticket_store(ctrl)
        assert ts.exists("TICKET-T-IDEM")

        # Simulate re-run: reset task status to todo, re-queue it
        # (In practice the controller skips BLOCKED tasks on resume,
        #  but test the handler's own idempotency directly)
        from tools.auto.exhaustion_handler import ExhaustionHandler
        handler = ExhaustionHandler(ctrl.state)
        result  = _exhausted_result("T-IDEM")

        # Should not raise TicketAlreadyExists
        outcome = handler.handle(task, result)
        assert outcome.ticket_id == "TICKET-T-IDEM"

        # Still exactly one ticket on disk
        all_tickets = ts.list_all()
        idem_tickets = [t for t in all_tickets if t["linked_task"] == "T-IDEM"]
        assert len(idem_tickets) == 1

    def test_knowledge_file_overwritten_on_second_handle(self, tmp_path):
        """AC6: re-running handle() overwrites knowledge.md (no stale content appended)."""
        task = _make_task("T-KOW")
        ctrl = _make_controller(tmp_path, [task])

        from tools.auto.exhaustion_handler import ExhaustionHandler
        handler = ExhaustionHandler(ctrl.state)

        handler.handle(task, _exhausted_result("T-KOW"))
        handler.handle(task, _exhausted_result("T-KOW"))

        kpath = ctrl.agent_dir / "tasks" / "T-KOW" / "knowledge.md"
        content = kpath.read_text()
        # Header should appear exactly once (not appended twice)
        assert content.count("Deferred Investigation") == 1
