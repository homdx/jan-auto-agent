"""tests_bugfix/test_bugfix_counter_schema_guard.py

BUG 7 — counter fields were incremented without any schema guard.

``StateStore.set_task_status`` validates the fields it merges (B1's
``_validate_extra_task_fields``), but the two counter write paths —
``increment_task_counters`` and ``increment_impl_version`` (plus
``apply_rewrite``, which bumps ``impl_version`` too) — never go through
``**extra_fields`` and so were never covered by that guard. They read the
counter straight off a task that came from disk and added a delta to it.

A legacy or hand-edited ``plan.json`` carrying ``"attempt": "2"``,
``null`` or ``true`` therefore raised an unhandled ``TypeError`` in the
middle of a run, after work had already been committed.

The fix repairs the value in place (to the same default ``make_task``
would have used) and reports it, rather than raising: a counter is
bookkeeping, and losing a run over one is a worse outcome than resetting
it. These tests pin both halves — no raise, AND the repaired value is
actually persisted and reported, since a silently reset attempt counter is
indistinguishable from a task that was never retried.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore, make_task  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("test goal", tmp_path)
    return st


def _task_with(store: StateStore, field: str, value) -> None:
    """Insert a valid task, then corrupt one counter the way a hand-edited
    plan.json would — bypassing upsert_task's validation on purpose."""
    store.upsert_task(make_task(
        id="T-1", title="t", instruction="i", acceptance_check="true",
    ))
    store._plan["tasks"][0][field] = value


MALFORMED = [
    pytest.param("2", id="string"),
    pytest.param(None, id="null"),
    pytest.param(True, id="bool"),
    pytest.param([1], id="list"),
]


class TestIncrementTaskCounters:
    @pytest.mark.parametrize("bad", MALFORMED)
    def test_malformed_attempt_does_not_raise(self, store: StateStore, bad) -> None:
        _task_with(store, "attempt", bad)
        store.increment_task_counters("T-1", attempt_delta=1)
        assert store.get_task("T-1")["attempt"] == 1

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_malformed_round_does_not_raise(self, store: StateStore, bad) -> None:
        _task_with(store, "round", bad)
        store.increment_task_counters("T-1", round_delta=1)
        assert store.get_task("T-1")["round"] == 1

    def test_repair_is_persisted_to_disk(self, store: StateStore, tmp_path: Path) -> None:
        """The repaired counter must survive a resume — an in-memory-only fix
        would hand the same TypeError to the next process."""
        _task_with(store, "attempt", "7")
        store.increment_task_counters("T-1", attempt_delta=1)

        on_disk = json.loads((tmp_path / ".agent" / "plan.json").read_text(encoding="utf-8"))
        assert on_disk["tasks"][0]["attempt"] == 1

    def test_repair_is_reported(self, store: StateStore, caplog) -> None:
        _task_with(store, "attempt", "7")
        with caplog.at_level(logging.WARNING, logger="tools.auto.state"):
            store.increment_task_counters("T-1", attempt_delta=1)

        messages = [r.getMessage() for r in caplog.records]
        assert any("attempt" in m and "T-1" in m for m in messages), (
            "a reset counter looks exactly like a task that was never "
            "retried — it must not be repaired silently"
        )

    def test_valid_counter_is_untouched_and_quiet(self, store: StateStore, caplog) -> None:
        """The guard must not disturb the happy path, nor log on it."""
        _task_with(store, "attempt", 4)
        with caplog.at_level(logging.WARNING, logger="tools.auto.state"):
            store.increment_task_counters("T-1", attempt_delta=1)

        assert store.get_task("T-1")["attempt"] == 5
        assert not any("attempt" in r.getMessage() for r in caplog.records)

    def test_missing_counter_still_starts_at_zero(self, store: StateStore) -> None:
        """An absent key is not malformed — the pre-existing default applies
        and nothing is reported."""
        _task_with(store, "attempt", 0)
        del store._plan["tasks"][0]["attempt"]
        store.increment_task_counters("T-1", attempt_delta=1)
        assert store.get_task("T-1")["attempt"] == 1


class TestIncrementImplVersion:
    @pytest.mark.parametrize("bad", MALFORMED)
    def test_malformed_impl_version_does_not_raise(self, store: StateStore, bad) -> None:
        _task_with(store, "impl_version", bad)
        assert store.increment_impl_version("T-1") == 2

    def test_valid_impl_version_is_untouched(self, store: StateStore) -> None:
        _task_with(store, "impl_version", 3)
        assert store.increment_impl_version("T-1") == 4

    def test_apply_rewrite_tolerates_malformed_impl_version(self, store: StateStore) -> None:
        """apply_rewrite bumps the same counter through its own code path —
        guarding only increment_impl_version would leave this one crashing."""
        _task_with(store, "impl_version", "1")
        assert store.apply_rewrite("T-1", instruction="new instruction") == 2
        assert store.get_task("T-1")["instruction"] == "new instruction"


def test_unknown_task_still_raises(store: StateStore) -> None:
    """The guard must not swallow the genuinely exceptional case: a counter
    bump for a task that is not in the plan is a caller bug, not bad data."""
    with pytest.raises(ValueError):
        store.increment_task_counters("T-MISSING", attempt_delta=1)
    with pytest.raises(ValueError):
        store.increment_impl_version("T-MISSING")
