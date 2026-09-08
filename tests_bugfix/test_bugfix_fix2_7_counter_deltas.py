"""FIX-2 #7 — ``increment_task_counters`` never validated its delta arguments.

``_coerce_counter`` (added by "Bug7 and Bug17 fix after fix improvements")
repairs a malformed *stored* counter, so a ``"2"`` or ``null`` left in
plan.json no longer breaks the addition. The *incoming* deltas were still
unchecked, so a caller bug reached the ``+`` unguarded — a non-int delta
raised ``TypeError`` out of a persistence helper and aborted an otherwise
healthy task loop, and a float delta was silently written into an int field.

``_coerce_delta`` gives the delta side the same repair-rather-than-raise
treatment: warn and treat as 0 (a no-op), never abort the run over
bookkeeping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore, _coerce_delta, make_task  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / ".agent")
    s.initialise("goal", tmp_path)
    s.upsert_task(make_task(
        id="T-1", title="t", instruction="do", target_files=[],
        acceptance_check="true",
    ))
    return s


class TestCoerceDelta:
    def test_valid_int_passes_through(self) -> None:
        assert _coerce_delta(3, "attempt_delta") == 3

    def test_zero_passes_through(self) -> None:
        assert _coerce_delta(0, "round_delta") == 0

    def test_negative_int_passes_through(self) -> None:
        assert _coerce_delta(-1, "attempt_delta") == -1

    @pytest.mark.parametrize("bad", ["1", 1.5, None, [1], {"a": 1}])
    def test_malformed_delta_becomes_a_no_op(self, bad) -> None:
        assert _coerce_delta(bad, "attempt_delta") == 0

    def test_bool_is_rejected(self) -> None:
        """bool is an int subclass, but no counter delta means True."""
        assert _coerce_delta(True, "attempt_delta") == 0


class TestIncrementTaskCounters:
    def test_normal_increment_is_unaffected(self, store: StateStore) -> None:
        store.increment_task_counters("T-1", attempt_delta=2, round_delta=1)
        t = store.get_task("T-1")
        assert (t["attempt"], t["round"]) == (2, 1)

    def test_non_numeric_delta_does_not_raise(self, store: StateStore) -> None:
        """Without the fix this raises TypeError out of the task loop."""
        store.increment_task_counters("T-1", attempt_delta="oops")
        assert store.get_task("T-1")["attempt"] == 0

    def test_float_delta_does_not_corrupt_the_int_field(
        self, store: StateStore
    ) -> None:
        store.increment_task_counters("T-1", attempt_delta=0.5)
        attempt = store.get_task("T-1")["attempt"]
        assert isinstance(attempt, int) and not isinstance(attempt, bool)
        assert attempt == 0

    def test_one_bad_delta_does_not_discard_the_other(
        self, store: StateStore
    ) -> None:
        store.increment_task_counters("T-1", attempt_delta=None, round_delta=2)
        t = store.get_task("T-1")
        assert (t["attempt"], t["round"]) == (0, 2)

    def test_unknown_task_still_raises(self, store: StateStore) -> None:
        """The fix must not swallow a genuine caller error."""
        with pytest.raises(ValueError):
            store.increment_task_counters("GHOST", attempt_delta=1)
