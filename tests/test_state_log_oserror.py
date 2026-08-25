"""tests/test_state_log_oserror.py — report REPORT20260824.md §4, item 7.

Before the fix, StateStore.log() was an unguarded file write — any OSError
(disk full, permissions, run.log's parent directory removed mid-run)
propagated straight out of every .log() call site, 62 of them across the
codebase. The most damaging is inside outer_loop.py's AUTO-OUTER-GUARD-1
handler, whose entire documented purpose is "any exception
inner_loop.run_task doesn't already handle itself shouldn't crash the whole
multi-task run" — that handler itself calls self.state.log(...) unguarded,
so an OSError from logging could defeat the very safety net it exists to
provide.

After the fix, StateStore.log() catches OSError, logs a warning via the
module logger, and returns normally instead of raising — the dropped line
is diagnostic-only, not authoritative state (contrast with
_atomic_write/_save_plan/_save_progress, which intentionally do NOT
swallow OSError, since a silently failed plan/progress write would be
worse than a loud one).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore


def _initialised_store(tmp_path: Path) -> StateStore:
    agent = tmp_path / ".agent"
    store = StateStore(agent)
    store.initialise("test goal", tmp_path)
    return store


def test_log_survives_missing_directory(tmp_path, caplog):
    """.log() must not raise even if its target directory is gone."""
    store = _initialised_store(tmp_path)
    shutil.rmtree(store.agent_dir)

    with caplog.at_level("WARNING", logger="tools.auto.state"):
        # Must not raise OSError/FileNotFoundError.
        store.log("this must not crash the run")

    # A warning should be emitted so the failure isn't silently invisible.
    assert any("failed to write" in rec.message for rec in caplog.records)


def test_log_survives_missing_directory_no_caplog_dependency(tmp_path):
    """Same as above, without relying on caplog/logger propagation config."""
    store = _initialised_store(tmp_path)
    shutil.rmtree(store.agent_dir)
    try:
        store.log("still must not crash")
    except OSError as exc:  # pragma: no cover - only hit if bug regresses
        pytest.fail(f"StateStore.log() raised OSError instead of degrading: {exc}")


def test_log_still_writes_normally_when_directory_exists(tmp_path):
    """No regression on the happy path: a normal log() call still appends."""
    store = _initialised_store(tmp_path)
    store.log("hello world")
    log_path = store.agent_dir / "run.log"
    assert log_path.exists()
    assert "hello world" in log_path.read_text(encoding="utf-8")


def test_log_from_outer_guard_style_handler_does_not_escalate(tmp_path):
    """Simulates AUTO-OUTER-GUARD-1's own call shape: a try/except Exception
    handler that itself calls .log() after catching an unrelated failure.
    The .log() call must not be able to defeat that handler's guarantee
    that the run keeps going.
    """
    store = _initialised_store(tmp_path)
    shutil.rmtree(store.agent_dir)

    caught_something_else = False
    try:
        try:
            raise RuntimeError("simulated inner_loop.run_task failure")
        except Exception as exc:  # noqa: BLE001 - mirrors AUTO-OUTER-GUARD-1
            store.log(f"task raised: {exc}")
            caught_something_else = True
    except OSError:  # pragma: no cover - only hit if bug regresses
        pytest.fail("log() inside the outer guard handler escalated to OSError")

    assert caught_something_else
