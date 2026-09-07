"""tests_bugfix/test_bugfix_reset_clears_deadline.py — A2: reset must clear the deadline.

Bug: ``AutoController._reset_resettable_blocked_tasks`` returns a resettable
BLOCKED task to TODO but never removes ``deadline_started_at.txt``.  On resume
the persisted start time is re-read, ``elapsed`` already exceeds the budget,
``remaining`` is 0, and the task is re-blocked immediately.  This is the only
genuine livelock in either report: the reset mechanism is structurally incapable
of granting the new attempt it exists to grant.  Every resume burns a cycle and
re-parks the task.

Fix: unlink ``deadline_started_at.txt`` inside the same reset that clears the
status, so the two halves of "give this task a fresh start" stay together.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import STATUS_BLOCKED, STATUS_TODO, StateStore, make_task


def _run_real_reset(state: StateStore) -> None:
    """Invoke the REAL AutoController._reset_resettable_blocked_tasks."""
    cfg = configparser.ConfigParser()
    cfg.add_section("auto")
    stub = MagicMock()
    stub.state = state
    AutoController._reset_resettable_blocked_tasks(stub, cfg)


class TestResetClearsDeadline:
    def test_reset_blocked_task_removes_deadline_started_at(self, tmp_path):
        """A resettable BLOCKED task must lose its deadline start time too."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        state.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
            target_files=["a.py"], acceptance_check="true",
        ))
        state.set_task_status("AUTO-T1", STATUS_BLOCKED)

        # Simulate OuterLoop.run_task having written the deadline start time
        # on the first attempt (outer_loop.py:164).
        state.write_task_file("AUTO-T1", "deadline_started_at.txt", "1700000000.0")
        deadline_path = state.task_dir("AUTO-T1") / "deadline_started_at.txt"
        assert deadline_path.exists(), (
            "precondition: deadline_started_at.txt should exist before the reset"
        )

        _run_real_reset(state)

        assert state.get_task("AUTO-T1")["status"] == STATUS_TODO
        assert not deadline_path.exists(), (
            "deadline_started_at.txt must be removed by the reset — otherwise "
            "OuterLoop.run_task re-reads the stale start time on resume, "
            "computes ~0 seconds of remaining budget, and re-blocks the task "
            "immediately, leaving the reset structurally incapable of granting "
            "the new attempt it exists to grant"
        )

    def test_reset_without_deadline_file_is_noop(self, tmp_path):
        """A task that never had a deadline file must not crash the reset."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        state.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
            target_files=["a.py"], acceptance_check="true",
        ))
        state.set_task_status("AUTO-T1", STATUS_BLOCKED)

        # No deadline_started_at.txt — must not raise.
        _run_real_reset(state)

        assert state.get_task("AUTO-T1")["status"] == STATUS_TODO

    def test_round_exhausted_task_keeps_deadline_file(self, tmp_path):
        """Round-exhausted tasks are NOT reset — their deadline file stays."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        state.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
            target_files=["a.py"], acceptance_check="true",
        ))
        state.set_task_status("AUTO-T1", STATUS_BLOCKED)
        state.write_task_file("AUTO-T1", "deadline_started_at.txt", "1700000000.0")

        # Burn every configured round so the task is case-2 (round-exhausted).
        from tools.auto.utils import highest_completed_round
        for n in range(1, 11):
            state.write_task_file("AUTO-T1", f"feedback_round_{n}.md", "fb")
        assert highest_completed_round(state.task_dir("AUTO-T1")) >= 10

        _run_real_reset(state)

        # Not reset — stays BLOCKED, deadline file untouched.
        assert state.get_task("AUTO-T1")["status"] == STATUS_BLOCKED
        assert (state.task_dir("AUTO-T1") / "deadline_started_at.txt").exists()