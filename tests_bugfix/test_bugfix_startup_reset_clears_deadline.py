"""A2: the startup BLOCKED-reset must also clear the wall-clock deadline.

Real failure mode
-----------------
``OuterLoop.run_task`` (outer_loop.py:143-175) persists a task's wall-clock
start time once, in ``deadline_started_at.txt``, and on every resume derives
the remaining budget from ``time.time() - started_at``.

``AutoController._reset_resettable_blocked_tasks`` resets an unmet-dependency
BLOCKED task back to TODO so it can be attempted again — but it never touched
that file. So on the very next run the persisted start time was re-read, the
elapsed time already exceeded ``max_task_seconds``, the remaining budget came
out as 0, and the deadline gate parked the task straight back into BLOCKED
without a single new attempt.

That is the only genuine livelock of the two: the reset mechanism is
structurally incapable of granting the attempt it exists to grant. Every
resume burned a cycle and re-parked the task.
"""

import configparser
import time
from unittest.mock import MagicMock

from tools.auto.controller import AutoController
from tools.auto.outer_loop import OuterLoop
from tools.auto.state import (
    STATUS_BLOCKED,
    STATUS_TODO,
    StateStore,
    make_task,
)

TASK_ID = "AUTO-T1"


def _real_startup_reset(state: StateStore) -> None:
    """Invoke the REAL AutoController._reset_resettable_blocked_tasks.

    Replaying the method's logic inside this file would make these tests
    tautological — they would pass on unfixed production code.
    """
    cfg = configparser.ConfigParser()
    cfg.add_section("auto")
    cfg.set("auto", "max_rounds_per_task", "10")
    stub = MagicMock()
    stub.state = state
    AutoController._reset_resettable_blocked_tasks(stub, cfg)


def _store_with_blocked_task(tmp_path) -> StateStore:
    state = StateStore(tmp_path / ".agent")
    state.initialise("make it work", tmp_path)
    state.upsert_task(make_task(
        id=TASK_ID, title="t", instruction="i",
        target_files=["a.py"], acceptance_check="true",
    ))
    state.set_task_status(TASK_ID, STATUS_BLOCKED)
    return state


def _write_stale_deadline(state: StateStore, seconds_ago: float,
                          task_id: str = TASK_ID) -> None:
    state.write_task_file(
        task_id, "deadline_started_at.txt", repr(time.time() - seconds_ago),
    )


def _deadline_path(state: StateStore, task_id: str = TASK_ID):
    return state.task_dir(task_id) / "deadline_started_at.txt"


class TestStartupResetClearsDeadline:

    def test_reset_removes_stale_deadline_file(self, tmp_path):
        state = _store_with_blocked_task(tmp_path)
        _write_stale_deadline(state, 999_999)
        assert _deadline_path(state).exists()

        _real_startup_reset(state)

        assert state.get_task(TASK_ID)["status"] == STATUS_TODO
        assert not _deadline_path(state).exists(), (
            "deadline_started_at.txt must not survive the reset — otherwise "
            "the fresh attempt inherits the already-exhausted budget of the "
            "previous one and is re-blocked immediately"
        )

    def test_reset_without_deadline_file_does_not_raise(self, tmp_path):
        state = _store_with_blocked_task(tmp_path)
        _real_startup_reset(state)  # must not raise
        assert state.get_task(TASK_ID)["status"] == STATUS_TODO

    def test_round_exhausted_task_is_not_touched(self, tmp_path):
        """The fix is scoped to the reset — skipped tasks keep their files."""
        state = _store_with_blocked_task(tmp_path)
        _write_stale_deadline(state, 999_999)
        tdir = state.task_dir(TASK_ID)
        # 10 completed rounds == max_rounds_per_task → not resettable
        for n in range(1, 11):
            (tdir / f"feedback_round_{n}.md").write_text(
                "impl v1\nFinal issue to fix next round:\nnothing\n",
                encoding="utf-8",
            )

        _real_startup_reset(state)

        assert state.get_task(TASK_ID)["status"] == STATUS_BLOCKED
        assert _deadline_path(state).exists()

    def test_fix_task_parking_is_not_touched(self, tmp_path):
        state = _store_with_blocked_task(tmp_path)
        fix_id = "BUG-FIX-AUTO-T2"
        state.upsert_task(make_task(id=fix_id, title="f", instruction="i"))
        state.set_task_status(fix_id, STATUS_BLOCKED)
        _write_stale_deadline(state, 999_999, task_id=fix_id)

        _real_startup_reset(state)

        # The parked fix task is skipped entirely, so its budget file must be
        # left alone too.
        assert state.get_task(fix_id)["status"] == STATUS_BLOCKED
        assert _deadline_path(state, fix_id).exists()
        # ...while the resettable task next to it does get a clean budget.
        assert not _deadline_path(state).exists()


class TestResetActuallyGrantsARealAttempt:
    """The observable consequence: the retry gets a fresh budget, not zero."""

    def test_reset_task_runs_with_full_budget(self, tmp_path):
        budget = 600  # seconds

        state = _store_with_blocked_task(tmp_path)
        _write_stale_deadline(state, 999_999)
        _real_startup_reset(state)

        seen: dict = {}

        class FakeResult:
            passed = True
            attempts_used = 1
            context_satisfied = True

        class FakeInner:
            max_task_seconds = budget

            def run_task(self, task, base_dir, prior_feedback=None,
                         prior_implementations=None, deadline=None):
                seen["deadline"] = deadline
                return FakeResult()

        outer = OuterLoop(FakeInner(), state, max_rounds=3)
        result = outer.run_task(state.get_task(TASK_ID), tmp_path)

        assert result.passed is True
        remaining = seen["deadline"] - time.monotonic()
        assert remaining >= budget * 0.5, (
            f"the reset task should get a fresh ~{budget}s budget, got "
            f"{remaining:.1f}s — deadline_started_at.txt was not cleared"
        )


class TestBaselineWithoutReset:
    """The same stale deadline, with no reset: zero attempts get run."""

    def test_stale_deadline_blocks_without_any_attempt(self, tmp_path):
        budget = 600

        state = _store_with_blocked_task(tmp_path)
        _write_stale_deadline(state, 999_999)
        # Deliberately NO _real_startup_reset here.

        attempts: list = []

        class FakeResult:
            passed = True
            attempts_used = 1
            context_satisfied = True

        class FakeInner:
            max_task_seconds = budget

            def run_task(self, task, base_dir, prior_feedback=None,
                         prior_implementations=None, deadline=None):
                attempts.append(deadline)
                return FakeResult()

        outer = OuterLoop(FakeInner(), state, max_rounds=3)
        result = outer.run_task(state.get_task(TASK_ID), tmp_path)

        assert result.passed is False
        assert result.exhausted is True
        assert not attempts, (
            "an already-exhausted wall-clock budget must prevent the attempt"
        )
