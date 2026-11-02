"""tests/test_harden30_d_bugfix_ledger_reset.py — HARDEN-30 D.

An explicit reset (ticket set back to ``open``) clears the time-budget ledger
whether or not there are feedback rounds to archive. An ordinary resume that is
not a reset keeps the accumulated budget.
"""

from __future__ import annotations

from pathlib import Path

from tools.auto.bug_fix_loop import BugFixLoop
from tools.auto.state import StateStore


def _make_loop(tmp_path: Path) -> tuple[BugFixLoop, Path]:
    state = StateStore(tmp_path / ".agent")
    loop = BugFixLoop(
        outer_loop=None, commit_on_success=None, ticket_store=None,
        state_store=state,
    )
    return loop, tmp_path / ".agent" / "tasks" / "BUG-FIX-T1"


def test_clear_stale_fix_rounds_clears_ledger_without_feedback_rounds(
    tmp_path: Path
) -> None:
    loop, tdir = _make_loop(tmp_path)
    tdir.mkdir(parents=True)
    (tdir / "deadline_started_at.txt").write_text('{"consumed_s": 1234.5}\n')
    assert (tdir / "deadline_started_at.txt").exists()
    # No feedback_round_*.md: the old code returned here before reaching the
    # ledger, so it survived. The fix clears it regardless.
    loop._clear_stale_fix_rounds("BUG-FIX-T1")
    assert not (tdir / "deadline_started_at.txt").exists()


def test_clear_stale_fix_rounds_archives_rounds_and_clears_ledger(
    tmp_path: Path
) -> None:
    loop, tdir = _make_loop(tmp_path)
    tdir.mkdir(parents=True)
    (tdir / "deadline_started_at.txt").write_text('{"consumed_s": 1234.5}\n')
    (tdir / "feedback_round_1.md").write_text("feedback one\n")
    (tdir / "feedback_round_2.md").write_text("feedback two\n")
    loop._clear_stale_fix_rounds("BUG-FIX-T1")
    # Existing feedback is still archived...
    archives = [p for p in tdir.iterdir()
                if p.is_dir() and p.name.startswith("previous_attempt_")]
    assert len(archives) == 1
    assert (archives[0] / "feedback_round_1.md").exists()
    assert (archives[0] / "feedback_round_2.md").exists()
    assert not (tdir / "feedback_round_1.md").exists()
    # ...and the time-budget ledger is cleared too.
    assert not (tdir / "deadline_started_at.txt").exists()
