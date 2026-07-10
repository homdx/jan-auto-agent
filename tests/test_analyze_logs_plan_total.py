"""tests/test_analyze_logs_plan_total.py — plan_total banner fix.

Covers:
  1. plan_ready event populates run["plan_total"] in analyze()
  2. plan_ready with zero total_tasks is ignored (plan_total stays 0)
  3. render_run_summary shows plan= not total= and uses plan_total
  4. render_run_summary falls back to len(real) when plan_total == 0
  5. pipeline.py emits plan_ready after _run_plan_phase when run_trace present
  6. pipeline.py does NOT emit plan_ready when run_trace is None (no crash)
  7. Multiple runs in one trace — each gets its own plan_total
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyze_logs import analyze, render_run_summary


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _evt(kind: str, params: dict | None = None, run_id: str = "run1", **kw) -> dict:
    return {
        "run_id": run_id,
        "kind": kind,
        "source": kw.get("source", "run_trace"),
        "target": kw.get("target", "auto_run"),
        "ts": "2024-01-01T00:00:00",
        "params": params or {},
        "content": kw.get("content", ""),
    }


def _run_start(goal: str = "improve code", run_id: str = "run1") -> dict:
    return _evt("run_start", {"goal": goal, "run_id": run_id}, run_id=run_id)


def _plan_ready(total: int, run_id: str = "run1") -> dict:
    return _evt("plan_ready", {"total_tasks": total, "run_id": run_id}, run_id=run_id)


def _capture_summary(run: dict) -> str:
    """Render run summary to a string (strip ANSI)."""
    import re
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        render_run_summary(run)
    finally:
        sys.stdout = old_stdout
    text = buf.getvalue()
    # Strip ANSI escape codes
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ─────────────────────────────────────────────────────────────────────────────
# 1. plan_ready populates plan_total
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanReadyEvent:
    def test_plan_ready_sets_plan_total(self) -> None:
        events = [_run_start(), _plan_ready(7)]
        runs = analyze(events)
        assert runs["run1"]["plan_total"] == 7

    def test_plan_ready_zero_ignored(self) -> None:
        events = [_run_start(), _plan_ready(0)]
        runs = analyze(events)
        assert runs["run1"]["plan_total"] == 0

    def test_no_plan_ready_leaves_zero(self) -> None:
        events = [_run_start()]
        runs = analyze(events)
        assert runs["run1"]["plan_total"] == 0

    def test_plan_ready_overrides_previous(self) -> None:
        # Second plan_ready (resume scenario) updates the value
        events = [_run_start(), _plan_ready(3), _plan_ready(5)]
        runs = analyze(events)
        assert runs["run1"]["plan_total"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# 2. render_run_summary displays plan= and uses plan_total
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderRunSummary:
    def _make_run(self, plan_total: int = 0, n_tasks: int = 0) -> dict:
        """Build a minimal run dict for render_run_summary."""
        tasks = {}
        for i in range(n_tasks):
            tid = f"T{i}"
            tasks[tid] = {
                "task_id": tid,
                "title": f"Task {i}",
                "status": "DONE",
                "iterations": 1,
                "approved": 1,
                "rejected": 0,
                "commit": None,
                "start_ts": "2024-01-01T00:00:00",
                "end_ts": "2024-01-01T00:01:00",
            }
        return {
            "run_id": "run1",
            "start_ts": "2024-01-01T00:00:00",
            "end_ts": "2024-01-01T01:00:00",
            "stop_reason": None,
            "goal": "test",
            "tasks": tasks,
            "prompt_changes": [],
            "llm_calls": 0,
            "context_requests": [],
            "total_events": 0,
            "plan_total": plan_total,
            "files_preparing": [],
            "_current_task": None,
        }

    def test_summary_shows_plan_label(self) -> None:
        run = self._make_run(plan_total=10, n_tasks=3)
        text = _capture_summary(run)
        assert "plan=10" in text, f"Expected 'plan=10' in: {text}"

    def test_summary_shows_seen_label(self) -> None:
        run = self._make_run(plan_total=10, n_tasks=3)
        text = _capture_summary(run)
        assert "seen=3" in text, f"Expected 'seen=3' in: {text}"

    def test_summary_no_longer_shows_total_label_in_tasks_line(self) -> None:
        run = self._make_run(plan_total=10, n_tasks=3)
        text = _capture_summary(run)
        # Find the Tasks line and confirm it uses plan=/seen= not total=
        tasks_line = next(
            (l for l in text.splitlines() if "Tasks:" in l or "plan=" in l), ""
        )
        assert "plan=" in tasks_line, f"Tasks line must contain 'plan=': {tasks_line}"
        assert "total=" not in tasks_line, (
            f"Tasks line must not contain 'total=': {tasks_line}"
        )

    def test_fallback_to_seen_when_plan_total_zero(self) -> None:
        run = self._make_run(plan_total=0, n_tasks=4)
        text = _capture_summary(run)
        # When plan_total=0, fallback: plan= shows len(real)=4
        assert "plan=4" in text, f"Expected fallback 'plan=4': {text}"

    def test_plan_total_larger_than_seen(self) -> None:
        # 10 planned, only 3 seen in trace (rest haven't started yet)
        run = self._make_run(plan_total=10, n_tasks=3)
        text = _capture_summary(run)
        assert "plan=10" in text
        assert "seen=3" in text


# ─────────────────────────────────────────────────────────────────────────────
# 3. Multiple runs — each gets its own plan_total
# ─────────────────────────────────────────────────────────────────────────────

class TestMultipleRuns:
    def test_each_run_has_independent_plan_total(self) -> None:
        events = [
            _run_start(run_id="runA"),
            _plan_ready(5, run_id="runA"),
            _run_start(run_id="runB"),
            _plan_ready(12, run_id="runB"),
        ]
        runs = analyze(events)
        assert runs["runA"]["plan_total"] == 5
        assert runs["runB"]["plan_total"] == 12


# ─────────────────────────────────────────────────────────────────────────────
# 4. pipeline.py emits plan_ready after _run_plan_phase
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineEmitsPlanReady:
    def _make_controller(self, tmp_path: Path, n_tasks: int = 3) -> MagicMock:
        from tools.auto.state import StateStore, make_task

        agent_dir = tmp_path / ".agent"
        state = StateStore(agent_dir)
        state.initialise("goal", tmp_path)
        for i in range(n_tasks):
            state.upsert_task(make_task(
                id=f"AUTO-T{i}", title=f"t{i}", instruction="x",
                target_files=["f.py"],
            ))

        ctrl = MagicMock()
        ctrl.state = state
        ctrl.goal = "goal"
        ctrl.base_dir = tmp_path
        ctrl.config_path = str(tmp_path / "agents.ini")
        ctrl.dry_run = False
        ctrl.task_mode = "code"
        ctrl.progress_display = None
        ctrl.run_trace = MagicMock()
        ctrl.run_trace.run_id = "testrun"
        ctrl._run_task_loop.return_value = (None, 0)
        return ctrl

    def test_plan_ready_emitted_with_correct_count(self, tmp_path: Path) -> None:
        from tools.auto.pipeline import run_pipeline

        ctrl = self._make_controller(tmp_path, n_tasks=4)

        emitted: list[dict] = []

        def fake_tracer_event(**kw):
            emitted.append(kw)

        with (
            patch("tools.auto.pipeline._run_plan_phase"),
            patch("tools.agent_trace.tracer.event", side_effect=fake_tracer_event),
        ):
            run_pipeline(ctrl)

        plan_ready_events = [e for e in emitted if e.get("kind") == "plan_ready"]
        assert len(plan_ready_events) == 1, (
            f"Expected exactly 1 plan_ready event, got {len(plan_ready_events)}"
        )
        assert plan_ready_events[0]["params"]["total_tasks"] == 4

    def test_no_crash_when_run_trace_is_none(self, tmp_path: Path) -> None:
        from tools.auto.pipeline import run_pipeline

        ctrl = self._make_controller(tmp_path, n_tasks=2)
        ctrl.run_trace = None  # no tracer

        with patch("tools.auto.pipeline._run_plan_phase"):
            stop, done = run_pipeline(ctrl)  # must not raise

        assert stop is None

    def test_plan_ready_run_id_matches_tracer(self, tmp_path: Path) -> None:
        from tools.auto.pipeline import run_pipeline

        ctrl = self._make_controller(tmp_path, n_tasks=2)
        ctrl.run_trace.run_id = "abc123"

        emitted: list[dict] = []

        def fake_tracer_event(**kw):
            emitted.append(kw)

        with (
            patch("tools.auto.pipeline._run_plan_phase"),
            patch("tools.agent_trace.tracer.event", side_effect=fake_tracer_event),
        ):
            run_pipeline(ctrl)

        plan_ready = next(
            (e for e in emitted if e.get("kind") == "plan_ready"), None
        )
        assert plan_ready is not None
        assert plan_ready["params"]["run_id"] == "abc123"
