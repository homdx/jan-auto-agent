"""B9 -- a failed cleanup discard killed the whole multi-task run.

In the exhaustion branch of ``AutoController._run_task_loop``,
``self.git.discard_working_changes()`` was called unguarded.
``GitManager._run()`` raises ``GitError`` on a timeout (a stale index.lock, a
hung hook), a missing git binary, or a non-zero ``git reset --hard``. That
one failure escaped the per-task loop and aborted the run at the worst
possible moment: the task had already been fully accounted for -- exhaustion
note written, ticket filed, status set to BLOCKED -- so every task still
pending in plan.json was dropped along with it.

The worst case if the discard fails is that the residue gets swept into the
next commit. Bad, recoverable, and strictly better than losing the run.

Two things beyond "does not raise" are pinned:

* ``GitError`` is caught specifically, not ``Exception`` -- an unexpected
  failure on this path must still surface
  (``test_unexpected_exception_still_propagates``);
* the failure reaches ``state.log`` and not just ``logger``. The run log is
  what gets read during a postmortem, and logger may be configured to a
  level that drops the warning
  (``test_failure_is_recorded_in_the_run_log``).

Without the fix the module has no ``_discard_exhausted_residue`` at all.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits  # noqa: E402
from tools.auto.git_manager import GitError  # noqa: E402
from tools.auto.state import StateStore  # noqa: E402


@pytest.fixture()
def controller(tmp_path: Path) -> AutoController:
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
    return ctrl


def _git(side_effect=None) -> SimpleNamespace:
    return SimpleNamespace(
        discard_working_changes=MagicMock(side_effect=side_effect)
    )


class TestGitErrorIsTolerated:
    def test_giterror_does_not_propagate(self, controller: AutoController) -> None:
        controller.git = _git(GitError("git reset --hard failed"))
        controller._discard_exhausted_residue("T-EX")  # must not raise

    def test_failure_is_recorded_in_the_run_log(
        self, controller: AutoController
    ) -> None:
        controller.git = _git(GitError("timeout waiting for index.lock"))
        controller._discard_exhausted_residue("T-EX")
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "T-EX" in log
        assert "NOT discarded" in log

    def test_failure_reason_is_in_the_run_log(
        self, controller: AutoController
    ) -> None:
        controller.git = _git(GitError("index.lock is held"))
        controller._discard_exhausted_residue("T-EX")
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "index.lock is held" in log


class TestNarrowCatch:
    def test_unexpected_exception_still_propagates(
        self, controller: AutoController
    ) -> None:
        """Only GitError is tolerated; a real bug must not be hidden."""
        controller.git = _git(AttributeError("typo in GitManager"))
        with pytest.raises(AttributeError):
            controller._discard_exhausted_residue("T-EX")


class TestHappyPath:
    def test_successful_discard_is_logged(self, controller: AutoController) -> None:
        controller.git = _git()
        controller._discard_exhausted_residue("T-OK")
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "uncommitted edits discarded" in log

    def test_successful_discard_actually_calls_git(
        self, controller: AutoController
    ) -> None:
        controller.git = _git()
        controller._discard_exhausted_residue("T-OK")
        controller.git.discard_working_changes.assert_called_once()

    def test_no_git_is_a_silent_no_op(self, controller: AutoController) -> None:
        controller.git = None
        controller._discard_exhausted_residue("T-OK")
        log = (controller.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "uncommitted edits" not in log
