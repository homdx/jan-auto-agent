"""tests_bugfix/test_bugfix_check_regressions_guarded.py

BUG 2 (check-regressions-guard): ``AutoController._check_regressions`` is
invoked at the post-commit step of ``_run_task_loop`` (controller.py:799)
with no try/except at the call site, and — before this fix — the method's
own for-loop over previously-DONE tasks had no internal guard either.

``_check_regressions`` re-runs every previously-DONE task's acceptance
check and, on a regression, drives a full ``BugFixLoop`` (git + executor +
LLM subprocesses). ``executor.run()`` can raise ``OSError`` (a vanished
workspace), a timeout, or ``GitError``; ``bug_fix_loop.handle_regression()``
can raise for the same reasons plus anything inside the fix loop itself.

Before the fix, any such exception escaped ``_check_regressions`` entirely
and aborted the whole ``--auto`` run: the task that had just been committed
was already fully accounted for, but every task still pending in
``plan.json`` was dropped along with it.

The fix has two layers, and both are pinned here:

1. A call-site guard in ``_run_task_loop`` (defense in depth — catches
   anything that can raise before the internal per-item guard is reached,
   e.g. ``self.state.all_tasks()``).
2. A per-``done_task`` guard *inside* ``_check_regressions`` itself, so one
   failing regression check does not skip the regression check for every
   *other* already-DONE task in the same post-commit batch.

Coverage note: most of the six draft patches for this bug only proved
"does not raise". This file also asserts *how many* done-tasks got
checked (``test_one_failing_done_task_does_not_skip_the_next_one``) —
that is the one behavior that actually distinguishes a call-site-only fix
from the finer-grained fix used here, and none of the drafts pinned it.
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

from tools.auto.bug_fix_loop import BugFixResult  # noqa: E402
from tools.auto.controller import AutoController, RunLimits  # noqa: E402
from tools.auto.git_manager import GitError  # noqa: E402
from tools.auto.outer_loop import OuterLoopResult  # noqa: E402
from tools.auto.state import StateStore, make_task  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeExecResult:
    passed: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


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


@pytest.fixture()
def controller(tmp_path: Path) -> AutoController:
    """Minimal real AutoController (StateStore is real; everything that
    talks to the network/LLMs is mocked at the call sites in each test)."""
    base = tmp_path / "repo"
    base.mkdir()

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits = RunLimits()
    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    ctrl.git = None
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )
    return ctrl


def _done_task(tid: str, acceptance_check: str = "true") -> dict:
    return make_task(
        id=tid,
        title=f"regression target {tid}",
        instruction="echo ok",
        acceptance_check=acceptance_check,
        status="done",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — end-to-end through _run_task_loop (call-site guard)
# ─────────────────────────────────────────────────────────────────────────────


def _run_full_loop(ctrl: AutoController, exc: BaseException):
    """Drive the real _run_task_loop with an executor whose run() always
    raises *exc*. T-DONE is a pre-existing DONE task, so the post-commit
    regression check of the first pending task actually exercises the
    guarded path — that is the code path that used to let *exc* escape and
    abort the whole run."""
    ctrl.state.upsert_task(_done_task("T-DONE", acceptance_check="pytest -q"))
    for tid in ("T-1", "T-2"):
        ctrl.state.upsert_task(make_task(
            id=tid, title=f"Task {tid}", instruction="do it",
            acceptance_check="true",
        ))

    fake_outer = MagicMock()
    fake_outer.run_task.side_effect = [_passed_outer("T-1"), _passed_outer("T-2")]
    fake_executor = MagicMock()
    fake_executor.run.side_effect = exc
    fake_bfl = MagicMock()
    fake_bfl.handle_regression.return_value = BugFixResult(
        ticket_id="BUG-NONE", fix_task_id="BUG-FIX-NONE",
        fixed=True, commit_hash="aabbccdd1234",
    )

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"), \
         patch("tools.auto.executor.make_executor", return_value=fake_executor), \
         patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=fake_bfl):
        stop_reason, tasks_done = ctrl._run_task_loop()

    return stop_reason, tasks_done


class TestFullLoopToleratesRegressionCheckFailure:
    def test_giterror_does_not_abort_the_run(self, controller: AutoController) -> None:
        """A git failure inside the regression check must not kill the loop."""
        stop_reason, tasks_done = _run_full_loop(controller, GitError("index.lock is held"))
        assert stop_reason is None
        assert tasks_done == 2

    def test_oserror_does_not_abort_the_run(self, controller: AutoController) -> None:
        stop_reason, tasks_done = _run_full_loop(controller, OSError("workspace vanished"))
        assert stop_reason is None
        assert tasks_done == 2

    def test_loop_continues_with_remaining_tasks(self, controller: AutoController) -> None:
        """Every pending task is still executed and marked done after the
        regression-check failure — the crash used to drop them all."""
        _, tasks_done = _run_full_loop(controller, GitError("git not found"))
        assert tasks_done == 2
        assert controller.state.get_task("T-1")["status"] == "done"
        assert controller.state.get_task("T-2")["status"] == "done"

    def test_failure_is_recorded_in_the_run_log(self, controller: AutoController) -> None:
        """The failure must reach state.log (read during a postmortem), not
        just the logger, which may be configured below WARNING."""
        _run_full_loop(controller, GitError("index.lock is held"))
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "regression" in log.lower()
        assert "index.lock is held" in log


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — calling _check_regressions directly (per-item inner guard)
# ─────────────────────────────────────────────────────────────────────────────


class TestCheckRegressionsInnerGuard:
    def test_executor_run_exception_is_tolerated(self, controller: AutoController) -> None:
        """executor.run raising must not escape _check_regressions itself —
        proves the guard lives inside the method, not only at the call
        site in _run_task_loop."""
        controller.state.upsert_task(_done_task("T-REG"))

        executor = MagicMock()
        executor.run.side_effect = GitError("git reset --hard blew up")
        bug_fix_loop = MagicMock()

        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)

    def test_bug_fix_loop_handle_regression_exception_is_tolerated(
        self, controller: AutoController
    ) -> None:
        """A failure inside handle_regression() (separate from executor.run)
        must also be contained here."""
        controller.state.upsert_task(_done_task("T-REG"))

        executor = MagicMock()
        exec_result = MagicMock()
        exec_result.passed = False
        exec_result.exit_code = 1
        executor.run.return_value = exec_result

        bug_fix_loop = MagicMock()
        bug_fix_loop.handle_regression.side_effect = GitError("bfl git failure")

        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)

    def test_failure_is_recorded_in_the_run_log(self, controller: AutoController) -> None:
        controller.state.upsert_task(_done_task("T-REG"))

        executor = MagicMock()
        executor.run.side_effect = GitError("index.lock is held")
        bug_fix_loop = MagicMock()

        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)

        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "T-REG" in log
        assert "index.lock is held" in log

    def test_one_failing_done_task_does_not_skip_the_next_one(
        self, controller: AutoController
    ) -> None:
        """The key behavior a call-site-only guard cannot provide: if
        checking the FIRST done task raises, the SECOND done task in the
        same batch must still be checked.

        Under a call-site-only fix, the exception from T-REG-1 would
        propagate out of _check_regressions entirely and T-REG-2's check
        would never run (executor.run called once). Under this fix's inner
        per-item guard, the loop continues (executor.run called twice)."""
        controller.state.upsert_task(_done_task("T-REG-1"))
        controller.state.upsert_task(_done_task("T-REG-2"))

        executor = MagicMock()
        ok_result = FakeExecResult(passed=True)
        executor.run.side_effect = [GitError("first check blew up"), ok_result]
        bug_fix_loop = MagicMock()

        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)

        assert executor.run.call_count == 2
        checked_ids = [c.args[0]["id"] for c in executor.run.call_args_list]
        assert checked_ids == ["T-REG-1", "T-REG-2"]

    def test_no_done_tasks_is_a_silent_no_op(self, controller: AutoController) -> None:
        """With no DONE tasks to re-check, nothing must be called and
        nothing must raise."""
        executor = MagicMock()
        bug_fix_loop = MagicMock()
        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)
        executor.run.assert_not_called()
        bug_fix_loop.handle_regression.assert_not_called()

    def test_just_committed_id_is_excluded(self, controller: AutoController) -> None:
        """The task that was just committed must not be re-checked — its
        check was already validated moments ago by outer_loop."""
        controller.state.upsert_task(_done_task("T-COMMITTED"))

        executor = MagicMock()
        bug_fix_loop = MagicMock()
        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)
        executor.run.assert_not_called()

    def test_valid_regression_path_still_runs_bug_fix_loop(
        self, controller: AutoController
    ) -> None:
        """Sanity: the happy/regression-detected path is unchanged by the
        guard — a real regression still drives BugFixLoop and logs its
        summary."""
        controller.state.upsert_task(_done_task("T-REG"))

        executor = MagicMock()
        exec_result = FakeExecResult(passed=False, exit_code=1)
        executor.run.return_value = exec_result

        bug_fix_loop = MagicMock()
        bfl_result = MagicMock()
        bfl_result.summary.return_value = "fixed T-REG"
        bug_fix_loop.handle_regression.return_value = bfl_result

        controller._check_regressions("T-COMMITTED", executor, bug_fix_loop)

        bug_fix_loop.handle_regression.assert_called_once()
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "fixed T-REG" in log
