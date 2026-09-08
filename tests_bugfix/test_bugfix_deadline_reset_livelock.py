"""A2: resetting a BLOCKED task must clear its persisted wall-clock deadline.

OuterLoop persists the task's wall-clock start time in
``.agent/tasks/<id>/deadline_started_at.txt`` once, the first time the task
runs (outer_loop.py), and derives the remaining budget from elapsed
wall-clock time on every resume. That is what makes the cap survive a
restart.

But ``AutoController._reset_resettable_blocked_tasks`` — the startup
mechanism that exists to give a stuck task a fresh attempt — reset the
status back to TODO without removing that file. On resume the persisted
start time was re-read, the elapsed time already exceeded the budget, the
remaining budget was 0, and the task was immediately re-blocked by the
deadline gate. The reset mechanism was structurally incapable of granting
the new attempt it exists to grant: every resume burned a cycle and
re-parked the task.

Note BugFixLoop already clears the file when it archives stale feedback
rounds for an operator reset (bug_fix_loop.py) — this is the second,
unguarded call site with the same consequence.
"""

import configparser
from unittest.mock import MagicMock

import pytest

from tools.auto.controller import AutoController
from tools.auto.state import StateStore, make_task, STATUS_BLOCKED, STATUS_TODO


DEADLINE_FILE = "deadline_started_at.txt"


def _real_startup_reset(state):
    """Invoke the REAL _reset_resettable_blocked_tasks against *state*.

    Re-implementing the method's logic inside the test would make the
    assertions tautological — they would pass identically on unfixed code.
    """
    cfg = configparser.ConfigParser()
    cfg.add_section("auto")
    stub = MagicMock()
    stub.state = state
    AutoController._reset_resettable_blocked_tasks(stub, cfg)


def _blocked_task_with_deadline(tmp_path, task_id="AUTO-T1", start=1000.0):
    """A task parked BLOCKED whose deadline clock has already been started."""
    state = StateStore(tmp_path / ".agent")
    state.initialise("g", tmp_path)
    state.upsert_task(make_task(
        id=task_id, title="stuck", instruction="i",
    ))
    state.set_task_status(task_id, STATUS_BLOCKED)
    state.write_task_file(task_id, DEADLINE_FILE, repr(start))
    return state


class TestResetClearsDeadline:
    def test_deadline_file_removed_on_reset(self, tmp_path):
        state = _blocked_task_with_deadline(tmp_path)
        assert state.read_task_file("AUTO-T1", DEADLINE_FILE) is not None

        _real_startup_reset(state)

        assert state.get_task("AUTO-T1")["status"] == STATUS_TODO
        assert state.read_task_file("AUTO-T1", DEADLINE_FILE) is None, (
            "stale deadline persisted: the resumed run starts with 0 budget "
            "left and is immediately re-blocked"
        )

    def test_deadline_file_gone_from_disk_too(self, tmp_path):
        """read_task_file returns None for a missing file, but so does
        task_dir() creating the parent — assert the actual unlink."""
        state = _blocked_task_with_deadline(tmp_path)
        path = state.task_dir("AUTO-T1") / DEADLINE_FILE
        assert path.exists()

        _real_startup_reset(state)

        assert not path.exists()

    def test_resume_reads_no_stale_start_time(self, tmp_path):
        state = _blocked_task_with_deadline(tmp_path)
        _real_startup_reset(state)

        # This is the exact read OuterLoop performs to derive the deadline.
        raw = state.read_task_file("AUTO-T1", DEADLINE_FILE)
        assert raw is None
        assert (state.task_dir("AUTO-T1") / DEADLINE_FILE).exists() is False

    def test_repeated_reset_is_idempotent(self, tmp_path):
        """A second startup must not raise on a file that is already gone."""
        state = _blocked_task_with_deadline(tmp_path)
        _real_startup_reset(state)
        state.set_task_status("AUTO-T1", STATUS_BLOCKED)
        _real_startup_reset(state)
        assert state.get_task("AUTO-T1")["status"] == STATUS_TODO
        assert not (state.task_dir("AUTO-T1") / DEADLINE_FILE).exists()


class TestNotOverBroad:
    def test_round_exhausted_task_is_left_alone(self, tmp_path):
        """Round-exhausted tasks are deliberately NOT reset — and their
        deadline must not be touched either."""
        state = _blocked_task_with_deadline(tmp_path)
        # Burn the whole round budget so the case-2 check trips.
        for n in range(1, 11):
            state.write_task_file("AUTO-T1", f"feedback_round_{n}.md", "fb")

        _real_startup_reset(state)

        assert state.get_task("AUTO-T1")["status"] == STATUS_BLOCKED
        assert (state.task_dir("AUTO-T1") / DEADLINE_FILE).exists()

    def test_parked_fix_task_is_left_alone(self, tmp_path):
        state = _blocked_task_with_deadline(tmp_path, task_id="BUG-FIX-AUTO-T1")
        _real_startup_reset(state)
        assert state.get_task("BUG-FIX-AUTO-T1")["status"] == STATUS_BLOCKED
        assert (state.task_dir("BUG-FIX-AUTO-T1") / DEADLINE_FILE).exists()

    def test_non_blocked_task_is_left_alone(self, tmp_path):
        state = _blocked_task_with_deadline(tmp_path)
        state.set_task_status("AUTO-T1", STATUS_TODO)
        _real_startup_reset(state)
        assert (state.task_dir("AUTO-T1") / DEADLINE_FILE).exists()

    def test_task_with_no_deadline_file_is_unaffected(self, tmp_path):
        """Most reset tasks have never been worked yet — no file, no error."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        state.upsert_task(make_task(
            id="AUTO-T2", title="blocked on a dependency", instruction="i",
        ))
        state.set_task_status("AUTO-T2", STATUS_BLOCKED)

        _real_startup_reset(state)

        assert state.get_task("AUTO-T2")["status"] == STATUS_TODO
