"""tests/test_auto_4.py — Tests for AUTO-A4: Run limits & safety.

Covers all ACs from the story:
  AC1: Global wall-clock cap (max_runtime_min) terminates the run gracefully.
  AC2: Tasks-per-session cap (max_tasks_per_run) terminates the run gracefully.
  AC3: Run stops gracefully (exit code 0), state saved (progress.json gets 'capped').
  AC4: Run is resumable after hitting either cap.
"""

import configparser
import sys
from pathlib import Path

import pytest

# Make project root importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import make_task, STATUS_DONE


def _write_config(path: Path, max_runtime: float = 0, max_tasks: int = 0) -> str:
    """Helper to generate a temporary agents.ini file with limits."""
    cfg = configparser.ConfigParser()
    cfg["auto"] = {
        "max_runtime_min": str(max_runtime),
        "max_tasks_per_run": str(max_tasks)
    }
    ini_path = path / "agents.ini"
    with ini_path.open("w", encoding="utf-8") as f:
        cfg.write(f)
    return str(ini_path)


class MockClock:
    """A predictable, deterministic clock for testing timeouts without sleeping."""
    def __init__(self, step_seconds: float = 0.0):
        self.time = 0.0
        self.step = step_seconds

    def __call__(self) -> float:
        current = self.time
        self.time += self.step
        return current


# ─────────────────────────────────────────────────────────────────────────────
# AC2 & AC3 — Task Cap
# ─────────────────────────────────────────────────────────────────────────────

def test_halts_at_task_cap(tmp_path):
    """Run must stop exactly when max_tasks_per_run is reached and save state."""
    config_path = _write_config(tmp_path, max_tasks=2)
    
    # 1. Setup existing state with 4 pending tasks
    ctrl_setup = AutoController("goal", tmp_path, config_path)
    ctrl_setup.state.initialise("goal", tmp_path)
    for i in range(4):
        ctrl_setup.state.upsert_task(make_task(id=f"T{i}", title=f"Task {i}", instruction="i"))
        
    # 2. Run the controller
    ctrl = AutoController("goal", tmp_path, config_path)
    exit_code = ctrl.run()
    
    # 3. Verify it exited 0, stopped after 2 tasks, and logged the correct reason
    assert exit_code == 0
    prog = ctrl.state.get_progress()
    
    assert prog["status"] == "capped"
    assert prog["stop_reason"] == "task_cap"
    assert prog["done_count"] == 2
    assert prog["pending_count"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC1 & AC3 — Runtime Cap
# ─────────────────────────────────────────────────────────────────────────────

def test_halts_at_runtime_cap(tmp_path):
    """Run must stop if execution time exceeds max_runtime_min (wall-clock cap)."""
    # 1 minute cap = 60 seconds
    config_path = _write_config(tmp_path, max_runtime=1.0)
    
    ctrl_setup = AutoController("goal", tmp_path, config_path)
    ctrl_setup.state.initialise("goal", tmp_path)
    for i in range(3):
        ctrl_setup.state.upsert_task(make_task(id=f"T{i}", title=f"Task {i}", instruction="i"))
        
    # Mock time to advance 40 seconds every time it is checked.
    # Start: 0s. Task 1 check: 40s (Passes). Task 2 check: 80s (Fails, cap hit).
    clock = MockClock(step_seconds=40.0)
    
    ctrl = AutoController("goal", tmp_path, config_path, _time_fn=clock)
    exit_code = ctrl.run()
    
    assert exit_code == 0
    prog = ctrl.state.get_progress()
    
    assert prog["status"] == "capped"
    assert prog["stop_reason"] == "runtime_cap"
    assert prog["done_count"] == 1
    assert prog["pending_count"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Resumability
# ─────────────────────────────────────────────────────────────────────────────

def test_run_is_resumable_after_cap(tmp_path):
    """A capped run must be able to be resumed on the next execution."""
    config_path = _write_config(tmp_path, max_tasks=2)
    
    # Seed 3 tasks
    ctrl_setup = AutoController("goal", tmp_path, config_path)
    ctrl_setup.state.initialise("goal", tmp_path)
    for i in range(3):
        ctrl_setup.state.upsert_task(make_task(id=f"T{i}", title=f"Task {i}", instruction="i"))
        
    # Run 1: Hits cap of 2
    ctrl_1 = AutoController("goal", tmp_path, config_path)
    ctrl_1.run()
    assert ctrl_1.state.get_progress()["done_count"] == 2
    assert ctrl_1.state.get_progress()["status"] == "capped"
    
    # Run 2: Resumes and finishes the last task
    ctrl_2 = AutoController("goal", tmp_path, config_path)
    ctrl_2.run()
    
    prog = ctrl_2.state.get_progress()
    assert prog["done_count"] == 3
    assert prog["pending_count"] == 0
    assert prog["status"] == "idle"          # No longer capped
    assert "stop_reason" not in prog         # Cleared out


# ─────────────────────────────────────────────────────────────────────────────
# Limits Disabled (0)
# ─────────────────────────────────────────────────────────────────────────────

def test_run_completes_when_limits_disabled(tmp_path):
    """If limits are 0 (disabled), it should process everything and go idle."""
    config_path = _write_config(tmp_path, max_runtime=0, max_tasks=0)
    
    ctrl_setup = AutoController("goal", tmp_path, config_path)
    ctrl_setup.state.initialise("goal", tmp_path)
    for i in range(5):
        ctrl_setup.state.upsert_task(make_task(id=f"T{i}", title=f"Task {i}", instruction="i"))
        
    ctrl = AutoController("goal", tmp_path, config_path)
    ctrl.run()
    
    prog = ctrl.state.get_progress()
    assert prog["status"] == "idle"
    assert prog["done_count"] == 5
    assert prog.get("stop_reason") is None

if __name__ == "__main__":
    print("Run with: pytest tests/test_auto_4.py -v")