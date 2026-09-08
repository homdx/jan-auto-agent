"""B1 -- ``StateStore.set_task_status`` merged ``**extra_fields`` unchecked.

Before the fix, ``set_task_status(task_id, "todo", round="2")`` wrote the
string ``"2"`` straight into a field the task schema declares as ``int``.
Nothing complained at write time; the failure surfaced much later and far
away, when arithmetic on that field raised ``TypeError`` mid-run.

The fix type-checks the *incoming* fields only. It deliberately does NOT
re-validate the merged task: a legacy or hand-edited ``plan.json`` whose
tasks are missing a required field must keep accepting plain status writes,
otherwise an unreachable audit finding becomes a reachable mid-run abort.

Without the fix: ``test_wrong_typed_extra_field_is_rejected`` and its
siblings fail (no exception is raised and the bad value is persisted).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore  # noqa: E402


def _task(task_id: str = "T-1") -> dict:
    return {
        "id": task_id,
        "title": "Some task",
        "instruction": "Do the thing",
        "target_files": [],
        "acceptance_check": "true",
        "status": "todo",
        "dependencies": [],
        "attempt": 0,
        "round": 0,
        "cited_locations": [],
        "impl_version": 1,
    }


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / ".agent")
    s.initialise("goal", tmp_path)
    s.upsert_task(_task())
    return s


class TestExtraFieldValidation:
    def test_wrong_typed_extra_field_is_rejected(self, store: StateStore) -> None:
        with pytest.raises(ValueError, match="round"):
            store.set_task_status("T-1", "todo", round="2")

    def test_rejected_write_does_not_persist(self, store: StateStore) -> None:
        with pytest.raises(ValueError):
            store.set_task_status("T-1", "todo", round="2")
        assert store.get_task("T-1")["round"] == 0

    def test_wrong_typed_attempt_is_rejected(self, store: StateStore) -> None:
        with pytest.raises(ValueError, match="attempt"):
            store.set_task_status("T-1", "todo", attempt=None)

    def test_correctly_typed_extra_field_is_accepted(self, store: StateStore) -> None:
        store.set_task_status("T-1", "todo", round=3)
        assert store.get_task("T-1")["round"] == 3

    def test_free_form_extra_field_passes_through(self, store: StateStore) -> None:
        """Keys outside the required schema are bookkeeping, not schema."""
        store.set_task_status("T-1", "done", commit="abc123")
        assert store.get_task("T-1")["commit"] == "abc123"

    def test_plain_status_write_survives_an_incomplete_task(
        self, store: StateStore
    ) -> None:
        """The regression the narrow scope exists to prevent.

        A hand-edited plan.json can hold a task with a required field
        missing. Validating the merged task would reject this write; only
        validating the incoming fields keeps it working.
        """
        store._plan["tasks"][0].pop("cited_locations")
        store._save_plan()
        store.set_task_status("T-1", "in_progress")
        assert store.get_task("T-1")["status"] == "in_progress"
