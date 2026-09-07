"""B2 -- ``StateStore.apply_rewrite`` persisted any instruction verbatim.

``outer_loop`` calls ``apply_rewrite`` with ``new_task.get("instruction",
"")``. A truncated rewriter reply therefore replaced the live instruction
with an empty string, stamped ``original_instruction`` and bumped
``impl_version``. The empty body became the Coder's active task, and because
``impl_version`` had advanced, the real (already-failing) instruction was no
longer in the "previously tried" history the rewrite cap relies on.

Two properties are locked here:

* a blank or non-string instruction raises ``ValueError``;
* the rejection is total -- ``instruction``, ``impl_version`` AND
  ``original_instruction`` are all left exactly as they were. Validating
  after ``original_instruction`` has been stamped would freeze the v1
  baseline at the wrong value.

Without the fix the blank cases are silently accepted, and
``test_non_string_instruction_raises_value_error`` gets ``AttributeError``.
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
        "instruction": "ORIGINAL v1 instruction",
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


class TestBlankRewriteRejected:
    @pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
    def test_blank_instruction_raises_value_error(
        self, store: StateStore, bad: str
    ) -> None:
        with pytest.raises(ValueError, match="blank or non-string"):
            store.apply_rewrite("T-1", instruction=bad)

    def test_non_string_instruction_raises_value_error(
        self, store: StateStore
    ) -> None:
        """Not AttributeError from .strip() -- the caller catches ValueError."""
        with pytest.raises(ValueError):
            store.apply_rewrite("T-1", instruction=None)

    def test_previous_instruction_survives(self, store: StateStore) -> None:
        with pytest.raises(ValueError):
            store.apply_rewrite("T-1", instruction="  ")
        assert store.get_task("T-1")["instruction"] == "ORIGINAL v1 instruction"

    def test_impl_version_is_not_bumped(self, store: StateStore) -> None:
        with pytest.raises(ValueError):
            store.apply_rewrite("T-1", instruction="")
        assert store.get_task("T-1")["impl_version"] == 1

    def test_original_instruction_is_not_stamped_on_rejection(
        self, store: StateStore
    ) -> None:
        """The guard must run before ANY mutation.

        Stamping original_instruction and then raising leaves the v1 baseline
        frozen at whatever the instruction happened to be at rejection time,
        which is exactly the history corruption this fix exists to prevent.
        """
        with pytest.raises(ValueError):
            store.apply_rewrite("T-1", instruction="")
        assert "original_instruction" not in store.get_task("T-1")


class TestValidRewriteStillWorks:
    def test_valid_rewrite_applies_and_bumps_version(
        self, store: StateStore
    ) -> None:
        new_version = store.apply_rewrite("T-1", instruction="REWRITTEN v2")
        task = store.get_task("T-1")
        assert new_version == 2
        assert task["instruction"] == "REWRITTEN v2"
        assert task["impl_version"] == 2

    def test_v1_baseline_is_preserved_after_a_rejected_attempt(
        self, store: StateStore
    ) -> None:
        with pytest.raises(ValueError):
            store.apply_rewrite("T-1", instruction="")
        store.apply_rewrite("T-1", instruction="REWRITTEN v2")
        assert (
            store.get_task("T-1")["original_instruction"]
            == "ORIGINAL v1 instruction"
        )
