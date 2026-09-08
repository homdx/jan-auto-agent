"""FIX-2 #2 -- a non-numeric ``impl_version`` crashed the outer loop.

``OuterLoop.run_task`` seeded its rewrite counter from the persisted plan
value with a bare ``int(impl_version or 1)``. plan.json is a hand-writable
file, so a value like ``"abc"`` -- or the JSON null that Architect's
``_to_int_or_none`` happily writes for an unparseable line anchor -- raised
``ValueError`` before the first round even started.

That raised out of ``run_task``, the per-task body of the whole ``--auto``
loop, so one bad integer in one task's plan entry dropped every task still
pending in plan.json.

The seed is a counter, not a piece of state to trust: a value that cannot
be read as an integer means "we don't know how many rewrites happened",
which is the same as "we don't know", so seed from zero and let
``max_rewrites`` enforce the cap on the rewrites this process actually
observes. The persisted value is left untouched -- nothing about it can be
safely repaired in place.

Without the fix every test in ``TestNonNumericImplVersion`` raises
``ValueError: invalid literal for int()`` instead of returning a result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.outer_loop import OuterLoop  # noqa: E402
from tools.auto.state import StateStore, STATUS_DONE  # noqa: E402


def _task() -> dict:
    return {
        "id": "T-1",
        "title": "Some task",
        "instruction": "Do the thing",
        "target_files": [],
        "acceptance_check": "true",
        "status": "todo",
        "dependencies": [],
        "attempt": 0,
        "round": 0,
        "cited_locations": [],
    }


def _seed_task(store: StateStore, impl_version=1) -> dict:
    """Insert a task, then force ``impl_version`` to *impl_version*.

    ``upsert_task`` type-checks the value, so a corrupt value can only get
    into plan.json from outside the current write path: a plan.json written
    by an older version of this code, or a hand edit. The seed mutates the
    store's plan directly to reproduce that, which is the case the bug is
    about -- a run must survive a corrupt plan.json it finds on disk.
    """
    store.upsert_task(_task())
    store._plan["tasks"][0]["impl_version"] = impl_version
    store._save_plan()
    return store.get_task("T-1")


class _PassingInnerLoop:
    """Inner loop whose first round passes, so the outer loop returns fast."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_task(self, task, base_dir, **kwargs):
        self.calls.append(task)
        return SimpleNamespace(passed=True, attempts_used=1)


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / ".agent")
    s.initialise("goal", tmp_path)
    return s


@pytest.fixture()
def outer_pair(store: StateStore) -> tuple[OuterLoop, _PassingInnerLoop]:
    inner = _PassingInnerLoop()
    return OuterLoop(inner, store, max_rounds=1), inner


class TestNonNumericImplVersion:
    @pytest.mark.parametrize("impl_version", ["abc", "2.5", "v2"])
    def test_non_numeric_impl_version_does_not_crash(
        self, impl_version, store: StateStore, tmp_path: Path, outer_pair
    ) -> None:
        task = _seed_task(store, impl_version=impl_version)
        outer, _inner = outer_pair
        result = outer.run_task(task, tmp_path)

        # The run completed and the task is DONE -- not dropped, not BLOCKED.
        assert result.passed is True
        assert store.get_task("T-1")["status"] == STATUS_DONE

    def test_int_impl_version_is_unaffected(
        self, store: StateStore, tmp_path: Path, outer_pair
    ) -> None:
        """The normal case must keep working exactly as before."""
        task = _seed_task(store, impl_version=1)
        outer, inner = outer_pair
        result = outer.run_task(task, tmp_path)

        assert result.passed is True
        assert len(inner.calls) == 1

    def test_numeric_string_impl_version_does_not_crash(
        self, store: StateStore, tmp_path: Path, outer_pair
    ) -> None:
        """A digit string is the closest thing to a valid value; read it as
        the integer it encodes rather than treating it as unknown."""
        task = _seed_task(store, impl_version="3")
        outer, _inner = outer_pair
        result = outer.run_task(task, tmp_path)

        assert result.passed is True

    def test_corrupt_version_is_not_overwritten(
        self, store: StateStore, tmp_path: Path, outer_pair
    ) -> None:
        """The fix seeds the counter locally; it must not rewrite plan.json.

        Repairing the persisted value in place would silently invent a history
        for a task whose real rewrite count is unknown.
        """
        task = _seed_task(store, impl_version="abc")
        outer, _inner = outer_pair
        outer.run_task(task, tmp_path)
        assert store.get_task("T-1")["impl_version"] == "abc"
