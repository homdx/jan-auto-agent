"""tests/test_bugfix_commit_on_success_nothing_staged_guard.py

Bug: the `else` (nothing-staged) branch of `CommitOnSuccess.commit()` called
`self._state.set_task_status()` / `self._state.log()` unguarded in both of
its sub-branches (BLOCKED for a trivial acceptance check, and DONE with an
empty commit hash), while the sibling `if sha:` branch explicitly wraps the
identical calls in try/except — with a comment explaining exactly why: a
StateStore write hiccup must not propagate and abort the whole automated
run. Fix: wrap both nothing-staged sub-branches in the same guard, log the
failure, and still return normally (never raise) — matching the module's
documented "never raises" contract in every branch, not just the success
path.
"""
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.commit_on_success import CommitOnSuccess
from tools.auto.state import StateStore, make_task


class FakeGitManager:
    """Always reports nothing staged (commit_task returns None)."""

    def commit_task(self, task_id: str, title: str) -> Optional[str]:
        return None

    def ensure_repo(self) -> bool:  # pragma: no cover
        return False

    def configure_identity(self) -> None:  # pragma: no cover
        pass


class RaisingStateStore:
    """Minimal StateStore stand-in whose writes always fail."""

    def set_task_status(self, *args, **kwargs):
        raise OSError("simulated disk failure")

    def log(self, *args, **kwargs):
        raise OSError("simulated disk failure")


TASK_TRIVIAL = {
    "id": "AUTO-T1",
    "title": "No-op chapter check",
    "instruction": "do it",
    "target_files": ["chapter1.md"],
    "acceptance_check": "true",
}

TASK_REAL_CHECK = {
    "id": "AUTO-T2",
    "title": "Fix off-by-one",
    "instruction": "do it",
    "target_files": ["main.py"],
    "acceptance_check": "pytest -q",
}


def test_blocked_branch_state_failure_does_not_raise():
    cos = CommitOnSuccess(FakeGitManager(), RaisingStateStore(), task_mode="creative")
    # Must not raise even though every StateStore write fails.
    sha = cos.commit(TASK_TRIVIAL)
    assert sha is None


def test_done_empty_commit_branch_state_failure_does_not_raise():
    cos = CommitOnSuccess(FakeGitManager(), RaisingStateStore(), task_mode="code")
    # Must not raise even though every StateStore write fails.
    sha = cos.commit(TASK_REAL_CHECK)
    assert sha is None
