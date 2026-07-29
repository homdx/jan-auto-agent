"""tests/test_bug_fix_loop_archive_collision.py

Regression guard: ``BugFixLoop._clear_stale_fix_rounds`` archives a fix
task's ``feedback_round_*.md`` files into ``previous_attempt_<stamp>/`` so a
retry starts from round 1 while the old feedback stays inspectable ("moved
aside rather than deleted").

Two calls for the *same* fix_id within the same wall-clock second used to
collide: `stamp` (second-resolution) is identical, so ``archive`` is the
same path both times, and ``archive.mkdir(parents=True, exist_ok=True)``
silently reused the existing directory instead of failing or picking a new
one. Because feedback rounds are renumbered from round 1 on every retry,
the second call's ``feedback_round_1.md`` (etc.) then overwrote the FIRST
attempt's same-named file via ``f.rename(archive / f.name)`` — destroying
exactly the forensic history this function exists to preserve.

The identical same-second collision was already reproduced and fixed this
same way (a disambiguating numeric suffix) in ``TicketStore._quarantine``
and ``MetricsCollector``'s corrupt-file quarantine; this locks in the same
fix applied to ``_clear_stale_fix_rounds``.
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


def _write_rounds(tdir: Path, names: list[str], contents: list[str]) -> None:
    tdir.mkdir(parents=True, exist_ok=True)
    for name, content in zip(names, contents):
        (tdir / name).write_text(content)


def test_same_second_archive_calls_do_not_overwrite_each_other(monkeypatch):
    """Two archive calls landing on the same `stamp` must each keep their
    own feedback_round_1.md instead of the second silently clobbering the
    first's."""
    base = Path(tempfile.mkdtemp())
    loop, st = _make_loop(base)
    fix_id = "BUG-FIX-1"
    tdir = st.task_dir(fix_id)

    # Freeze "now" so both calls compute the identical second-resolution stamp.
    import tools.auto.bug_fix_loop as bfl_mod

    class _FrozenDatetime:
        @classmethod
        def now(cls):
            import datetime as _dt
            return _dt.datetime(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(bfl_mod, "datetime", _FrozenDatetime)

    # First attempt's rounds, then archive them.
    _write_rounds(tdir, ["feedback_round_1.md"], ["FIRST ATTEMPT FEEDBACK"])
    loop._clear_stale_fix_rounds(fix_id)

    # Retry burns a round with the SAME filename (rounds always renumber
    # from 1), then archive again within the same frozen second.
    _write_rounds(tdir, ["feedback_round_1.md"], ["SECOND ATTEMPT FEEDBACK"])
    loop._clear_stale_fix_rounds(fix_id)

    archives = sorted(p for p in tdir.iterdir() if p.is_dir() and p.name.startswith("previous_attempt_"))
    assert len(archives) == 2, f"expected two distinct archive dirs, got {[p.name for p in archives]}"

    contents = sorted((a / "feedback_round_1.md").read_text() for a in archives)
    assert contents == ["FIRST ATTEMPT FEEDBACK", "SECOND ATTEMPT FEEDBACK"], (
        "the first attempt's feedback was overwritten by the second — "
        f"got {contents}"
    )


def test_normal_different_second_archiving_still_works(monkeypatch):
    """Non-colliding calls (the common case) are unaffected by the fix."""
    base = Path(tempfile.mkdtemp())
    loop, st = _make_loop(base)
    fix_id = "BUG-FIX-2"
    tdir = st.task_dir(fix_id)

    _write_rounds(tdir, ["feedback_round_1.md"], ["only attempt"])
    loop._clear_stale_fix_rounds(fix_id)

    archives = [p for p in tdir.iterdir() if p.is_dir() and p.name.startswith("previous_attempt_")]
    assert len(archives) == 1
    assert (archives[0] / "feedback_round_1.md").read_text() == "only attempt"
    # feedback_round_1.md was moved out of tdir, not copied
    assert not (tdir / "feedback_round_1.md").exists()
