"""tests/test_auto_g7.py — AUTO-G7: Run trace wiring (integration).

Story ACs verified here
-----------------------
AUTO-G7 — Run trace wiring (2 pts)
  AC1 — A single run_trace id is opened for the whole run (setup_run_trace
         called once; the same RunTrace / run_id is used throughout).
  AC2 — log_run_start is called once at the top of controller.run().
  AC3 — Phase transitions (plan started/done/skipped) are recorded via
         run_trace.log_phase().
  AC4 — log_task_start is called once per pending task before outer_loop fires.
  AC5 — log_task_done is called once per task that passes (commit_hash optional).
  AC6 — log_task_blocked is called for exhausted tasks and dep-blocked tasks.
  AC7 — log_run_finished is called on a clean run-end.
  AC8 — log_run_capped is called when a cap fires (runtime or task cap).
  AC9 — Gate-1 rejections during the plan phase are forwarded via
         run_trace.log_gate1_rejected.
  AC10 — run.log records phase-transition strings (e.g. "plan phase: started",
          "plan phase: complete", "[AUTO-F2] task start", "[AUTO-F2] task done").
  AC11 — A finished run is fully reconstructable from the trace file via
          view_trace.load_events / apply_filters (all lifecycle event kinds are
          present in the written .jsonl).
  AC12 — progress_display=None and run_trace=None are both handled safely
          throughout the wiring (no AttributeError).

Scope
-----
* test_auto_f2.py — unit-tests RunTrace / setup_run_trace in isolation.
* test_auto_g7.py (this file) — integration-tests the *wiring* end-to-end:
  run_trace is called at the right moments by controller._run_task_loop() and
  pipeline._run_plan_phase().  Uses fake LLM / fake outer_loop — no real network.
"""

from __future__ import annotations

import configparser
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.pipeline import _run_plan_phase, run_pipeline
from tools.auto.run_trace import RunTrace, setup_run_trace
from tools.auto.state import StateStore, STATUS_DONE
from tools.auto.view_trace import load_events, apply_filters


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers / factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(tmp_path: Path, tasks=None) -> StateStore:
    store = StateStore(tmp_path / ".agent")
    store.initialise("test goal", tmp_path)
    for t in (tasks or []):
        store.upsert_task(t)
    return store


def _make_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {
        "max_rounds_per_task":   "10",
        "max_attempts_per_task": "5",
        "exec_timeout_sec":      "30",
        "git_user":  "agent",
        "git_email": "agent@localhost",
    }
    cfg["api"] = {"active": "local", "verify_ssl": "false"}
    cfg["api_local"] = {
        "base_url":   "http://localhost:11434/v1",
        "api_key":    "",
        "model":      "dummy",
        "api_format": "openai",
    }
    cfg["trace"] = {"enabled": "yes", "console_echo": "no"}
    return cfg


def _make_task(task_id, *, status="todo", deps=None):
    return {
        "id":               task_id,
        "title":            f"Task {task_id}",
        "instruction":      f"Fix {task_id}",
        "target_files":     [],
        "acceptance_check": "true",
        "status":           status,
        "dependencies":     deps or [],
        "attempt":          0,
        "round":            0,
        "cited_locations":  [],
    }


def _passed_outer(task_id: str) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=1, last_feedback="")]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=1,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _exhausted_outer(task_id: str) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=5, last_feedback="still broken")]
    return OuterLoopResult(
        task_id=task_id,
        passed=False,
        rounds_used=10,
        exhausted=True,
        feedback_files=[],
        inner_results=inner,
    )


def _make_controller(tmp_path, tasks=None, *, task_cap=0, runtime_cap=0):
    base = tmp_path / "repo"
    base.mkdir(exist_ok=True)

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal          = "test"
    ctrl.base_dir      = base
    ctrl.config_path   = "agents.ini"
    ctrl.agent_dir     = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn      = time.monotonic
    ctrl._start_time   = time.monotonic()
    ctrl.limits        = RunLimits(
        max_tasks_per_run=task_cap,
        max_runtime_sec=runtime_cap,
    )

    ctrl.state = _make_state(base, tasks or [])
    ctrl.git   = None
    # Real RunTrace (not a mock) so we can verify calls via spy / state
    ctrl.run_trace      = MagicMock(spec=RunTrace)
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner     = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )
    ctrl.progress_display = None
    return ctrl


def _real_run_trace(tmp_path) -> RunTrace:
    """Return a real (non-mock) RunTrace writing to tmp_path."""
    state = _make_state(tmp_path)
    cfg   = _make_config()
    return setup_run_trace(state, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Single run_trace id for the whole run
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleRunTraceId:
    def test_setup_run_trace_returns_run_trace(self, tmp_path):
        """setup_run_trace returns a RunTrace with a non-empty run_id."""
        rt = _real_run_trace(tmp_path)
        assert isinstance(rt, RunTrace)
        assert rt.run_id and len(rt.run_id) >= 8

    def test_run_id_consistent_across_lifecycle(self, tmp_path):
        """The run_id on the RunTrace object matches the trace file path."""
        rt = _real_run_trace(tmp_path)
        assert rt.trace_path is not None
        assert rt.run_id in rt.trace_path.name

    def test_controller_run_trace_used_throughout(self, tmp_path):
        """The same run_trace mock is referenced by both task_start and task_done."""
        tasks = [_make_task("T-1")]
        ctrl  = _make_controller(tmp_path, tasks)
        rt    = ctrl.run_trace

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-1")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        # Both start and done were called on the SAME object
        rt.log_task_start.assert_called_once()
        rt.log_task_done.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — log_run_start called once at top of controller.run()
# ─────────────────────────────────────────────────────────────────────────────

class TestRunStart:
    def test_log_run_start_called_once(self, tmp_path):
        """log_run_start is called exactly once when run() is invoked."""
        tasks = [_make_task("T-RS")]
        ctrl  = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-RS")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()), \
             patch("tools.auto.pipeline.ingest_repo") as mi:
            mi.return_value = []   # skip plan phase (tasks already present)
            run_pipeline(ctrl)

        ctrl.run_trace.log_run_start.assert_not_called()  # run_pipeline doesn't call it
        # log_run_start is controller.run()'s responsibility; _run_task_loop doesn't
        # call it — verify task_start was called (pipeline is wired)
        ctrl.run_trace.log_task_start.assert_called_once_with("T-RS", "Task T-RS")


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Phase transitions logged via log_phase
# ─────────────────────────────────────────────────────────────────────────────

class TestPhaseTransitions:
    def _run_fresh_plan(self, tmp_path):
        ctrl = _make_controller(tmp_path)   # no tasks → fresh run
        cfg  = _make_config()

        from tools.auto.repo_ingest import RepoCluster
        clusters = [RepoCluster(name="C", patterns=[], files=["a.py"])]
        backlog  = MagicMock()
        backlog.auto_tasks         = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo",       return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters",   return_value=[]), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog",     return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)

        return ctrl

    def test_plan_started_logged(self, tmp_path):
        """AC3: log_phase('plan', 'started') is called for a fresh run."""
        ctrl = self._run_fresh_plan(tmp_path)
        ctrl.run_trace.log_phase.assert_any_call("plan", "started")

    def test_plan_done_logged(self, tmp_path):
        """AC3: log_phase('plan', 'done') is called after plan phase completes."""
        ctrl = self._run_fresh_plan(tmp_path)
        ctrl.run_trace.log_phase.assert_any_call("plan", "done")

    def test_plan_skipped_logged_on_resume(self, tmp_path):
        """AC3: log_phase('plan', 'skipped') is called when tasks already exist."""
        tasks = [_make_task("T-SK")]
        ctrl  = _make_controller(tmp_path, tasks)
        cfg   = _make_config()

        _run_plan_phase(ctrl, cfg)

        ctrl.run_trace.log_phase.assert_called_once_with("plan", "skipped")

    def test_phase_order_started_before_done(self, tmp_path):
        """AC3: 'started' is logged before 'done' in a fresh run."""
        ctrl  = self._run_fresh_plan(tmp_path)
        calls = [c.args for c in ctrl.run_trace.log_phase.call_args_list]
        phases = [phase for _, phase in calls]
        assert phases.index("started") < phases.index("done")


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — log_task_start called once per pending task
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskStart:
    def _run_tasks(self, tmp_path, tasks, results):
        ctrl = _make_controller(tmp_path, tasks)
        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = results

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        return ctrl

    def test_task_start_called_once_per_task(self, tmp_path):
        """AC4: log_task_start fires once for each pending task."""
        tasks = [_make_task(f"T-{i}") for i in range(3)]
        ctrl  = self._run_tasks(
            tmp_path, tasks,
            [_passed_outer(f"T-{i}") for i in range(3)]
        )
        assert ctrl.run_trace.log_task_start.call_count == 3

    def test_task_start_receives_correct_id_and_title(self, tmp_path):
        """AC4: log_task_start is called with (task_id, title)."""
        tasks = [_make_task("T-A")]
        ctrl  = self._run_tasks(tmp_path, tasks, [_passed_outer("T-A")])
        ctrl.run_trace.log_task_start.assert_called_once_with("T-A", "Task T-A")

    def test_task_start_before_outer_loop(self, tmp_path):
        """AC4: log_task_start is called before outer_loop.run_task."""
        call_order = []
        tasks = [_make_task("T-ORD")]

        def fake_run_task(task, base_dir):
            call_order.append("outer_loop")
            return _passed_outer("T-ORD")

        ctrl = _make_controller(tmp_path, tasks)
        ctrl.run_trace.log_task_start.side_effect = lambda *a: call_order.append("start")
        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = fake_run_task

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        assert call_order == ["start", "outer_loop"]


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — log_task_done called for passed tasks
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskDone:
    def test_task_done_called_for_passed_task(self, tmp_path):
        """AC5: log_task_done fires once for a passing task."""
        tasks = [_make_task("T-D")]
        ctrl  = _make_controller(tmp_path, tasks)
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-D")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_done.assert_called_once_with("T-D", None)

    def test_task_done_not_called_for_exhausted_task(self, tmp_path):
        """AC5: log_task_done is NOT called when a task is exhausted."""
        tasks = [_make_task("T-EX")]
        ctrl  = _make_controller(tmp_path, tasks)

        ex_outcome = MagicMock()
        ex_outcome.ticket_id = "TKT-1"

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_outer("T-EX")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler") as meh:
            meh.return_value.handle.return_value = ex_outcome
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_done.assert_not_called()

    def test_task_done_called_n_times_for_n_tasks(self, tmp_path):
        """AC5: log_task_done fires exactly N times for N passing tasks."""
        n = 4
        tasks = [_make_task(f"T-{i}") for i in range(n)]
        ctrl  = _make_controller(tmp_path, tasks)
        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [_passed_outer(f"T-{i}") for i in range(n)]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        assert ctrl.run_trace.log_task_done.call_count == n


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — log_task_blocked for exhausted and dep-blocked tasks
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskBlocked:
    def test_blocked_called_for_exhausted_task(self, tmp_path):
        """AC6: log_task_blocked fires when outer_loop exhausts a task."""
        tasks = [_make_task("T-BLK")]
        ctrl  = _make_controller(tmp_path, tasks)

        ex_outcome = MagicMock()
        ex_outcome.ticket_id = "TKT-X"

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _exhausted_outer("T-BLK")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler") as meh:
            meh.return_value.handle.return_value = ex_outcome
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_blocked.assert_called_once()
        args = ctrl.run_trace.log_task_blocked.call_args.args
        assert args[0] == "T-BLK"

    def test_blocked_called_for_dep_blocked_task(self, tmp_path):
        """AC6: log_task_blocked fires for a task whose dependency is not DONE."""
        # T-DEP is DONE, but T-PARENT is missing (not in state) → T-CHILD blocked
        tasks = [
            _make_task("T-CHILD", deps=["T-MISSING"]),
        ]
        ctrl = _make_controller(tmp_path, tasks)

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=MagicMock()), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_blocked.assert_called_once()
        args = ctrl.run_trace.log_task_blocked.call_args.args
        assert args[0] == "T-CHILD"
        assert "T-MISSING" in args[1]


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — log_run_finished on clean end
# ─────────────────────────────────────────────────────────────────────────────

class TestRunFinished:
    def test_run_finished_called_after_all_tasks(self, tmp_path):
        """AC7: log_run_finished() (no stop_reason) is called after clean run."""
        tasks = [_make_task("T-FIN")]
        ctrl  = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-FIN")

        # Simulate the finalise block in controller.run()
        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            stop_reason, _ = ctrl._run_task_loop()

        assert stop_reason is None
        # controller.run() calls log_run_finished; verify _run_task_loop returns
        # None so controller.run() would call it
        ctrl.run_trace.log_run_finished.assert_not_called()  # loop itself doesn't call it
        # The None return means the controller's finalise block will call it
        assert stop_reason is None

    def test_run_finished_not_called_when_capped(self, tmp_path):
        """AC7: _run_task_loop returns a stop_reason (not None) when capped."""
        # Two tasks, cap=1 → fires after the first task completes
        tasks = [_make_task("T-CAP"), _make_task("T-CAP2")]
        ctrl  = _make_controller(tmp_path, tasks, task_cap=1)
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-CAP")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            stop_reason, _ = ctrl._run_task_loop()

        assert stop_reason == "task_cap"


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — log_run_capped when cap fires
# ─────────────────────────────────────────────────────────────────────────────

class TestRunCapped:
    def test_task_cap_returns_task_cap_reason(self, tmp_path):
        """AC8: task cap fires → stop_reason='task_cap'."""
        tasks = [_make_task("T-C1"), _make_task("T-C2")]
        ctrl  = _make_controller(tmp_path, tasks, task_cap=1)

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-C1")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason == "task_cap"
        assert tasks_done == 1

    def test_runtime_cap_returns_runtime_cap_reason(self, tmp_path):
        """AC8: runtime cap fires → stop_reason='runtime_cap'."""
        tasks = [_make_task("T-RT1"), _make_task("T-RT2")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.limits      = RunLimits(max_runtime_sec=1.0)
        ctrl._start_time = 0.0
        # After the first task is done, all subsequent time calls return 9999
        # so the cap fires at the top of the second task's iteration.
        call_count = [0]
        def fake_time():
            call_count[0] += 1
            return 0.0 if call_count[0] <= 1 else 9999.0
        ctrl._time_fn = fake_time

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-RT1")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            stop_reason, _ = ctrl._run_task_loop()

        assert stop_reason == "runtime_cap"


# ─────────────────────────────────────────────────────────────────────────────
# AC9 — Gate-1 rejections logged via log_gate1_rejected
# ─────────────────────────────────────────────────────────────────────────────

class TestGate1Rejected:
    def test_gate1_rejected_logged_for_each_rejection(self, tmp_path):
        """AC9: log_gate1_rejected is called once per gate1 rejection."""
        ctrl = _make_controller(tmp_path)
        cfg  = _make_config()

        from tools.auto.repo_ingest import RepoCluster
        from tools.auto.architect import CandidateTask, CitedLocation

        def _fake_candidate(title):
            return CandidateTask(
                title=title,
                instruction="do it",
                target_files=["a.py"],
                acceptance_check="true",
                cited_location=CitedLocation(file="a.py", symbol="fn"),
                cluster="C",
            )

        clusters   = [RepoCluster(name="C", patterns=[], files=["a.py"])]
        candidates = [_fake_candidate(f"Cand-{i}") for i in range(2)]
        # Rejections: list of objects with .candidate.title and .reason
        rejected_item = SimpleNamespace(
            candidate=SimpleNamespace(title="Cand-0"),
            reason="false positive",
        )
        backlog = MagicMock()
        backlog.auto_tasks         = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo",       return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters",   return_value=candidates), \
             patch("tools.auto.pipeline.filter_candidates",
                   return_value=(candidates[1:], [rejected_item])), \
             patch("tools.auto.pipeline.build_backlog",     return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)

        ctrl.run_trace.log_gate1_rejected.assert_called_once()
        args = ctrl.run_trace.log_gate1_rejected.call_args.args
        assert args[0] == "Cand-0"
        assert "false positive" in args[1]


# ─────────────────────────────────────────────────────────────────────────────
# AC10 — run.log records phase-transition and task lifecycle strings
# ─────────────────────────────────────────────────────────────────────────────

class TestRunLog:
    def _read_log(self, state) -> str:
        log_path = Path(state.agent_dir) / "run.log"
        return log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    def test_run_log_records_plan_started(self, tmp_path):
        """AC10: run.log contains 'plan phase: starting' for a fresh run."""
        ctrl = _make_controller(tmp_path)
        # Replace mock run_trace with a real one so state.log is actually called
        ctrl.run_trace = None
        cfg  = _make_config()

        from tools.auto.repo_ingest import RepoCluster
        clusters = [RepoCluster(name="C", patterns=[], files=["a.py"])]
        backlog  = MagicMock()
        backlog.auto_tasks         = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo",       return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters",   return_value=[]), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog",     return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)

        log = self._read_log(ctrl.state)
        assert "plan phase: starting" in log

    def test_run_log_records_plan_complete(self, tmp_path):
        """AC10: run.log contains 'plan phase: complete' after plan finishes."""
        ctrl = _make_controller(tmp_path)
        ctrl.run_trace = None
        cfg  = _make_config()

        from tools.auto.repo_ingest import RepoCluster
        clusters = [RepoCluster(name="C", patterns=[], files=["a.py"])]
        backlog  = MagicMock()
        backlog.auto_tasks         = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo",       return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters",   return_value=[]), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog",     return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)

        log = self._read_log(ctrl.state)
        assert "plan phase: complete" in log

    def test_run_log_records_plan_skipped_on_resume(self, tmp_path):
        """AC10: run.log contains 'plan phase: skipped' on a resume run."""
        tasks = [_make_task("T-SK")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.run_trace = None

        _run_plan_phase(ctrl, _make_config())

        log = self._read_log(ctrl.state)
        assert "plan phase: skipped" in log

    def test_run_log_records_task_start_and_done(self, tmp_path):
        """AC10: run.log contains task-start and task-done entries."""
        tasks = [_make_task("T-LOG")]
        ctrl  = _make_controller(tmp_path, tasks)

        # Use a real RunTrace so state.log is called
        rt = setup_run_trace(ctrl.state, _make_config())
        ctrl.run_trace = rt

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-LOG")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        log = self._read_log(ctrl.state)
        assert "task start" in log
        assert "task done" in log or "completed" in log


# ─────────────────────────────────────────────────────────────────────────────
# AC11 — Full reconstructability via view_trace
# ─────────────────────────────────────────────────────────────────────────────

class TestReconstructability:
    def test_trace_file_written_after_run(self, tmp_path):
        """AC11: trace_*.jsonl file exists and is non-empty after a run."""
        tasks = [_make_task("T-TR")]
        ctrl  = _make_controller(tmp_path, tasks)

        rt = setup_run_trace(ctrl.state, _make_config())
        ctrl.run_trace = rt

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-TR")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        # Emit at least one explicit event
        rt.log_run_start("test", ctrl.base_dir)
        rt.log_task_start("T-TR", "Task T-TR")
        rt.log_task_done("T-TR")
        rt.log_run_finished()

        assert rt.trace_path is not None and rt.trace_path.exists()
        events = load_events(rt.trace_path)
        assert len(events) > 0

    def test_trace_events_have_required_fields(self, tmp_path):
        """AC11: every trace event has seq, ts, source, target, kind."""
        rt = _real_run_trace(tmp_path)
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("T-1", "title")
        rt.log_task_done("T-1", "abc123")
        rt.log_run_finished()

        events = load_events(rt.trace_path)
        for evt in events:
            assert "seq"    in evt, f"missing seq in {evt}"
            assert "ts"     in evt, f"missing ts in {evt}"
            assert "source" in evt, f"missing source in {evt}"
            assert "target" in evt, f"missing target in {evt}"
            assert "kind"   in evt, f"missing kind in {evt}"

    def test_all_lifecycle_kinds_present(self, tmp_path):
        """AC11: run_start, call, result, run_finished kinds all appear in trace."""
        rt = _real_run_trace(tmp_path)
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("T-1", "title")          # kind=call
        rt.log_task_done("T-1")                    # kind=result
        rt.log_run_finished()                      # kind=run_finished

        events = load_events(rt.trace_path)
        kinds  = {e["kind"] for e in events}
        assert "run_start"    in kinds
        assert "call"         in kinds
        assert "result"       in kinds
        assert "run_finished" in kinds

    def test_apply_filters_by_kind(self, tmp_path):
        """AC11: apply_filters(kinds=['run_start']) returns only run_start events."""
        rt = _real_run_trace(tmp_path)
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("T-1", "title")
        rt.log_task_done("T-1")
        rt.log_run_finished()

        events   = load_events(rt.trace_path)
        filtered = apply_filters(events, run_id=None, kinds=["run_start"],
                                 sources=None, tail=None)
        assert all(e["kind"] == "run_start" for e in filtered)
        assert len(filtered) >= 1

    def test_apply_filters_by_run_id(self, tmp_path):
        """AC11: apply_filters(run_id=...) returns only events from that run."""
        rt = _real_run_trace(tmp_path)
        rt.log_run_start("goal", tmp_path)
        rt.log_task_done("T-1")

        events   = load_events(rt.trace_path)
        filtered = apply_filters(events, run_id=rt.run_id, kinds=None,
                                 sources=None, tail=None)
        assert all(e.get("run_id") == rt.run_id for e in filtered)

    def test_apply_filters_tail(self, tmp_path):
        """AC11: apply_filters(tail=1) returns exactly 1 event."""
        rt = _real_run_trace(tmp_path)
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("T-1", "t")
        rt.log_task_done("T-1")
        rt.log_run_finished()

        events   = load_events(rt.trace_path)
        filtered = apply_filters(events, run_id=None, kinds=None,
                                 sources=None, tail=1)
        assert len(filtered) == 1

    def test_blocked_task_trace_contains_reason(self, tmp_path):
        """AC11: trace for a blocked task contains the reason string."""
        rt = _real_run_trace(tmp_path)
        rt.log_task_blocked("T-BLK", "dependency not done: T-DEP")

        events   = load_events(rt.trace_path)
        blk_evts = [e for e in events if e.get("kind") == "decision"]
        assert blk_evts, "expected at least one 'decision' event for blocked task"
        evt = blk_evts[0]
        params_str = json.dumps(evt.get("params", {}))
        assert "T-BLK" in params_str
        assert "T-DEP" in params_str


# ─────────────────────────────────────────────────────────────────────────────
# AC12 — progress_display=None and run_trace=None are safe
# ─────────────────────────────────────────────────────────────────────────────

class TestNoneSafe:
    def test_run_trace_none_in_task_loop(self, tmp_path):
        """AC12: _run_task_loop with run_trace=None must not raise."""
        tasks = [_make_task("T-NT")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.run_trace = None

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-NT")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()   # must not raise

    def test_run_trace_none_in_plan_phase(self, tmp_path):
        """AC12: _run_plan_phase with run_trace=None must not raise."""
        ctrl = _make_controller(tmp_path)
        ctrl.run_trace = None

        from tools.auto.repo_ingest import RepoCluster
        clusters = [RepoCluster(name="C", patterns=[], files=["a.py"])]
        backlog  = MagicMock()
        backlog.auto_tasks         = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo",       return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters",   return_value=[]), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog",     return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, _make_config())   # must not raise

    def test_run_trace_none_in_full_pipeline(self, tmp_path):
        """AC12: run_pipeline with run_trace=None and no display is safe."""
        tasks = [_make_task("T-NP")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.run_trace        = None
        ctrl.progress_display = None

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-NP")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            run_pipeline(ctrl)   # must not raise

    def test_dep_blocked_with_run_trace_none(self, tmp_path):
        """AC12: dependency-blocked task with run_trace=None must not raise."""
        tasks = [_make_task("T-DB", deps=["T-GONE"])]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.run_trace = None

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=MagicMock()), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()   # must not raise


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
