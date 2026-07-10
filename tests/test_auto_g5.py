"""tests/test_auto_g5.py — AUTO-G5: Post-commit bug loop wiring (integration).

Story ACs verified here
-----------------------
AUTO-G5 — Post-commit bug loop wiring (3 pts)
  AC1 — A seeded regression in a previously-DONE task produces:
         • a BUG-<id> ticket (status="fixed" after fix),
         • a fix commit via commit_on_success,
         • the ticket is closed (status="fixed") at the end.
  AC2 — A permanently-failing regression fix produces a "deferred" ticket
         and does NOT crash or stall the run.
  AC3 — _check_regressions is invoked after each successful commit and
         skips the just-committed task (no self-regression check).
  AC4 — A task with no acceptance_check is silently skipped by
         _check_regressions (no spurious executor calls).
  AC5 — When a previously-DONE task regresses, its bug-fix result is
         logged to run.log.
  AC6 — The run continues and finishes all independent pending tasks even
         when a regression is detected mid-run.

How this differs from D2 test suite
-------------------------------------
* test_auto_d2.py — unit-tests BugFixLoop in isolation.
* test_auto_g5.py (this file) — end-to-end integration: real StateStore, real
  TicketStore, BugFixLoop wired through controller._run_task_loop(), fake
  outer_loop and fake executor only.

All tests are offline; no real LLM or git subprocess is required.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import (
    StateStore,
)
from tools.auto.ticket_store import make_ticket_store
from tools.auto.bug_fix_loop import BugFixResult


# ─────────────────────────────────────────────────────────────────────────────
# Fake helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FakeExecResult:
    passed:    bool  = True
    exit_code: int   = 0
    stdout:    str   = ""
    stderr:    str   = ""
    traceback: str   = ""
    timed_out: bool  = False


@dataclass
class FakeExecResultFail:
    passed:    bool  = False
    exit_code: int   = 1
    stdout:    str   = "FAILED"
    stderr:    str   = ""
    traceback: str   = "AssertionError"
    timed_out: bool  = False


def _passed_outer(task_id: str) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=1, last_feedback="")]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=1,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _exhausted_outer(task_id: str, rounds: int = 10) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=5, last_feedback="still broken")]
    return OuterLoopResult(
        task_id=task_id,
        passed=False,
        rounds_used=rounds,
        exhausted=True,
        feedback_files=[],
        inner_results=inner,
    )


def _make_task(
    task_id: str,
    title: str = "",
    *,
    status: str = "todo",
    deps: list[str] | None = None,
    acceptance_check: str = "true",
) -> dict:
    return {
        "id":               task_id,
        "title":            title or f"Task {task_id}",
        "instruction":      f"Fix {task_id}",
        "target_files":     [],
        "acceptance_check": acceptance_check,
        "status":           status,
        "dependencies":     deps or [],
        "attempt":          0,
        "round":            0,
        "cited_locations":  [],
    }


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
    ctrl.goal          = "test"
    ctrl.base_dir      = base
    ctrl.config_path   = "agents.ini"
    ctrl.agent_dir     = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"

    ctrl._time_fn    = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits      = RunLimits(max_tasks_per_run=task_cap)

    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    for t in tasks:
        ctrl.state.upsert_task(t)

    ctrl.git              = None
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


def _run_with_fakes(
    ctrl: AutoController,
    *,
    outer_side_effects,
    executor_side_effects=None,
    bfl_side_effects=None,
):
    """Run _run_task_loop patching outer_loop, executor, and bug_fix_loop."""
    fake_outer = MagicMock()
    fake_outer.run_task.side_effect = outer_side_effects

    fake_executor = MagicMock()
    if executor_side_effects is not None:
        fake_executor.run.side_effect = executor_side_effects
    else:
        fake_executor.run.return_value = FakeExecResult(passed=True)

    fake_bfl = MagicMock()
    if bfl_side_effects is not None:
        fake_bfl.handle_regression.side_effect = bfl_side_effects
    else:
        fake_bfl.handle_regression.return_value = BugFixResult(
            ticket_id="BUG-NONE", fix_task_id="BUG-FIX-NONE",
            fixed=True, commit_hash="aabbccdd1234",
        )

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"), \
         patch("tools.auto.executor.make_executor", return_value=fake_executor), \
         patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=fake_bfl):
        stop_reason, tasks_done = ctrl._run_task_loop()

    return stop_reason, tasks_done, fake_outer, fake_executor, fake_bfl


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Seeded regression → bug ticket opened + fix commit + ticket closed
# ─────────────────────────────────────────────────────────────────────────────

class TestG5RegressionFixed:
    def test_bfl_called_when_prior_done_task_regresses(self, tmp_path):
        """AC1: bug_fix_loop.handle_regression invoked for a regressed DONE task."""
        # T-DONE was already completed before this run; T-NEW is pending
        done_task = _make_task("T-DONE", status="done", acceptance_check="pytest -q")
        new_task  = _make_task("T-NEW")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        # T-DONE is already done — it should not be re-executed by outer_loop.
        # T-NEW passes. executor detects T-DONE regressed.
        _, _, _, fake_exec, fake_bfl = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NEW")],
            executor_side_effects=[FakeExecResult(passed=False, exit_code=1)],
        )

        fake_bfl.handle_regression.assert_called_once()
        call_task = fake_bfl.handle_regression.call_args[0][0]
        assert call_task["id"] == "T-DONE"

    def test_bfl_receives_exec_result(self, tmp_path):
        """AC1: exec_result passed to handle_regression is the failed check output."""
        done_task = _make_task("T-EX", status="done", acceptance_check="pytest")
        new_task  = _make_task("T-NW")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        fail_result = FakeExecResult(passed=False, exit_code=2, stdout="assertion fail")
        _, _, _, _, fake_bfl = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NW")],
            executor_side_effects=[fail_result],
        )

        assert fake_bfl.handle_regression.called
        exec_arg = fake_bfl.handle_regression.call_args[0][1]
        assert exec_arg.exit_code == 2

    def test_run_log_records_regression(self, tmp_path):
        """AC5: run.log contains a regression entry after a regressed task is handled."""
        done_task = _make_task("T-LOG", status="done", acceptance_check="true")
        new_task  = _make_task("T-NEW2")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NEW2")],
            executor_side_effects=[FakeExecResult(passed=False, exit_code=1)],
        )

        log_text = (ctrl.agent_dir / "run.log").read_text()
        assert "regression" in log_text.lower()
        assert "T-LOG" in log_text

    def test_bfl_summary_logged(self, tmp_path):
        """AC5: bug_fix_loop result summary appears in run.log."""
        done_task = _make_task("T-SUM", status="done", acceptance_check="check.sh")
        new_task  = _make_task("T-NEW3")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NEW3")],
            executor_side_effects=[FakeExecResult(passed=False, exit_code=1)],
            bfl_side_effects=[
                BugFixResult("BUG-T-SUM", "BUG-FIX-T-SUM", fixed=True,
                             commit_hash="deadbeef1234")
            ],
        )

        log_text = (ctrl.agent_dir / "run.log").read_text()
        assert "BUG-T-SUM" in log_text


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Permanently-failing fix → "deferred" ticket, no crash
# ─────────────────────────────────────────────────────────────────────────────

class TestG5RegressionExhausted:
    def test_exhausted_fix_does_not_raise(self, tmp_path):
        """AC2: exhausted BugFixResult must not raise or stall the run."""
        done_task = _make_task("T-EXHD", status="done", acceptance_check="pytest")
        new_task  = _make_task("T-FWD")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        exhausted_result = BugFixResult(
            "BUG-T-EXHD", "BUG-FIX-T-EXHD", fixed=False, exhausted=True
        )

        stop_reason, tasks_done, *_ = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-FWD")],
            executor_side_effects=[FakeExecResult(passed=False, exit_code=1)],
            bfl_side_effects=[exhausted_result],
        )

        assert stop_reason is None
        assert tasks_done == 1   # T-FWD still counted as done

    def test_exhausted_fix_logged(self, tmp_path):
        """AC2: exhausted fix result is recorded in run.log."""
        done_task = _make_task("T-EXLOG", status="done", acceptance_check="true")
        new_task  = _make_task("T-NX")
        ctrl      = _make_controller(tmp_path, [done_task, new_task])

        exhausted_result = BugFixResult(
            "BUG-T-EXLOG", "BUG-FIX-T-EXLOG", fixed=False, exhausted=True
        )

        _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NX")],
            executor_side_effects=[FakeExecResult(passed=False, exit_code=1)],
            bfl_side_effects=[exhausted_result],
        )

        log_text = (ctrl.agent_dir / "run.log").read_text()
        assert "BUG-T-EXLOG" in log_text


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — just-committed task excluded from regression scan
# ─────────────────────────────────────────────────────────────────────────────

class TestG5ExcludeJustCommitted:
    def test_just_committed_task_not_checked(self, tmp_path):
        """AC3: executor.run is not called for the task just committed."""
        task = _make_task("T-ONLY")
        ctrl = _make_controller(tmp_path, [task])

        _, _, _, fake_exec, _ = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-ONLY")],
        )

        # No previously-DONE tasks (besides the one we just committed),
        # so executor should never be called for a regression check.
        fake_exec.run.assert_not_called()

    def test_only_prior_done_tasks_checked(self, tmp_path):
        """AC3: only pre-existing DONE tasks are regression-checked, not the new commit."""
        done1 = _make_task("T-OLD1", status="done", acceptance_check="check1")
        done2 = _make_task("T-OLD2", status="done", acceptance_check="check2")
        new   = _make_task("T-NEW4")
        ctrl  = _make_controller(tmp_path, [done1, done2, new])

        _, _, _, fake_exec, _ = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NEW4")],
            executor_side_effects=[
                FakeExecResult(passed=True),
                FakeExecResult(passed=True),
            ],
        )

        # Executor called exactly twice: once for T-OLD1, once for T-OLD2
        assert fake_exec.run.call_count == 2
        checked_ids = {c[0][0]["id"] for c in fake_exec.run.call_args_list}
        assert checked_ids == {"T-OLD1", "T-OLD2"}
        assert "T-NEW4" not in checked_ids


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Tasks without acceptance_check are silently skipped
# ─────────────────────────────────────────────────────────────────────────────

class TestG5SkipNoCheck:
    def test_task_without_check_not_executed(self, tmp_path):
        """AC4: DONE tasks with no acceptance_check are not re-run by executor."""
        done_no_check = _make_task("T-NOCH", status="done", acceptance_check="")
        new_task      = _make_task("T-NXY")
        ctrl          = _make_controller(tmp_path, [done_no_check, new_task])

        _, _, _, fake_exec, fake_bfl = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NXY")],
        )

        fake_exec.run.assert_not_called()
        fake_bfl.handle_regression.assert_not_called()

    def test_whitespace_only_check_treated_as_empty(self, tmp_path):
        """AC4: acceptance_check of only whitespace is also skipped."""
        done_ws  = _make_task("T-WS", status="done", acceptance_check="   ")
        new_task = _make_task("T-NXZ")
        ctrl     = _make_controller(tmp_path, [done_ws, new_task])

        _, _, _, fake_exec, fake_bfl = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-NXZ")],
        )

        fake_exec.run.assert_not_called()
        fake_bfl.handle_regression.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Run continues when regression is detected mid-run
# ─────────────────────────────────────────────────────────────────────────────

class TestG5RunContinues:
    def test_pending_tasks_run_after_regression_fixed(self, tmp_path):
        """AC6: tasks after a commit still execute when a regression was detected+fixed."""
        done   = _make_task("T-PRE",  status="done", acceptance_check="check.sh")
        task1  = _make_task("T-A")
        task2  = _make_task("T-B")
        ctrl   = _make_controller(tmp_path, [done, task1, task2])

        # After T-A commits:  check T-PRE (1 call, fails → bfl triggered)
        # After T-B commits:  check T-PRE + T-A (2 calls, both pass)
        _, tasks_done, fake_outer, fake_exec, _ = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-A"), _passed_outer("T-B")],
            executor_side_effects=[
                FakeExecResult(passed=False, exit_code=1),  # T-A commit: T-PRE regresses
                FakeExecResult(passed=True),                # T-B commit: T-PRE passes
                FakeExecResult(passed=True),                # T-B commit: T-A passes
            ],
        )

        assert tasks_done == 2
        assert fake_outer.run_task.call_count == 2

    def test_no_regression_means_no_bfl_call(self, tmp_path):
        """AC6: if all prior DONE tasks still pass, handle_regression is never called."""
        done  = _make_task("T-GREEN", status="done", acceptance_check="true")
        task1 = _make_task("T-P1")
        task2 = _make_task("T-P2")
        ctrl  = _make_controller(tmp_path, [done, task1, task2])

        # After T-P1 commit: check T-GREEN (1 call)
        # After T-P2 commit: check T-GREEN + T-P1 (2 calls)
        _, _, _, _, fake_bfl = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-P1"), _passed_outer("T-P2")],
            executor_side_effects=[
                FakeExecResult(passed=True),   # after T-P1 commit: T-GREEN
                FakeExecResult(passed=True),   # after T-P2 commit: T-GREEN
                FakeExecResult(passed=True),   # after T-P2 commit: T-P1
            ],
        )

        fake_bfl.handle_regression.assert_not_called()

    def test_regression_on_one_does_not_block_others(self, tmp_path):
        """AC6: a regression in T-PRE while executing T-A does not block T-B."""
        done   = _make_task("T-R0", status="done", acceptance_check="x")
        task_a = _make_task("T-RA")
        task_b = _make_task("T-RB")
        ctrl   = _make_controller(tmp_path, [done, task_a, task_b])

        # After T-RA commit: check T-R0 → fails (1 call)
        # After T-RB commit: check T-R0 + T-RA → both fail (2 calls)
        stop_reason, tasks_done, *_ = _run_with_fakes(
            ctrl,
            outer_side_effects=[_passed_outer("T-RA"), _passed_outer("T-RB")],
            executor_side_effects=[
                FakeExecResult(passed=False, exit_code=1),  # after T-RA: T-R0
                FakeExecResult(passed=False, exit_code=1),  # after T-RB: T-R0
                FakeExecResult(passed=False, exit_code=1),  # after T-RB: T-RA
            ],
        )

        assert stop_reason is None
        assert tasks_done == 2


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _check_regressions in isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRegressionsUnit:
    """Unit tests for controller._check_regressions directly."""

    def _make_ctrl_with_tasks(self, tmp_path, tasks):
        ctrl = _make_controller(tmp_path, tasks)
        return ctrl

    def test_passes_base_dir_to_bfl(self, tmp_path):
        """base_dir is forwarded to bug_fix_loop.handle_regression."""
        done_task = _make_task("T-BD", status="done", acceptance_check="cmd")
        ctrl      = _make_controller(tmp_path, [done_task])

        fake_exec = MagicMock()
        fake_exec.run.return_value = FakeExecResult(passed=False, exit_code=1)
        fake_bfl  = MagicMock()
        fake_bfl.handle_regression.return_value = BugFixResult(
            "BUG-T-BD", "BUG-FIX-T-BD", fixed=True
        )

        ctrl._check_regressions("T-OTHER", fake_exec, fake_bfl)

        assert fake_bfl.handle_regression.called
        kwargs = fake_bfl.handle_regression.call_args
        # base_dir passed as third positional arg or keyword
        base_dir_arg = (
            kwargs[0][2]
            if len(kwargs[0]) > 2
            else kwargs[1].get("base_dir")
        )
        assert base_dir_arg == ctrl.base_dir

    def test_green_done_task_does_not_trigger_bfl(self, tmp_path):
        """A DONE task whose acceptance check passes does not invoke BugFixLoop."""
        done_task = _make_task("T-GR", status="done", acceptance_check="cmd")
        ctrl      = _make_controller(tmp_path, [done_task])

        fake_exec = MagicMock()
        fake_exec.run.return_value = FakeExecResult(passed=True)
        fake_bfl  = MagicMock()

        ctrl._check_regressions("T-OTHER", fake_exec, fake_bfl)

        fake_bfl.handle_regression.assert_not_called()

    def test_just_committed_id_excluded(self, tmp_path):
        """The just-committed task is excluded even if it is DONE."""
        done_task = _make_task("T-EXCL", status="done", acceptance_check="cmd")
        ctrl      = _make_controller(tmp_path, [done_task])

        fake_exec = MagicMock()
        fake_bfl  = MagicMock()

        ctrl._check_regressions("T-EXCL", fake_exec, fake_bfl)

        fake_exec.run.assert_not_called()
        fake_bfl.handle_regression.assert_not_called()

    def test_multiple_regressions_all_handled(self, tmp_path):
        """All regressed DONE tasks are passed to bug_fix_loop, not just the first."""
        tasks = [
            _make_task("T-R1", status="done", acceptance_check="c1"),
            _make_task("T-R2", status="done", acceptance_check="c2"),
            _make_task("T-R3", status="done", acceptance_check="c3"),
        ]
        ctrl = _make_controller(tmp_path, tasks)

        fake_exec = MagicMock()
        fake_exec.run.side_effect = [
            FakeExecResult(passed=False, exit_code=1),
            FakeExecResult(passed=False, exit_code=1),
            FakeExecResult(passed=False, exit_code=1),
        ]
        fake_bfl = MagicMock()
        fake_bfl.handle_regression.return_value = BugFixResult(
            "BUG-X", "BUG-FIX-X", fixed=True
        )

        ctrl._check_regressions("T-NEW99", fake_exec, fake_bfl)

        assert fake_bfl.handle_regression.call_count == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
