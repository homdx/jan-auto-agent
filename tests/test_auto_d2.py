"""tests/test_auto_d2.py — AUTO-D2: post-commit bug detection and fix loop.

ACs (from the Jira story):
  * A seeded regression produces a ticket, a fix commit, and a closed ticket.
  * A permanently-failing fix produces a "deferred" ticket, not a crash.
  * An already-fixed ticket is detected and skipped (idempotent).
  * The fix task is registered in the StateStore so it is resumable.
  * The outer loop is invoked exactly once per handle_regression call.
  * Ticket body contains the acceptance check command, exit code, and output.
  * Commit-on-success is called iff the fix loop passes.
  * BugFixResult carries the correct ticket_id, fix_task_id, and fixed flag.
  * make_bug_fix_loop factory builds a BugFixLoop without errors.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.bug_fix_loop import BugFixLoop, BugFixResult, make_bug_fix_loop
from tools.auto.ticket_store import TicketStore, make_ticket_store
from tools.auto.state import StateStore


# ── Fake helpers ─────────────────────────────────────────────────────────────

@dataclass
class FakeExecResult:
    passed:    bool = False
    exit_code: int = 1
    stdout:    str = "FAILED: assertion error"
    stderr:    str = ""
    traceback: str = "Traceback (most recent call last):\n  AssertionError"
    timed_out: bool = False


@dataclass
class FakeOuterResult:
    task_id:    str   = "BUG-FIX-AUTO-T1"
    passed:     bool  = False
    exhausted:  bool  = False
    rounds_used: int  = 1
    feedback_files: list = dc_field(default_factory=list)

    def knowledge(self) -> str:
        return "Round 1: assertion still fails"


def _make_triggering_task(id="AUTO-T1", title="Add cache layer"):
    return {
        "id": id,
        "title": title,
        "instruction": "Add a cache to the reader.",
        "target_files": ["tools/file_reader.py"],
        "acceptance_check": "pytest tests/test_reader.py -q",
    }


def _make_state(tmp_path: Path) -> StateStore:
    agent_dir = tmp_path / ".agent"
    st = StateStore(agent_dir)
    st.initialise("fix regressions", tmp_path)
    return st


def _make_tickets(tmp_path: Path) -> TicketStore:
    return make_ticket_store(tmp_path / ".agent")


def _make_outer(result: FakeOuterResult):
    outer = MagicMock()
    outer.run_task.return_value = result
    return outer


def _make_cos(sha: Optional[str] = "abc123def456"):
    cos = MagicMock()
    cos.commit.return_value = sha
    return cos


def _make_bfl(
    tmp_path: Path,
    outer_result: FakeOuterResult,
    commit_sha: Optional[str] = "abc123def456",
) -> tuple[BugFixLoop, TicketStore, StateStore]:
    state   = _make_state(tmp_path)
    tickets = _make_tickets(tmp_path)
    outer   = _make_outer(outer_result)
    cos     = _make_cos(commit_sha)
    bfl = BugFixLoop(outer, cos, tickets, state)
    return bfl, tickets, state


# ── BugFixResult ──────────────────────────────────────────────────────────────

class TestBugFixResult:
    def test_summary_fixed(self):
        r = BugFixResult("BUG-T1", "BUG-FIX-T1", fixed=True, commit_hash="abc123")
        assert "FIXED" in r.summary()
        assert "abc123" in r.summary()

    def test_summary_exhausted(self):
        r = BugFixResult("BUG-T1", "BUG-FIX-T1", fixed=False, exhausted=True)
        assert "EXHAUSTED" in r.summary()

    def test_summary_skipped(self):
        r = BugFixResult("BUG-T1", "BUG-FIX-T1", fixed=True, skipped=True)
        assert "skipped" in r.summary()


# ── handle_regression: happy path (fix passes) ───────────────────────────────

class TestHandleRegressionFixed:
    def test_returns_fixed_true(self, tmp_path):
        bfl, tickets, state = _make_bfl(
            tmp_path, FakeOuterResult(passed=True, exhausted=False)
        )
        result = bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        assert result.fixed is True
        assert result.exhausted is False
        assert result.skipped is False

    def test_commit_on_success_called(self, tmp_path):
        outer  = _make_outer(FakeOuterResult(passed=True))
        cos    = _make_cos("deadbeef1234")
        state  = _make_state(tmp_path)
        tickets = _make_tickets(tmp_path)
        bfl    = BugFixLoop(outer, cos, tickets, state)

        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        cos.commit.assert_called_once()

    def test_commit_hash_in_result(self, tmp_path):
        bfl, _, _ = _make_bfl(
            tmp_path, FakeOuterResult(passed=True), commit_sha="feed1234cafe"
        )
        result = bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        assert result.commit_hash == "feed1234cafe"

    def test_ticket_status_set_to_fixed(self, tmp_path):
        bfl, tickets, _ = _make_bfl(
            tmp_path, FakeOuterResult(passed=True)
        )
        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        ticket = tickets.get("BUG-AUTO-T1")
        assert ticket is not None
        assert ticket["status"] == "fixed"

    def test_ticket_ids(self, tmp_path):
        bfl, _, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        result = bfl.handle_regression(
            _make_triggering_task("AUTO-T3"), FakeExecResult(), base_dir=tmp_path
        )
        assert result.ticket_id == "BUG-AUTO-T3"
        assert result.fix_task_id == "BUG-FIX-AUTO-T3"


# ── handle_regression: fix exhausted ─────────────────────────────────────────

class TestHandleRegressionExhausted:
    def test_returns_fixed_false_exhausted_true(self, tmp_path):
        bfl, _, _ = _make_bfl(
            tmp_path,
            FakeOuterResult(passed=False, exhausted=True, rounds_used=10)
        )
        result = bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        assert result.fixed is False
        assert result.exhausted is True

    def test_commit_not_called_on_exhaustion(self, tmp_path):
        outer  = _make_outer(FakeOuterResult(passed=False, exhausted=True))
        cos    = _make_cos()
        state  = _make_state(tmp_path)
        tickets = _make_tickets(tmp_path)
        bfl    = BugFixLoop(outer, cos, tickets, state)

        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        cos.commit.assert_not_called()

    def test_ticket_status_deferred(self, tmp_path):
        bfl, tickets, _ = _make_bfl(
            tmp_path,
            FakeOuterResult(passed=False, exhausted=True)
        )
        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        ticket = tickets.get("BUG-AUTO-T1")
        assert ticket is not None
        assert ticket["status"] == "deferred"

    def test_knowledge_appended_to_ticket_body(self, tmp_path):
        bfl, tickets, _ = _make_bfl(
            tmp_path,
            FakeOuterResult(passed=False, exhausted=True)
        )
        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert "assertion still fails" in body


# ── ticket content ────────────────────────────────────────────────────────────

class TestTicketContent:
    def test_ticket_type_is_bug(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        ticket = tickets.get("BUG-AUTO-T1")
        assert ticket["type"] == "bug"

    def test_ticket_linked_task(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task("AUTO-T7"), FakeExecResult(), base_dir=tmp_path
        )
        ticket = tickets.get("BUG-AUTO-T7")
        assert ticket["linked_task"] == "AUTO-T7"

    def test_ticket_body_contains_acceptance_check(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        task = _make_triggering_task()
        bfl.handle_regression(task, FakeExecResult(), base_dir=tmp_path)
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert task["acceptance_check"] in body

    def test_ticket_body_contains_exit_code(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task(),
            FakeExecResult(exit_code=2),
            base_dir=tmp_path,
        )
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert "2" in body

    def test_ticket_body_contains_stdout(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task(),
            FakeExecResult(stdout="assertion error on line 42"),
            base_dir=tmp_path,
        )
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert "assertion error on line 42" in body

    def test_ticket_body_contains_traceback(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task(),
            FakeExecResult(traceback="AssertionError: expected 1 got 0"),
            base_dir=tmp_path,
        )
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert "AssertionError" in body


# ── fix task registration ─────────────────────────────────────────────────────

class TestFixTaskRegistration:
    def test_fix_task_registered_in_state(self, tmp_path):
        bfl, _, state = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task("AUTO-T2"), FakeExecResult(), base_dir=tmp_path
        )
        tasks = {t["id"]: t for t in state.all_tasks()}
        assert "BUG-FIX-AUTO-T2" in tasks

    def test_fix_task_has_acceptance_check(self, tmp_path):
        bfl, _, state = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        task = _make_triggering_task()
        bfl.handle_regression(task, FakeExecResult(), base_dir=tmp_path)
        tasks = {t["id"]: t for t in state.all_tasks()}
        fix = tasks["BUG-FIX-AUTO-T1"]
        assert fix["acceptance_check"] == task["acceptance_check"]

    def test_fix_task_inherits_target_files(self, tmp_path):
        bfl, _, state = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        task = _make_triggering_task()
        bfl.handle_regression(task, FakeExecResult(), base_dir=tmp_path)
        tasks = {t["id"]: t for t in state.all_tasks()}
        fix = tasks["BUG-FIX-AUTO-T1"]
        assert fix["target_files"] == task["target_files"]

    def test_fix_task_instruction_mentions_ticket(self, tmp_path):
        bfl, _, state = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task("AUTO-T5"), FakeExecResult(), base_dir=tmp_path
        )
        tasks = {t["id"]: t for t in state.all_tasks()}
        fix = tasks["BUG-FIX-AUTO-T5"]
        assert "BUG-AUTO-T5" in fix["instruction"]

    def test_outer_loop_called_with_fix_task(self, tmp_path):
        outer   = _make_outer(FakeOuterResult(passed=True))
        cos     = _make_cos()
        state   = _make_state(tmp_path)
        tickets = _make_tickets(tmp_path)
        bfl     = BugFixLoop(outer, cos, tickets, state)

        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        outer.run_task.assert_called_once()
        call_args = outer.run_task.call_args
        fix_task_arg = call_args[0][0]
        assert fix_task_arg["id"] == "BUG-FIX-AUTO-T1"


# ── idempotency ───────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_already_fixed_ticket_skipped(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        task = _make_triggering_task()
        exec_result = FakeExecResult()

        # First call fixes the ticket
        bfl.handle_regression(task, exec_result, base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T1")["status"] == "fixed"

        # Second call must skip without calling outer_loop again
        outer = _make_outer(FakeOuterResult(passed=True))
        cos   = _make_cos()
        bfl2  = BugFixLoop(outer, cos, tickets, bfl._state)
        result = bfl2.handle_regression(task, exec_result, base_dir=tmp_path)

        assert result.skipped is True
        assert result.fixed is True
        outer.run_task.assert_not_called()
        cos.commit.assert_not_called()

    def test_already_deferred_ticket_skipped(self, tmp_path):
        """Regression: a deferred ticket must not re-run the fix loop on every
        subsequent commit. controller._check_regressions re-checks every DONE
        task's acceptance check after every later commit for the rest of the
        run — without this short-circuit, a persistently-failing regression
        re-attempts the full (expensive) OuterLoop fix cycle from scratch on
        every single one of those re-checks, forever."""
        bfl, tickets, _ = _make_bfl(
            tmp_path, FakeOuterResult(passed=False, exhausted=True)
        )
        task = _make_triggering_task()
        exec_result = FakeExecResult()

        # First call exhausts and defers the ticket.
        bfl.handle_regression(task, exec_result, base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T1")["status"] == "deferred"

        # Second call (simulating the NEXT commit's regression re-check) must
        # skip without calling outer_loop again.
        outer = _make_outer(FakeOuterResult(passed=True))
        cos   = _make_cos()
        bfl2  = BugFixLoop(outer, cos, tickets, bfl._state)
        result = bfl2.handle_regression(task, exec_result, base_dir=tmp_path)

        assert result.skipped is True
        assert result.fixed is False
        assert result.exhausted is True
        outer.run_task.assert_not_called()
        cos.commit.assert_not_called()
        # Still exactly one ticket — no duplicate opened.
        assert len(tickets.list_by_task("AUTO-T1")) == 1

    def test_deferred_ticket_retried_after_manual_reset(self, tmp_path):
        """The documented escape hatch: an operator resets status to "open"
        to allow exactly one more attempt (mirrors
        test_existing_open_ticket_reused_not_duplicated's pattern)."""
        bfl, tickets, _ = _make_bfl(
            tmp_path, FakeOuterResult(passed=False, exhausted=True)
        )
        task = _make_triggering_task()
        exec_result = FakeExecResult()

        bfl.handle_regression(task, exec_result, base_dir=tmp_path)
        tickets.update("BUG-AUTO-T1", status="open")

        outer2 = _make_outer(FakeOuterResult(passed=True))
        cos2   = _make_cos()
        bfl2   = BugFixLoop(outer2, cos2, tickets, bfl._state)
        result = bfl2.handle_regression(task, exec_result, base_dir=tmp_path)

        outer2.run_task.assert_called_once()
        assert result.fixed is True

    def test_existing_open_ticket_reused_not_duplicated(self, tmp_path):
        """Second handle_regression on an open ticket must reuse it."""
        bfl, tickets, _ = _make_bfl(
            tmp_path, FakeOuterResult(passed=False, exhausted=True)
        )
        task = _make_triggering_task()
        exec_result = FakeExecResult()

        bfl.handle_regression(task, exec_result, base_dir=tmp_path)
        # Manually reset to "open" for the second call
        tickets.update("BUG-AUTO-T1", status="open")

        outer2  = _make_outer(FakeOuterResult(passed=True))
        cos2    = _make_cos()
        bfl2    = BugFixLoop(outer2, cos2, tickets, bfl._state)
        result  = bfl2.handle_regression(task, exec_result, base_dir=tmp_path)

        assert result.fixed is True
        # Exactly one ticket in total
        assert len(tickets.list_by_task("AUTO-T1")) == 1

    def test_outer_loop_called_once(self, tmp_path):
        outer   = _make_outer(FakeOuterResult(passed=True))
        cos     = _make_cos()
        state   = _make_state(tmp_path)
        tickets = _make_tickets(tmp_path)
        bfl     = BugFixLoop(outer, cos, tickets, state)
        bfl.handle_regression(
            _make_triggering_task(), FakeExecResult(), base_dir=tmp_path
        )
        outer.run_task.assert_called_once()


# ── timed_out flag ────────────────────────────────────────────────────────────

class TestTimedOut:
    def test_timed_out_noted_in_ticket_body(self, tmp_path):
        bfl, tickets, _ = _make_bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(
            _make_triggering_task(),
            FakeExecResult(timed_out=True),
            base_dir=tmp_path,
        )
        body = tickets.get("BUG-AUTO-T1")["body"]
        assert "timed out" in body.lower()


# ── make_bug_fix_loop factory ─────────────────────────────────────────────────

class TestFactory:
    def test_make_bug_fix_loop_returns_instance(self, tmp_path):
        import configparser
        config = configparser.ConfigParser()
        config["auto"] = {
            "max_rounds_per_task":   "2",
            "max_attempts_per_task": "2",
            "exec_timeout_sec":      "30",
            "git_user":  "test-agent",
            "git_email": "test@localhost",
        }
        config["api"] = {"active": "local", "verify_ssl": "false"}
        config["api_local"] = {
            "base_url":   "http://localhost:11434/v1",
            "api_key":    "",
            "model":      "dummy",
            "api_format": "openai",
        }
        state = _make_state(tmp_path)
        tickets = _make_tickets(tmp_path)
        outer   = _make_outer(FakeOuterResult(passed=False))
        cos     = _make_cos()
        bfl = make_bug_fix_loop(
            config, tmp_path, state,
            outer_loop=outer,
            commit_on_success=cos,
            ticket_store=tickets,
        )
        assert isinstance(bfl, BugFixLoop)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
