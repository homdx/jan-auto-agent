"""tests_bugfix/test_bugfix_reset_blocked_clears_deadline.py

The wall-clock budget for a task is persisted in
``.agent/tasks/<task_id>/deadline_started_at.txt``: OuterLoop.run_task writes
it the first time a task is worked and reads it back on every resume to derive
the remaining budget (outer_loop.py:143-175).

AutoController._reset_resettable_blocked_tasks resets a BLOCKED task back to
TODO so the resumed session can give it another attempt — but it only cleared
the STATUS half of "give this task a fresh start". The start-time file was
never unlinked, so on resume the persisted start time was re-read, the elapsed
time already exceeded the budget, ``_remaining`` was 0, and the task was
re-blocked immediately by the deadline gate.

That is the only genuine livelock in the pipeline: the reset mechanism is
structurally incapable of granting the new attempt it exists to grant. Every
resume burns a cycle and re-parks the task, with no error anywhere.

BugFixLoop._clear_stale_fix_rounds already unlinks the same file for the
retry path (see test_bugfix_clear_stale_deadline.py); the startup reset path
needed the same half of the fix.

Fix under test: unlink ``deadline_started_at.txt`` inside the same reset that
clears the status, so the two halves of "fresh start" stay together.
"""

from __future__ import annotations

import configparser
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_TODO,
    StateStore,
    make_task,
)


RESETTABLE_ID = "AUTO-T1"      # unmet dependency — case 1, genuinely resettable
EXHAUSTED_ID = "AUTO-T2"       # burned every round — must stay BLOCKED
PARKED_ID = "BUG-FIX-AUTO-T1"  # parked by BugFixLoop — must stay BLOCKED


def _cfg(max_rounds: int = 10) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.add_section("auto")
    cfg.set("auto", "max_rounds_per_task", str(max_rounds))
    return cfg


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / ".agent")
    store.initialise("goal", tmp_path)
    for task_id in (RESETTABLE_ID, EXHAUSTED_ID, PARKED_ID):
        store.upsert_task(make_task(
            id=task_id, title=f"title {task_id}", instruction="i",
            target_files=["a.py"], acceptance_check="true",
        ))
    return store


def _real_startup_reset(state) -> None:
    """Invoke the REAL AutoController._reset_resettable_blocked_tasks.

    Never re-implement the logic under test: a replayed copy of the method
    would pass identically on unfixed production code.
    """
    stub = MagicMock()
    stub.state = state
    AutoController._reset_resettable_blocked_tasks(stub, _cfg())


class TestResetClearsDeadline:
    def test_resettable_blocked_task_gets_fresh_deadline(self, tmp_path):
        """THE BUG: resetting the status must also unlink the persisted
        deadline, otherwise resume re-reads the old start time, the remaining
        budget is 0, and the task is re-blocked without a single new attempt."""
        store = _store(tmp_path)
        store.set_task_status(RESETTABLE_ID, STATUS_BLOCKED)

        # Exhausted wall-clock budget: the budget burned long ago.
        stale_start = repr(time.time() - 36000)
        store.write_task_file(RESETTABLE_ID, "deadline_started_at.txt", stale_start)
        store.write_task_file(EXHAUSTED_ID, "deadline_started_at.txt", stale_start)
        store.write_task_file(PARKED_ID, "deadline_started_at.txt", stale_start)

        # Resettable = BLOCKED with no rounds burned (case 1, dependency).
        # The other two are BLOCKED for reasons a reset must not undo.
        tdir = store.task_dir(EXHAUSTED_ID)
        for n in range(1, 11):
            (tdir / f"feedback_round_{n}.md").write_text(f"# round {n}\n")
        store.task_dir(PARKED_ID)

        _real_startup_reset(store)

        assert store.get_task(RESETTABLE_ID)["status"] == STATUS_TODO, (
            "the dependency case must still be reset to TODO"
        )
        assert not (store.task_dir(RESETTABLE_ID) / "deadline_started_at.txt").exists(), (
            "deadline_started_at.txt must be removed in the same reset that "
            "clears the status — leaving it means OuterLoop recomputes the "
            "elapsed time against the old start time, _remaining is 0, and "
            "the task is re-blocked without a new attempt"
        )

    def test_left_blocked_tasks_keep_their_deadline(self, tmp_path):
        """The unlink must be scoped to the task that was actually reset:
        round-exhausted and parked tasks stay BLOCKED and keep their files."""
        store = _store(tmp_path)
        for task_id in (RESETTABLE_ID, EXHAUSTED_ID, PARKED_ID):
            store.set_task_status(task_id, STATUS_BLOCKED)
            store.write_task_file(task_id, "deadline_started_at.txt", "1700000000.0")
        tdir = store.task_dir(EXHAUSTED_ID)
        for n in range(1, 11):
            (tdir / f"feedback_round_{n}.md").write_text(f"# round {n}\n")
        store.task_dir(PARKED_ID)

        _real_startup_reset(store)

        assert store.get_task(EXHAUSTED_ID)["status"] == STATUS_BLOCKED
        assert store.get_task(PARKED_ID)["status"] == STATUS_BLOCKED
        assert (store.task_dir(EXHAUSTED_ID) / "deadline_started_at.txt").exists()
        assert (store.task_dir(PARKED_ID) / "deadline_started_at.txt").exists()
        assert (store.task_dir(EXHAUSTED_ID) / "feedback_round_10.md").exists()

    def test_reset_without_deadline_file_is_noop(self, tmp_path):
        """max_task_seconds=0 means OuterLoop never wrote the file; the reset
        must not crash trying to unlink it."""
        store = _store(tmp_path)
        store.set_task_status(RESETTABLE_ID, STATUS_BLOCKED)
        store.set_task_status(PARKED_ID, STATUS_BLOCKED)

        _real_startup_reset(store)

        assert store.get_task(RESETTABLE_ID)["status"] == STATUS_TODO
        assert store.get_task(PARKED_ID)["status"] == STATUS_BLOCKED


def _outer_loop(store, tmp_path):
    """An OuterLoop wired to an inner loop that passes on the first round and
    counts how many rounds OuterLoop actually let it run."""
    from tools.auto.outer_loop import OuterLoop

    class Inner:
        max_task_seconds = 60

        def __init__(self):
            self.calls = 0

        def run_task(self, task, base_dir, **kwargs):
            self.calls += 1
            return MagicMock(passed=True, attempts_used=1)

    inner = Inner()
    return OuterLoop(inner, store, max_rounds=1), inner


def test_stale_deadline_is_what_blocks_the_attempt(tmp_path):
    """Causal link, pinned so the file-existence assertions above stay
    meaningful.

    With the stale file present OuterLoop grants 0 seconds of budget and
    returns exhausted WITHOUT running a single round — the exact livelock.
    With the file gone it grants the full budget and runs the round.

    (OuterLoop itself is correct; this test only documents that the file is
    the whole story, so that the reset test above is not asserting on a
    red herring.)
    """
    store = _store(tmp_path)
    loop, inner = _outer_loop(store, tmp_path)
    store.set_task_status(RESETTABLE_ID, STATUS_TODO)
    store.write_task_file(
        RESETTABLE_ID, "deadline_started_at.txt", repr(time.time() - 36000)
    )

    res = loop.run_task(store.get_task(RESETTABLE_ID), tmp_path)
    assert res.passed is False and res.exhausted is True
    assert inner.calls == 0, (
        "a stale deadline_started_at leaves zero budget, so OuterLoop stops "
        "before round 1 — the livelock this fix removes"
    )
    assert store.get_task(RESETTABLE_ID)["status"] == STATUS_BLOCKED

    store.task_dir(RESETTABLE_ID).joinpath("deadline_started_at.txt").unlink()
    store.set_task_status(RESETTABLE_ID, STATUS_TODO)
    res = loop.run_task(store.get_task(RESETTABLE_ID), tmp_path)
    assert res.passed is True
    assert inner.calls == 1, "a fresh start must actually run a round"
    assert store.get_task(RESETTABLE_ID)["status"] == STATUS_DONE


def test_reset_then_resume_actually_runs_the_task(tmp_path):
    """The full chain the operator experiences: a blocked task whose wall-clock
    budget burned out, then a resume.

    Pre-fix, the resume reset the status to TODO but the persisted start time
    survived, so OuterLoop got 0 seconds of budget, stopped before round 1, and
    parked the task again — every resume burned a cycle. The fixed reset clears
    both halves, so the retry is a genuine attempt.
    """
    store = _store(tmp_path)
    store.set_task_status(RESETTABLE_ID, STATUS_BLOCKED)
    store.write_task_file(
        RESETTABLE_ID, "deadline_started_at.txt", repr(time.time() - 36000)
    )

    # The resumed session: startup reset, then the main queue picks the task up.
    _real_startup_reset(store)
    assert store.get_task(RESETTABLE_ID)["status"] == STATUS_TODO

    loop, inner = _outer_loop(store, tmp_path)
    res = loop.run_task(store.get_task(RESETTABLE_ID), tmp_path)

    assert res.passed is True, (
        "the reset attempt was granted no wall-clock budget — OuterLoop stopped "
        "before round 1 and re-blocked the task"
    )
    assert inner.calls == 1, "the reset must actually grant a new round"
    assert store.get_task(RESETTABLE_ID)["status"] == STATUS_DONE
