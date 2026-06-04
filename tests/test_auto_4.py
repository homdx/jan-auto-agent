"""tests/test_auto_4.py — AUTO-A4: run limits & safety.

Covers the story ACs:
  * RunLimits reads max_runtime_min / max_tasks_per_run / exec_timeout_sec
    from agents.ini [auto] (0 = disabled).
  * Wall-clock cap fires (tested with an injected fake clock — no sleeping).
  * Task cap fires after N tasks in a session.
  * On a cap the run stops GRACEFULLY: exit 0, progress status "capped",
    stop_reason persisted, run.log records it.
  * The run is RESUMABLE after a cap (done tasks are skipped next time).
  * No caps configured → run completes normally (status "idle").
  * Execution constraint exec_timeout_sec is exposed for the executor.
"""

import configparser
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import (
    AutoController, RunLimits, STOP_RUNTIME_CAP, STOP_TASK_CAP,
)
from tools.auto.state import StateStore, make_task, STATUS_DONE


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**auto_kw) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c["auto"] = {k: str(v) for k, v in auto_kw.items()}
    return c


def _seed_tasks(base_dir: Path, goal: str, n: int) -> None:
    """Create a fresh state with n pending tasks."""
    store = StateStore(base_dir / ".agent")
    store.initialise(goal, base_dir)
    for i in range(1, n + 1):
        store.upsert_task(make_task(id=f"AUTO-T{i}", title=f"task {i}",
                                    instruction="x", target_files=["f.py"]))


class _FakeClock:
    """Monotonic clock we can advance by hand."""
    def __init__(self, start=1000.0):
        self.t = start
    def __call__(self):
        return self.t
    def advance(self, secs):
        self.t += secs


@pytest.fixture(autouse=True)
def mock_inner_loop_pipeline(monkeypatch):
    """Fixture to mock out real LLM API loops during safety/limit rule testing."""
    from tools.auto.inner_loop import InnerLoopResult, AttemptRecord
    class FakeInnerLoop:
        def __init__(self, config, base_dir):
            pass
        def run_task(self, task, base_dir, prior_feedback=None, prior_implementations=None):
            return InnerLoopResult(
                task_id=task["id"], passed=True, attempts_used=1,
                records=[AttemptRecord(1, True, True, True, "")]
            )
    monkeypatch.setattr("tools.auto.inner_loop.make_inner_loop", lambda config, base_dir: FakeInnerLoop(config, base_dir))


# ─────────────────────────────────────────────────────────────────────────────
# RunLimits.from_config
# ─────────────────────────────────────────────────────────────────────────────

class TestRunLimitsConfig:
    def test_defaults_no_caps(self):
        lim = RunLimits.from_config(configparser.ConfigParser())
        assert lim.max_runtime_sec == 0
        assert lim.max_tasks_per_run == 0
        assert not lim.runtime_capped
        assert not lim.task_capped

    def test_minutes_converted_to_seconds(self):
        lim = RunLimits.from_config(_cfg(max_runtime_min=2))
        assert lim.max_runtime_sec == 120
        assert lim.runtime_capped

    def test_task_cap_read(self):
        lim = RunLimits.from_config(_cfg(max_tasks_per_run=5))
        assert lim.max_tasks_per_run == 5
        assert lim.task_capped

    def test_exec_timeout_default_120(self):
        lim = RunLimits.from_config(configparser.ConfigParser())
        assert lim.exec_timeout_sec == 120
        assert lim.exec_timeout_active

    def test_exec_timeout_read_and_disable(self):
        assert RunLimits.from_config(_cfg(exec_timeout_sec=30)).exec_timeout_sec == 30
        assert RunLimits.from_config(_cfg(exec_timeout_sec=0)).exec_timeout_active is False

    def test_negative_values_clamped(self):
        lim = RunLimits(max_runtime_sec=-5, max_tasks_per_run=-3, exec_timeout_sec=-1)
        assert lim.max_runtime_sec == 0
        assert lim.max_tasks_per_run == 0
        assert lim.exec_timeout_sec == 0


# ─────────────────────────────────────────────────────────────────────────────
# cap predicates (with fake clock)
# ─────────────────────────────────────────────────────────────────────────────

class TestCapPredicates:
    def test_runtime_not_exceeded_before_cap(self, tmp_path):
        clk = _FakeClock()
        c = AutoController("g", tmp_path, config_path="none.ini", _time_fn=clk)
        c.limits = RunLimits(max_runtime_sec=60)
        c._start_time = clk()
        clk.advance(59)
        assert c.is_runtime_exceeded() is False

    def test_runtime_exceeded_after_cap(self, tmp_path):
        clk = _FakeClock()
        c = AutoController("g", tmp_path, config_path="none.ini", _time_fn=clk)
        c.limits = RunLimits(max_runtime_sec=60)
        c._start_time = clk()
        clk.advance(61)
        assert c.is_runtime_exceeded() is True

    def test_runtime_never_exceeded_when_disabled(self, tmp_path):
        clk = _FakeClock()
        c = AutoController("g", tmp_path, config_path="none.ini", _time_fn=clk)
        c.limits = RunLimits(max_runtime_sec=0)
        c._start_time = clk()
        clk.advance(10_000)
        assert c.is_runtime_exceeded() is False

    def test_task_cap_reached(self, tmp_path):
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=3)
        assert c.is_task_cap_reached(2) is False
        assert c.is_task_cap_reached(3) is True

    def test_task_cap_disabled(self, tmp_path):
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=0)
        assert c.is_task_cap_reached(9999) is False

    def test_check_caps_runtime_first(self, tmp_path):
        clk = _FakeClock()
        c = AutoController("g", tmp_path, config_path="none.ini", _time_fn=clk)
        c.limits = RunLimits(max_runtime_sec=60, max_tasks_per_run=1)
        c._start_time = clk()
        clk.advance(61)
        assert c.check_caps(tasks_done=5) == STOP_RUNTIME_CAP

    def test_check_caps_task(self, tmp_path):
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=2)
        assert c.check_caps(tasks_done=2) == STOP_TASK_CAP

    def test_check_caps_none(self, tmp_path):
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits()
        assert c.check_caps(tasks_done=100) is None


# ─────────────────────────────────────────────────────────────────────────────
# graceful stop + resume through run()
# ─────────────────────────────────────────────────────────────────────────────

class TestGracefulStop:
    def test_task_cap_stops_run_gracefully(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=5)
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=2)
        code = c.run()
        assert code == 0                                   # graceful exit
        prog = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert prog["status"] == "capped"
        assert prog["stop_reason"] == STOP_TASK_CAP

    def test_task_cap_only_runs_capped_count(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=5)
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=2)
        c.run()
        done = {t["id"] for t in c.state.all_tasks() if t["status"] == STATUS_DONE}
        assert len(done) == 2

    def test_runtime_cap_stops_run(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=5)
        # auto-advancing clock: each call jumps +100s, so the first cap check
        # inside the loop is already past a 10s cap (the skeleton loop itself
        # consumes no real time).
        class _AutoClock:
            def __init__(self): self.t = 1000.0
            def __call__(self):
                self.t += 100.0
                return self.t
        c = AutoController("g", tmp_path, config_path="none.ini", _time_fn=_AutoClock())
        c.limits = RunLimits(max_runtime_sec=10)
        code = c.run()
        assert code == 0
        prog = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert prog["status"] == "capped"
        assert prog["stop_reason"] == STOP_RUNTIME_CAP

    def test_cap_logged_to_run_log(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=3)
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits(max_tasks_per_run=1)
        c.run()
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "capped" in log and "task_cap" in log

    def test_resume_after_cap_completes_rest(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=4)
        # first session: cap at 2
        c1 = AutoController("g", tmp_path, config_path="none.ini")
        c1.limits = RunLimits(max_tasks_per_run=2)
        c1.run()
        assert len([t for t in c1.state.all_tasks() if t["status"] == STATUS_DONE]) == 2
        # second session: no cap → finishes the remaining 2
        c2 = AutoController("g", tmp_path, config_path="none.ini")
        c2.limits = RunLimits()
        code = c2.run()
        assert code == 0
        done = [t for t in c2.state.all_tasks() if t["status"] == STATUS_DONE]
        assert len(done) == 4
        prog = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert prog["status"] == "idle"

    def test_no_caps_completes_normally(self, tmp_path):
        _seed_tasks(tmp_path, "g", n=3)
        c = AutoController("g", tmp_path, config_path="none.ini")
        c.limits = RunLimits()
        code = c.run()
        assert code == 0
        prog = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert prog["status"] == "idle"
        assert len([t for t in c.state.all_tasks() if t["status"] == STATUS_DONE]) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))