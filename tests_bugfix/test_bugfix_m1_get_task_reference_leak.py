"""M1 — a task dict handed out by a read accessor must not reach disk.

M1 was filed as "``_save_plan()`` writes with zero validation", and its own
premise said the risk was theoretical: every call site is a validated setter,
and ``self._plan`` never escapes the class.

The second half of that was false. ``get_task`` returned the live dict out of
``self._plan``; ``all_tasks`` returned a *shallow* copy (a new list holding the
same dicts); ``resume_info`` returned live dicts in two of its three keys. So a
caller could write any value into a task and the next validated setter
serialised it — the setters validate their own arguments, not the plan they are
about to write. Confirmed before the fix::

    t = store.get_task("T1")
    t["status"] = 12345                                  # no validator sees this
    store.increment_task_counters("T1", round_delta=1)   # setter -> _save_plan()
    # plan.json now holds status: 12345, not one of the four legal values

The fix is copy-on-read (``StateStore._detached``) rather than validate-on-write:
it closes the path outright instead of re-checking a plan that outside code can
still reach, and it costs a small dict copy per accessor call.

These tests fail if any accessor starts leaking again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore  # noqa: E402

_TASK = {
    "id": "T1", "title": "t", "instruction": "i", "acceptance_check": "a",
    "target_files": ["x.py"], "status": "todo", "attempt": 0, "round": 0,
    "cluster": "c", "cited_locations": [], "dependencies": [], "impl_version": 1,
}


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / ".agent")
    s.initialise(goal="g", base_dir=tmp_path)
    s.upsert_task(dict(_TASK))
    return s


def _on_disk(tmp_path: Path) -> dict:
    plan = json.loads((tmp_path / ".agent" / "plan.json").read_text(encoding="utf-8"))
    return plan["tasks"][0]


# ── the reported escape path ─────────────────────────────────────────────────

def test_mutating_a_get_task_result_cannot_reach_disk(store, tmp_path) -> None:
    """The exact reproduction from the audit."""
    store.get_task("T1")["status"] = 12345
    store.increment_task_counters("T1", round_delta=1)   # a validated setter
    assert _on_disk(tmp_path)["status"] == "todo"


def test_mutating_an_all_tasks_element_cannot_reach_disk(store, tmp_path) -> None:
    """list() was a shallow copy — the list was new, the dicts were not."""
    store.all_tasks()[0]["status"] = "corrupt"
    store.increment_task_counters("T1", round_delta=1)
    assert _on_disk(tmp_path)["status"] == "todo"


@pytest.mark.parametrize("key", ["pending", "in_progress"])
def test_mutating_a_resume_info_task_cannot_reach_disk(store, tmp_path, key) -> None:
    """resume_info returns task dicts in two of its three keys."""
    store.set_task_status("T1", "in_progress")
    for task in store.resume_info()[key]:
        task["title"] = "corrupt"
    store.increment_task_counters("T1", round_delta=1)
    assert _on_disk(tmp_path)["title"] == "t"


# ── the accessors must not be sharing objects at all ─────────────────────────

def test_accessors_do_not_return_the_live_object(store) -> None:
    live = store._plan["tasks"][0]
    assert store.get_task("T1") is not live
    assert store.all_tasks()[0] is not live
    assert store.resume_info()["pending"][0] is not live


def test_two_reads_are_independent_of_each_other(store) -> None:
    """Not just detached from the plan — detached from each other, so one
    caller's scratch edits cannot surprise another caller."""
    a, b = store.get_task("T1"), store.get_task("T1")
    a["title"] = "changed"
    assert b["title"] == "t"


def test_nested_values_are_copied_too(store, tmp_path) -> None:
    """A shallow copy would leave lists and dicts inside the task shared, so
    the leak would survive one level down."""
    store.get_task("T1")["target_files"].append("injected.py")
    store.increment_task_counters("T1", round_delta=1)
    assert _on_disk(tmp_path)["target_files"] == ["x.py"]


# ── the fix must not break the thing it guards ───────────────────────────────

def test_reads_still_return_the_current_value(store) -> None:
    store.set_task_status("T1", "done")
    assert store.get_task("T1")["status"] == "done"
    assert store.all_tasks()[0]["status"] == "done"


def test_setters_still_persist(store, tmp_path) -> None:
    store.increment_task_counters("T1", attempt_delta=1, round_delta=2)
    disk = _on_disk(tmp_path)
    assert (disk["attempt"], disk["round"]) == (1, 2)


def test_missing_task_still_returns_none(store) -> None:
    assert store.get_task("NOPE") is None
