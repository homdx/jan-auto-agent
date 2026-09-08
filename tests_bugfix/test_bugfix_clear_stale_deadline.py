"""tests_bugfix/test_bugfix_clear_stale_deadline.py

Regression guard: ``BugFixLoop._clear_stale_fix_rounds`` archives a fix
task's ``feedback_round_*.md`` files so the retry starts from round 1,
but it used to leave ``deadline_started_at.txt`` behind — the wall-clock
budget file ``OuterLoop.run_task`` writes on the first run and reads on
every resume to compute ``_remaining`` (outer_loop.py:147-174).

With the deadline file still on disk, a retried bug fix computed
``_elapsed`` from the ORIGINAL run's start time, leaving ~0 seconds of
budget, so ``OuterLoop`` immediately returned exhausted on the first
round — the documented "operator reset to retry" path silently did
nothing.

The fix: ``_clear_stale_fix_rounds`` also removes ``deadline_started_at.txt``
so the next ``run_task`` call treats the retry as a fresh start and
writes a new deadline.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.bug_fix_loop import BugFixLoop
from tools.auto.state import StateStore


def _make_loop(base_dir: Path) -> tuple[BugFixLoop, StateStore]:
    st = StateStore(base_dir / ".agent")
    st.initialise("goal", base_dir)
    loop = BugFixLoop.__new__(BugFixLoop)  # bypass __init__: only self._state is needed
    loop._state = st
    return loop, st


def test_clear_stale_fix_rounds_removes_deadline_started_at(tmp_path):
    """_clear_stale_fix_rounds must also delete deadline_started_at.txt so
    the retried bug fix gets a full fresh wall-clock budget rather than
    inheriting the exhausted deadline from the failed attempt."""
    base = Path(tmp_path)
    loop, st = _make_loop(base)
    fix_id = "BUG-FIX-3"
    tdir = st.task_dir(fix_id)

    # Simulate the state OuterLoop.run_task leaves after the first attempt:
    # feedback rounds from a failed run, plus a deadline start time.
    (tdir / "feedback_round_1.md").write_text("# Round 1 — failed\n")
    (tdir / "feedback_round_2.md").write_text("# Round 2 — failed\n")
    st.write_task_file(fix_id, "deadline_started_at.txt", "1700000000.0")

    assert (tdir / "deadline_started_at.txt").exists(), (
        "precondition: the deadline file should exist before the clear"
    )

    loop._clear_stale_fix_rounds(fix_id)

    # feedback rounds are archived (already covered by existing tests)
    archives = [
        p for p in tdir.iterdir()
        if p.is_dir() and p.name.startswith("previous_attempt_")
    ]
    assert len(archives) == 1, "feedback rounds should be archived"

    # THE BUG: deadline_started_at.txt must NOT survive the clear —
    # if it does, OuterLoop.run_task computes _elapsed from the old
    # start time, leaving ~0 seconds of wall-clock budget, and the
    # retry is immediately exhausted.
    deadline_path = tdir / "deadline_started_at.txt"
    assert not deadline_path.exists(), (
        "deadline_started_at.txt must be removed by _clear_stale_fix_rounds "
        "so the retry gets a fresh wall-clock budget, not the stale one "
        "from the failed attempt that was just archived"
    )


def test_clear_stale_fix_rounds_without_deadline_file_is_noop(tmp_path):
    """If there is no deadline_started_at.txt (e.g. max_task_seconds=0,
    or the first run never reached the deadline-writing code path),
    _clear_stale_fix_rounds must not crash trying to delete it."""
    base = Path(tmp_path)
    loop, st = _make_loop(base)
    fix_id = "BUG-FIX-4"
    tdir = st.task_dir(fix_id)

    (tdir / "feedback_round_1.md").write_text("# Round 1\n")

    # No deadline_started_at.txt — should not raise
    loop._clear_stale_fix_rounds(fix_id)

    archives = [
        p for p in tdir.iterdir()
        if p.is_dir() and p.name.startswith("previous_attempt_")
    ]
    assert len(archives) == 1, "feedback rounds should be archived"
    assert not (tdir / "feedback_round_1.md").exists(), "rounds should be moved"
