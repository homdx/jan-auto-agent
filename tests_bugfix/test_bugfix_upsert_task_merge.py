"""A1: StateStore.upsert_task() must MERGE, not wholesale-replace.

Reachable path: plan_emitter.py and pipeline.py re-emit the ENTIRE backlog
on every plan phase (backlog.to_state_tasks() yields minimal dicts:
status="todo", round=0, attempt=0, impl_version=1, no commit key), and
bug_fix_loop.py upserts a *deterministic* fix id (BUG-FIX-<root>), so a
second regression on the same root task re-upserts the same id.

Before the fix, that call silently:
  * reverted a DONE task to TODO (work redone from scratch),
  * reset round/attempt to 0,
  * deleted the commit key recorded by commit_on_success,
  * dropped impl_version to 1 — defeating the LOOP-2/LOOP-3 cross-resume
    rewrite cap that exists precisely to bound rewrites.

No error was raised anywhere; the run just quietly redid finished work.
"""

from pathlib import Path

import pytest

from tools.auto.state import StateStore, make_task, STATUS_DONE, STATUS_TODO


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / ".agent")
    store.initialise("goal", tmp_path)
    return store


def _minimal(task_id: str = "AUTO-T1", **extra) -> dict:
    """The exact shape backlog.to_state_tasks() emits."""
    return make_task(
        id=task_id, title="replanned title", instruction="replanned instruction",
        target_files=["src/new.py"], acceptance_check="pytest -q",
        status=STATUS_TODO, round=0, attempt=0, impl_version=1,
        cited_locations=[], dependencies=[], **extra,
    )


class TestUpsertMergesInsteadOfReplacing:
    def test_done_task_not_reverted_to_todo(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="original", instruction="original instruction",
            target_files=["src/a.py"], acceptance_check="pytest tests -q",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="abc123def")
        store.increment_task_counters("AUTO-T1", round_delta=3)
        store.increment_task_counters("AUTO-T1", attempt_delta=2)

        store.upsert_task(_minimal("AUTO-T1"))

        t = store.get_task("AUTO-T1")
        assert t["status"] == STATUS_DONE
        assert t["commit"] == "abc123def"
        assert t["round"] == 3
        assert t["attempt"] == 2

    def test_impl_version_not_reset(self, tmp_path):
        """impl_version is the LOOP-2/LOOP-3 rewrite cap — losing it reopens
        an unbounded rewrite loop across resumes."""
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="v1", impl_version=3,
        ))
        store.upsert_task(_minimal("AUTO-T1"))
        assert store.get_task("AUTO-T1")["impl_version"] == 3

    def test_original_instruction_preserved(self, tmp_path):
        """outer_loop._build_impl_history reads original_instruction to label
        the v1 baseline after a later rewrite overwrites 'instruction'."""
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="rewritten text",
            original_instruction="the true v1 baseline",
        ))
        store.upsert_task(_minimal("AUTO-T1"))
        t = store.get_task("AUTO-T1")
        assert t["original_instruction"] == "the true v1 baseline"
        # the re-plan's own instruction text does apply
        assert t["instruction"] == "replanned instruction"

    def test_plain_fields_still_update(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="old title", instruction="old",
        ))
        store.upsert_task(_minimal("AUTO-T1"))
        t = store.get_task("AUTO-T1")
        assert t["title"] == "replanned title"
        assert t["instruction"] == "replanned instruction"
        assert t["target_files"] == ["src/new.py"]
        assert len(store.all_tasks()) == 1   # no duplicate

    def test_new_task_is_appended(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(_minimal("AUTO-T1"))
        store.upsert_task(_minimal("AUTO-T2"))
        assert {t["id"] for t in store.all_tasks()} == {"AUTO-T1", "AUTO-T2"}

    def test_merge_persists_to_disk(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="deadbeef")
        store.upsert_task(_minimal("AUTO-T1"))

        import json
        plan = json.loads((tmp_path / ".agent" / "plan.json").read_text())
        assert plan["tasks"][0]["status"] == STATUS_DONE
        assert plan["tasks"][0]["commit"] == "deadbeef"


class TestIncomingNewerValuesWin:
    def test_higher_impl_version_applied(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i", impl_version=1,
        ))
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i", impl_version=4,
        ))
        assert store.get_task("AUTO-T1")["impl_version"] == 4

    def test_higher_round_and_attempt_applied(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i", round=5, attempt=2,
        ))
        t = store.get_task("AUTO-T1")
        assert t["round"] == 5
        assert t["attempt"] == 2

    def test_missing_commit_does_not_erase_existing_one(self, tmp_path):
        """An incoming dict without the key must not delete it, and an empty
        incoming commit (no-git / empty-diff success) must not either."""
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="abc123")
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i", status=STATUS_DONE,
            commit="",
        ))
        assert store.get_task("AUTO-T1")["commit"] == "abc123"

    def test_explicit_empty_commit_with_downgrade_flag_applies(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="abc123")
        store.upsert_task(
            make_task(
                id="AUTO-T1", title="t", instruction="i",
                status=STATUS_TODO, commit="",
            ),
            allow_downgrade=True,
        )
        assert store.get_task("AUTO-T1")["status"] == STATUS_TODO


class TestDowngradeGuard:
    def test_done_to_todo_requires_explicit_flag(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE)
        store.upsert_task(_minimal("AUTO-T1"))
        assert store.get_task("AUTO-T1")["status"] == STATUS_DONE

    def test_explicit_flag_allows_downgrade(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE)
        store.upsert_task(_minimal("AUTO-T1"), allow_downgrade=True)
        assert store.get_task("AUTO-T1")["status"] == STATUS_TODO

    def test_done_to_blocked_also_guarded(self, tmp_path):
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("AUTO-T1", STATUS_DONE)
        store.upsert_task(make_task(
            id="AUTO-T1", title="t", instruction="i", status="blocked",
        ))
        assert store.get_task("AUTO-T1")["status"] == STATUS_DONE

    def test_blocked_to_todo_is_not_a_downgrade(self, tmp_path):
        """BugFixLoop parks fix tasks BLOCKED and a later authorised attempt
        re-upserts them as TODO through the normal path — that must keep
        working, or the documented operator retry becomes impossible."""
        store = _store(tmp_path)
        store.upsert_task(make_task(
            id="BUG-FIX-AUTO-T1", title="t", instruction="i",
        ))
        store.set_task_status("BUG-FIX-AUTO-T1", "blocked")
        store.upsert_task(_minimal("BUG-FIX-AUTO-T1"))
        assert store.get_task("BUG-FIX-AUTO-T1")["status"] == STATUS_TODO

    def test_upsert_rejects_invalid_task(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises((ValueError, KeyError)):
            store.upsert_task({"id": "X"})
