"""tests_bugfix/test_bugfix_upsert_task_merge_no_downgrade.py

StateStore.upsert_task() used to do ``tasks[i] = task`` — a wholesale replace
with no merge and no downgrade guard.

That is reachable from current code, not theoretical:

  * tools/auto/plan_emitter.py:137 and tools/auto/pipeline.py:452 iterate the
    entire ``backlog.to_state_tasks()`` on every re-plan.  ``to_state_tasks``
    emits MINIMAL dicts: ``status="todo"``, ``round=0``, ``attempt=0``,
    ``impl_version=1``, no ``commit``, no ``original_instruction``.
  * tools/auto/bug_fix_loop.py:660 upserts the synthetic fix task with
    ``fix_id = f"{_FIX_PREFIX}{root_id}"`` (line 469), which is deterministic,
    so a second regression on the same root task re-upserts the same id.

Result of a re-upsert over a completed task: status silently reverts to todo,
round/attempt reset to 0, the commit key recorded at
commit_on_success.py:165 is deleted outright, and impl_version drops to 1 —
which defeats the cross-resume rewrite cap LOOP-2/LOOP-3 exist to enforce. No
error is raised anywhere; the run simply redoes finished work and loses the git
linkage.

Fix under test: merge instead of replace — keep the existing status, round,
attempt, impl_version, original_instruction and commit unless the incoming dict
explicitly carries a newer value — and add a downgrade guard so done -> todo
requires the caller to say so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore, make_task


def _minimal_replan_task(task_id: str) -> dict:
    """Shape of a dict produced by PrioritisedBacklog.to_state_tasks().

    Minimal on purpose: no commit, no original_instruction, and the reset
    counters that a re-plan always emits.
    """
    return make_task(
        id=task_id,
        title="title after re-plan",
        instruction="instruction after re-plan",
        target_files=["b.py"],
        acceptance_check="true",
        status="todo",
        round=0,
        attempt=0,
        impl_version=1,
        cited_locations=[{"file": "b.py", "symbol": "f", "line_start": 1,
                          "line_end": 2, "new_file": False}],
    )


def _done_task(task_id: str) -> dict:
    return make_task(
        id=task_id,
        title="original title",
        instruction="original instruction",
        target_files=["a.py"],
        acceptance_check="true",
        status="done",
        round=2,
        attempt=1,
        impl_version=3,
        cited_locations=[{"file": "a.py", "symbol": "f", "line_start": 1,
                          "line_end": 9, "new_file": False}],
        commit="abc1234",
        original_instruction="original instruction",
    )


def _store(tmp_path: Path) -> StateStore:
    agent = tmp_path / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    store = StateStore(agent)
    store.initialise("goal", tmp_path)
    return store


class TestReupsertDoesNotClobberCompletedTask:
    def test_replan_does_not_downgrade_done_to_todo(self, tmp_path):
        """THE BUG: a re-plan re-upserting the same id must not reopen a
        completed task."""
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))
        assert store.get_task("AUTO-T1")["status"] == "done"

        store.upsert_task(_minimal_replan_task("AUTO-T1"))

        t = store.get_task("AUTO-T1")
        assert t["status"] == "done", (
            "a re-plan emitting status='todo' must not reopen a DONE task — "
            "that silently re-runs finished work"
        )

    def test_counters_are_monotonic(self, tmp_path):
        """round/attempt/impl_version must never move backwards; impl_version=1
        would defeat the LOOP-2/LOOP-3 cross-resume rewrite cap."""
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))

        store.upsert_task(_minimal_replan_task("AUTO-T1"))

        t = store.get_task("AUTO-T1")
        assert t["round"] == 2, "round must not reset to 0 on re-upsert"
        assert t["attempt"] == 1, "attempt must not reset to 0 on re-upsert"
        assert t["impl_version"] == 3, (
            "impl_version must not drop to 1 — the rewrite cap depends on it"
        )

    def test_commit_and_original_instruction_survive(self, tmp_path):
        """The commit sha is written by commit_on_success.py:165; dropping it
        severs the git linkage between plan.json and the real commit."""
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))

        store.upsert_task(_minimal_replan_task("AUTO-T1"))

        t = store.get_task("AUTO-T1")
        assert t.get("commit") == "abc1234", (
            "commit must not be deleted outright by a re-upsert"
        )
        assert t.get("original_instruction") == "original instruction", (
            "original_instruction is only recorded once and must survive"
        )

    def test_incoming_values_still_merge_in(self, tmp_path):
        """The fix is a merge, not a freeze: fields the incoming dict carries
        for real must still land, and existing content must not be duplicated."""
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))

        store.upsert_task(_minimal_replan_task("AUTO-T1"))

        t = store.get_task("AUTO-T1")
        assert t["title"] == "title after re-plan"
        assert t["instruction"] == "instruction after re-plan"
        assert t["target_files"] == ["b.py"]
        assert t["cited_locations"][0]["file"] == "b.py"
        assert len(store.all_tasks()) == 1, "no duplicate entry"


class TestDowngradeRequiresExplicitFlag:
    def test_done_to_todo_needs_allow_downgrade(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))

        store.upsert_task(_minimal_replan_task("AUTO-T1"))
        assert store.get_task("AUTO-T1")["status"] == "done"

        store.upsert_task(_minimal_replan_task("AUTO-T1"), allow_downgrade=True)
        assert store.get_task("AUTO-T1")["status"] == "todo", (
            "an explicit downgrade must be honoured when the caller asks for it"
        )

    def test_incoming_higher_values_win(self, tmp_path):
        """A newer impl_version / round in the incoming dict must override."""
        store = _store(tmp_path)
        store.upsert_task(_done_task("AUTO-T1"))

        higher = _minimal_replan_task("AUTO-T1")
        higher["impl_version"] = 7
        higher["round"] = 5
        store.upsert_task(higher)

        t = store.get_task("AUTO-T1")
        assert t["impl_version"] == 7
        assert t["round"] == 5


class TestSanity:
    def test_new_task_still_appended(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(_minimal_replan_task("AUTO-T9"))
        assert store.get_task("AUTO-T9") is not None
        assert store.get_task("AUTO-T9")["status"] == "todo"
        assert len(store.all_tasks()) == 1

    def test_title_update_still_works(self, tmp_path):
        """tests/test_auto_2.py contract: a plain field update still applies."""
        store = _store(tmp_path)
        first = make_task(id="AUTO-T1", title="t1", instruction="i")
        store.upsert_task(first)
        updated = dict(first)
        updated["title"] = "Updated title"
        store.upsert_task(updated)
        assert store.get_task("AUTO-T1")["title"] == "Updated title"
        assert len(store.all_tasks()) == 1

    def test_second_regression_same_root_id(self, tmp_path):
        """bug_fix_loop.py:660 re-upserts a deterministic BUG-FIX-<root> id, so
        the second regression on the same root must not undo the first fix."""
        store = _store(tmp_path)
        first = make_task(id="BUG-FIX-AUTO-T1", title="fix 1", instruction="i")
        store.upsert_task(first)
        store.set_task_status("BUG-FIX-AUTO-T1", "done", commit="cafebabe",
                              round=1, attempt=1, impl_version=2)

        second = make_task(id="BUG-FIX-AUTO-T1", title="fix 2", instruction="i2")
        store.upsert_task(second)

        t = store.get_task("BUG-FIX-AUTO-T1")
        assert t["status"] == "done"
        assert t.get("commit") == "cafebabe"
        assert t["impl_version"] == 2
        assert t["title"] == "fix 2"
