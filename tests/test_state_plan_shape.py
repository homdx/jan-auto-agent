"""tests/test_state_plan_shape.py — plan.json shape must be checked on LOAD.

The task schema is enforced on WRITE (upsert_task -> _validate_task_schema)
and was never enforced on READ.  _load_json_with_backup recovers from a
corrupt file and, when recovery fails, raises a clear "plan.json ... is
corrupted" error — but a file that is valid JSON of the WRONG SHAPE sailed
past it and crashed later, raw and far from the cause:

    plan.json is a list      -> AttributeError: 'list' object has no attribute 'get'
    "tasks" is a string      -> TypeError: string indices must be integers
    task missing "status"    -> KeyError: 'status'

Reachable by any route that writes plan.json other than upsert_task: a hand
edit — which this project's own recovery advice invites when it tells
operators to reset a ticket status by hand — a partially-migrated schema, or a
file restored from an older version.  Every case now raises the same
RuntimeError the corruption path uses, naming the file and the reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore, make_task


def _store_with_plan(tmp_path: Path, plan) -> StateStore:
    agent = tmp_path / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    text = plan if isinstance(plan, str) else json.dumps(plan)
    (agent / "plan.json").write_text(text, encoding="utf-8")
    return StateStore(agent)


class TestPlanShapeOnLoad:
    def test_plan_is_a_list(self, tmp_path):
        store = _store_with_plan(tmp_path, [])
        with pytest.raises(RuntimeError, match="not a JSON object"):
            store.initialise("g", tmp_path)

    def test_plan_is_a_string(self, tmp_path):
        store = _store_with_plan(tmp_path, '"just a string"')
        with pytest.raises(RuntimeError, match="not a JSON object"):
            store.initialise("g", tmp_path)

    def test_tasks_is_not_a_list(self, tmp_path):
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": "oops"})
        with pytest.raises(RuntimeError, match="'tasks' field of type str"):
            store.initialise("g", tmp_path)

    def test_task_is_not_an_object(self, tmp_path):
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": ["oops"]})
        with pytest.raises(RuntimeError, match="task #0 is str"):
            store.initialise("g", tmp_path)

    def test_task_missing_required_field(self, tmp_path):
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": [{"id": "T1"}]})
        with pytest.raises(RuntimeError, match="task #0 \\(T1\\).*schema violation"):
            store.initialise("g", tmp_path)

    def test_task_field_wrong_type(self, tmp_path):
        task = make_task(id="T1", title="t", instruction="i",
                         target_files=["a.py"], acceptance_check="true")
        task["status"] = 42
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": [task]})
        with pytest.raises(RuntimeError, match="must be str"):
            store.initialise("g", tmp_path)

    def test_error_names_the_offending_task(self, tmp_path):
        good = make_task(id="T1", title="t", instruction="i",
                         target_files=["a.py"], acceptance_check="true")
        store = _store_with_plan(
            tmp_path, {"goal": "g", "tasks": [good, {"id": "T2-BROKEN"}]}
        )
        with pytest.raises(RuntimeError, match="task #1 \\(T2-BROKEN\\)"):
            store.initialise("g", tmp_path)

    def test_valid_plan_still_loads(self, tmp_path):
        """The guard must not reject legitimate state."""
        task = make_task(id="T1", title="t", instruction="i",
                         target_files=["a.py"], acceptance_check="true")
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": [task]})
        store.initialise("g", tmp_path)
        assert [t["id"] for t in store.all_tasks()] == ["T1"]

    def test_legacy_plan_without_impl_version_is_backfilled(self, tmp_path):
        """_validate_task_schema backfills impl_version — don't regress that."""
        task = make_task(id="T1", title="t", instruction="i",
                         target_files=["a.py"], acceptance_check="true")
        task.pop("impl_version", None)
        store = _store_with_plan(tmp_path, {"goal": "g", "tasks": [task]})
        store.initialise("g", tmp_path)
        assert store.get_task("T1")["impl_version"] == 1

    def test_corrupt_json_still_reports_corruption(self, tmp_path):
        """The pre-existing corruption path must be untouched."""
        store = _store_with_plan(tmp_path, "{ not json")
        with pytest.raises(RuntimeError, match="corrupted"):
            store.initialise("g", tmp_path)
