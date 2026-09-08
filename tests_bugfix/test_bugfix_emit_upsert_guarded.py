"""tests_bugfix/test_bugfix_emit_upsert_guarded.py

BUG 14 (second half) — the ``upsert_task`` loop in
``PlanEmitter.emit`` was unguarded.

The write of ``IMPROVEMENTS.md`` was made atomic by an earlier fix, but the
loop right after it was not touched: ``upsert_task`` validates against the
plan.json schema and raises ``ValueError`` on a violation, and the tasks
being upserted are derived from LLM-authored candidates, so a violation is
a normal-frequency event.

Unguarded, the first bad task aborted ``emit()`` *after* IMPROVEMENTS.md
had been written and after every earlier task had already been persisted
by its own ``upsert_task`` — leaving plan.json half-populated, uncommitted,
and disagreeing with the IMPROVEMENTS.md sitting beside it. The next run
resumed from that partial plan.

The fix skips the offending task and keeps going. What these tests pin
beyond "does not raise" is that the tasks *after* the bad one are still
persisted (a try/except around the whole loop would drop them, and would
look identical from the caller's side) and that the shortfall is recorded,
since plan.json and IMPROVEMENTS.md now legitimately disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.plan_emitter import PlanEmitter  # noqa: E402
from tools.auto.state import StateStore, make_task  # noqa: E402


class _FakeBacklog:
    """Stands in for PrioritisedBacklog: emit() only needs to_state_tasks(),
    auto_tasks and manual_suggestions, and building a real one would drag in
    the whole architect pipeline for no extra coverage."""

    def __init__(self, tasks: list[dict]) -> None:
        self._tasks = tasks
        self.auto_tasks = tasks
        self.manual_suggestions: list = []

    def to_state_tasks(self, *, status: str = "todo") -> list[dict]:
        return self._tasks


def _valid(tid: str) -> dict:
    return make_task(
        id=tid, title=f"task {tid}", instruction="do it",
        acceptance_check="true",
    )


def _invalid(tid: str) -> dict:
    """Schema-valid enough to build, then corrupted the way an LLM-derived
    candidate can be: 'round' as a string where the schema demands int."""
    task = _valid(tid)
    task["round"] = "not an int"
    return task


@pytest.fixture()
def emitter(tmp_path: Path, monkeypatch) -> tuple[PlanEmitter, StateStore]:
    base = tmp_path / "repo"
    base.mkdir()
    state = StateStore(base / ".agent")
    state.initialise("test goal", base)

    git = MagicMock()
    git.commit.return_value = "abcdef1234567890"

    em = PlanEmitter(base, state, git)
    # to_improvements_md is exercised by its own tests; keep this one focused
    # on the upsert loop.
    monkeypatch.setattr(
        "tools.auto.plan_emitter.to_improvements_md", lambda backlog: "# plan\n"
    )
    return em, state


class TestUpsertLoopIsGuarded:
    def test_invalid_task_does_not_abort_emit(self, emitter) -> None:
        em, _ = emitter
        commit = em.emit(_FakeBacklog([_invalid("T-BAD")]))
        assert commit == "abcdef1234567890"

    def test_tasks_after_the_bad_one_are_still_persisted(self, emitter) -> None:
        """The behaviour that distinguishes a per-task guard from a guard
        wrapped around the whole loop: T-3 comes after the failure and must
        still reach plan.json."""
        em, state = emitter
        em.emit(_FakeBacklog([_valid("T-1"), _invalid("T-2"), _valid("T-3")]))

        ids = [t["id"] for t in state.all_tasks()]
        assert ids == ["T-1", "T-3"]

    def test_shortfall_is_recorded_in_the_run_log(self, emitter) -> None:
        """plan.json and IMPROVEMENTS.md now disagree by design — an operator
        reading the run log must be able to see why."""
        em, state = emitter
        em.emit(_FakeBacklog([_valid("T-1"), _invalid("T-2")]))

        log = (state.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "T-2" in log

    def test_commit_still_happens_after_a_rejection(self, emitter) -> None:
        """The surviving tasks must be committed, not left uncommitted on
        disk — that partial-but-uncommitted state was the original symptom."""
        em, _ = emitter
        em.emit(_FakeBacklog([_invalid("T-BAD"), _valid("T-OK")]))
        # git.commit is the MagicMock installed in the fixture.
        assert em._git.commit.call_count == 1

    def test_all_tasks_invalid_still_completes(self, emitter) -> None:
        em, state = emitter
        em.emit(_FakeBacklog([_invalid("T-1"), _invalid("T-2")]))
        assert state.all_tasks() == []


class TestHappyPathUnchanged:
    def test_all_valid_tasks_are_upserted(self, emitter) -> None:
        em, state = emitter
        em.emit(_FakeBacklog([_valid("T-1"), _valid("T-2")]))
        assert [t["id"] for t in state.all_tasks()] == ["T-1", "T-2"]

    def test_no_shortfall_line_when_nothing_was_rejected(self, emitter) -> None:
        """The guard must stay quiet on the happy path; a 'not added' line on
        every ordinary run is noise."""
        em, state = emitter
        em.emit(_FakeBacklog([_valid("T-1")]))

        log = (state.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "not added to plan.json" not in log
