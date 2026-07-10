"""tests/test_auto_g8.py — AUTO-G8: Auto-tuning wiring (integration).

Story ACs verified here
-----------------------
AUTO-G8 — Auto-tuning wiring (3 pts)
  AC1 — metrics_stream.record_gate2() is called after every task (passed or
         exhausted), with the correct task_id, approved flag, feedback, attempts,
         and prompt_store reference.
  AC2 — auto_tuner.maybe_tune() is called after every task, exactly once per
         task, after record_gate2 has already been called.
  AC3 — When maybe_tune() returns a promoted outcome, the event is logged to
         run.log with the score (AUTO-E1).
  AC4 — When maybe_tune() returns a non-promoted outcome, run.log is NOT written
         for the tune event (no noise for every non-triggered poll).
  AC5 — Auto metrics are isolated from interactive metrics.json: the stream
         writes to <agent_dir>/metrics.json, never to the interactive default.
  AC6 — A promoted validator prompt is hot-reloaded mid-run: auto_tuner's
         reload_agents_fn (or prompt_store.push) is invoked.
  AC7 — The tuning block is skipped cleanly when metrics_stream=None or
         auto_tuner=None (no AttributeError anywhere).
  AC8 — Attempts and feedback are computed correctly from inner_results:
         attempts = sum(r.attempts_used), feedback = inner_results[-1].last_feedback.
  AC9 — The auto metrics file (agent_dir/metrics.json) is independent of the
         interactive metrics.json; AutoMetricsStream refuses to write to the
         interactive default path.
  AC10 — After N tasks, metrics_stream.record_gate2 has been called exactly N
          times (one per task, regardless of pass/fail).

Scope
-----
* test_auto_e1.py / test_auto_e2.py — unit-test AutoTuner / AutoMetricsStream.
* test_auto_g8.py (this file) — integration: wiring through
  controller._run_task_loop(), using a real AutoMetricsStream + a mock
  AutoTuner so we can inspect exactly when and how each is called.
"""

from __future__ import annotations

import configparser
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import StateStore
from tools.auto.auto_metrics import AutoMetricsStream
from tools.auto.auto_tuner import AutoTuner, TuneOutcome


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

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
    cfg["prompt_optimizer"] = {
        "enabled":                    "yes",
        "min_runs_before_optimize":   "5",
        "trigger_avg_iterations":     "2.0",
        "trigger_json_fail_rate":     "0.30",
    }
    return cfg


def _make_task(task_id, *, status="todo", attempts=1):
    return {
        "id":               task_id,
        "title":            f"Task {task_id}",
        "instruction":      f"Fix {task_id}",
        "target_files":     [],
        "acceptance_check": "true",
        "status":           status,
        "dependencies":     [],
        "attempt":          attempts,
        "round":            1,
        "cited_locations":  [],
    }


def _passed_outer(task_id: str, *, attempts: int = 1,
                  feedback: str = "") -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=attempts, last_feedback=feedback)]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=1,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _exhausted_outer(task_id: str, *, attempts: int = 5,
                     feedback: str = "still broken") -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=attempts, last_feedback=feedback)]
    return OuterLoopResult(
        task_id=task_id,
        passed=False,
        rounds_used=10,
        exhausted=True,
        feedback_files=[],
        inner_results=inner,
    )


def _not_promoted() -> TuneOutcome:
    return TuneOutcome(agent_name="validator", reason="below threshold")


def _promoted(score: float = 0.85) -> TuneOutcome:
    return TuneOutcome(
        agent_name="validator",
        triggered=True,
        promoted=True,
        reason="avg_iter above threshold",
        new_prompt_score=score,
    )


def _make_controller(tmp_path, tasks=None, *,
                     real_metrics=False, task_cap=0):
    """Build a minimal controller; use real AutoMetricsStream when requested."""
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
    ctrl.limits        = RunLimits(max_tasks_per_run=task_cap)

    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    for t in (tasks or []):
        ctrl.state.upsert_task(t)

    ctrl.git              = None
    ctrl.run_trace        = MagicMock()
    ctrl.progress_display = None

    if real_metrics:
        ctrl.metrics_stream = AutoMetricsStream(ctrl.agent_dir)
    else:
        ctrl.metrics_stream = MagicMock(spec=AutoMetricsStream)

    ctrl.auto_tuner = MagicMock(spec=AutoTuner)
    ctrl.auto_tuner.maybe_tune.return_value = _not_promoted()
    ctrl.auto_tuner.prompt_store = MagicMock()

    return ctrl


def _run_tasks(ctrl, outer_results, *, exhaustion_ticket="TKT-1"):
    """Run _run_task_loop with fake outer_loop and required patches."""
    fake_outer = MagicMock()
    fake_outer.run_task.side_effect = outer_results

    ex_outcome = MagicMock()
    ex_outcome.ticket_id = exhaustion_ticket

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"), \
         patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
         patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler") as meh:
        meh.return_value.handle.return_value = ex_outcome
        return ctrl._run_task_loop()


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — record_gate2 called after every task
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordGate2Called:
    def test_record_gate2_called_for_passed_task(self, tmp_path):
        """AC1: record_gate2 fires once after a passing task."""
        tasks = [_make_task("T-P")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer("T-P")])

        ctrl.metrics_stream.record_gate2.assert_called_once()

    def test_record_gate2_called_for_exhausted_task(self, tmp_path):
        """AC1: record_gate2 fires once after an exhausted task."""
        tasks = [_make_task("T-EX")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_exhausted_outer("T-EX")])

        ctrl.metrics_stream.record_gate2.assert_called_once()

    def test_record_gate2_approved_true_for_passed(self, tmp_path):
        """AC1: approved=True for a passing task."""
        tasks = [_make_task("T-OK")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer("T-OK")])

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["approved"] is True

    def test_record_gate2_approved_false_for_exhausted(self, tmp_path):
        """AC1: approved=False for an exhausted task."""
        tasks = [_make_task("T-FAIL")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_exhausted_outer("T-FAIL")])

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["approved"] is False

    def test_record_gate2_task_id_matches(self, tmp_path):
        """AC1: first positional arg is the task_id."""
        tasks = [_make_task("T-ID")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer("T-ID")])

        args, _ = ctrl.metrics_stream.record_gate2.call_args
        assert args[0] == "T-ID"

    def test_record_gate2_called_n_times_for_n_tasks(self, tmp_path):
        """AC10: record_gate2 is called exactly N times for N tasks."""
        n     = 5
        tasks = [_make_task(f"T-{i}") for i in range(n)]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer(f"T-{i}") for i in range(n)])

        assert ctrl.metrics_stream.record_gate2.call_count == n

    def test_record_gate2_called_for_mixed_pass_exhaust(self, tmp_path):
        """AC10: count is correct even with mixed pass/exhausted tasks."""
        tasks = [_make_task("T-P"), _make_task("T-E")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer("T-P"), _exhausted_outer("T-E")])

        assert ctrl.metrics_stream.record_gate2.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — attempts and feedback extracted correctly from inner_results
# ─────────────────────────────────────────────────────────────────────────────

class TestAttemptsAndFeedback:
    def test_attempts_summed_from_inner_results(self, tmp_path):
        """AC8: attempts = sum of attempts_used across all inner_results."""
        tasks = [_make_task("T-ATT")]
        ctrl  = _make_controller(tmp_path, tasks)

        # Two inner results with 2 + 3 = 5 attempts total
        inner = [
            SimpleNamespace(attempts_used=2, last_feedback=""),
            SimpleNamespace(attempts_used=3, last_feedback="ok"),
        ]
        result = OuterLoopResult(
            task_id="T-ATT", passed=True, rounds_used=2,
            exhausted=False, feedback_files=[], inner_results=inner,
        )
        _run_tasks(ctrl, [result])

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["attempts"] == 5

    def test_feedback_taken_from_last_inner_result(self, tmp_path):
        """AC8: feedback = last_feedback of the last inner result."""
        tasks = [_make_task("T-FB")]
        ctrl  = _make_controller(tmp_path, tasks)

        inner = [
            SimpleNamespace(attempts_used=1, last_feedback="first"),
            SimpleNamespace(attempts_used=1, last_feedback="final feedback"),
        ]
        result = OuterLoopResult(
            task_id="T-FB", passed=True, rounds_used=2,
            exhausted=False, feedback_files=[], inner_results=inner,
        )
        _run_tasks(ctrl, [result])

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["feedback"] == "final feedback"

    def test_empty_inner_results_does_not_crash(self, tmp_path):
        """AC8: an OuterLoopResult with no inner_results is handled safely."""
        tasks = [_make_task("T-EMPTY")]
        ctrl  = _make_controller(tmp_path, tasks)

        result = OuterLoopResult(
            task_id="T-EMPTY", passed=True, rounds_used=1,
            exhausted=False, feedback_files=[], inner_results=[],
        )
        _run_tasks(ctrl, [result])   # must not raise

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["attempts"] == 0
        assert kwargs["feedback"] == ""

    def test_prompt_store_passed_to_record_gate2(self, tmp_path):
        """AC1: prompt_store is forwarded from auto_tuner to record_gate2."""
        tasks = [_make_task("T-PS")]
        ctrl  = _make_controller(tmp_path, tasks)
        ps    = ctrl.auto_tuner.prompt_store

        _run_tasks(ctrl, [_passed_outer("T-PS")])

        _, kwargs = ctrl.metrics_stream.record_gate2.call_args
        assert kwargs["prompt_store"] is ps


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — maybe_tune called once per task, after record_gate2
# ─────────────────────────────────────────────────────────────────────────────

class TestMaybeTuneCalled:
    def test_maybe_tune_called_once_per_task(self, tmp_path):
        """AC2: maybe_tune fires exactly once after each task."""
        tasks = [_make_task(f"T-{i}") for i in range(3)]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_passed_outer(f"T-{i}") for i in range(3)])

        assert ctrl.auto_tuner.maybe_tune.call_count == 3

    def test_maybe_tune_called_after_record_gate2(self, tmp_path):
        """AC2: record_gate2 is called before maybe_tune in each task cycle."""
        call_order = []
        tasks = [_make_task("T-ORD")]
        ctrl  = _make_controller(tmp_path, tasks)

        ctrl.metrics_stream.record_gate2.side_effect = \
            lambda *a, **kw: call_order.append("record")
        ctrl.auto_tuner.maybe_tune.side_effect = \
            lambda: (call_order.append("tune") or _not_promoted())

        _run_tasks(ctrl, [_passed_outer("T-ORD")])

        assert call_order == ["record", "tune"]

    def test_maybe_tune_called_for_exhausted_task(self, tmp_path):
        """AC2: maybe_tune fires even when the task is exhausted."""
        tasks = [_make_task("T-EX")]
        ctrl  = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [_exhausted_outer("T-EX")])

        ctrl.auto_tuner.maybe_tune.assert_called_once()

    def test_maybe_tune_not_called_for_dep_blocked_task(self, tmp_path):
        """AC2: dep-blocked tasks skip the tune block (outer_loop never ran)."""
        tasks = [_make_task("T-BLK", status="todo")]
        tasks[0]["dependencies"] = ["T-MISSING"]
        ctrl = _make_controller(tmp_path, tasks)

        _run_tasks(ctrl, [])   # outer_loop never fires

        ctrl.auto_tuner.maybe_tune.assert_not_called()
        ctrl.metrics_stream.record_gate2.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — promoted outcome logged to run.log
# ─────────────────────────────────────────────────────────────────────────────

class TestPromotionLogging:
    def _read_log(self, ctrl) -> str:
        log_path = Path(ctrl.state.agent_dir) / "run.log"
        return log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    def test_promotion_written_to_run_log(self, tmp_path):
        """AC3: a promoted outcome is written to run.log with the score."""
        tasks = [_make_task("T-PROM")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner.maybe_tune.return_value = _promoted(score=0.91)

        _run_tasks(ctrl, [_passed_outer("T-PROM")])

        log = self._read_log(ctrl)
        assert "auto_tuner" in log.lower() or "AUTO-E1" in log
        assert "0.91" in log

    def test_promotion_log_contains_score(self, tmp_path):
        """AC3: the score appears in the log entry."""
        tasks = [_make_task("T-SC")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner.maybe_tune.return_value = _promoted(score=0.77)

        _run_tasks(ctrl, [_passed_outer("T-SC")])

        assert "0.77" in self._read_log(ctrl)

    def test_promotion_log_for_multiple_promotions(self, tmp_path):
        """AC3: each promotion is logged independently."""
        tasks = [_make_task("T-A"), _make_task("T-B")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner.maybe_tune.side_effect = [
            _promoted(score=0.80),
            _promoted(score=0.90),
        ]

        _run_tasks(ctrl, [_passed_outer("T-A"), _passed_outer("T-B")])

        log = self._read_log(ctrl)
        assert "0.80" in log
        assert "0.90" in log


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — non-promoted outcome does NOT write to run.log
# ─────────────────────────────────────────────────────────────────────────────

class TestNonPromotionSilent:
    def _read_log(self, ctrl) -> str:
        log_path = Path(ctrl.state.agent_dir) / "run.log"
        return log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    def test_non_promoted_not_logged(self, tmp_path):
        """AC4: a non-promoted (not triggered) outcome is silent in run.log."""
        tasks = [_make_task("T-NOPROM")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner.maybe_tune.return_value = _not_promoted()

        _run_tasks(ctrl, [_passed_outer("T-NOPROM")])

        log = self._read_log(ctrl)
        # tune-related terms should NOT appear when not promoted
        assert "AUTO-E1" not in log
        assert "promoted" not in log

    def test_triggered_but_not_promoted_not_logged(self, tmp_path):
        """AC4: triggered-but-discarded outcome is also silent."""
        tasks = [_make_task("T-DISC")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner.maybe_tune.return_value = TuneOutcome(
            agent_name="validator",
            triggered=True,
            promoted=False,
            reason="score too low",
            new_prompt_score=0.55,
        )

        _run_tasks(ctrl, [_passed_outer("T-DISC")])

        log = self._read_log(ctrl)
        assert "AUTO-E1" not in log
        assert "promoted" not in log


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — auto metrics isolated from interactive metrics.json
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsIsolation:
    def test_auto_metrics_stream_writes_to_agent_dir(self, tmp_path):
        """AC5: AutoMetricsStream writes to <agent_dir>/metrics.json."""
        agent_dir = tmp_path / ".agent"
        stream    = AutoMetricsStream(agent_dir)
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_auto_metrics_not_interactive_default(self, tmp_path):
        """AC5: auto metrics path != interactive default metrics.json."""
        from tools.metrics_collector import METRICS_PATH as INTERACTIVE
        agent_dir = tmp_path / ".agent"
        stream    = AutoMetricsStream(agent_dir)
        # Absolute paths must differ (resolve both for comparison)
        assert stream.metrics_path.resolve() != INTERACTIVE.resolve()

    def test_interactive_metrics_json_untouched_after_run(self, tmp_path):
        """AC5: interactive metrics.json is not created/modified by auto run."""
        interactive = Path("metrics.json")
        existed_before = interactive.exists()

        tasks = [_make_task("T-ISO")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_passed_outer("T-ISO")])

        # The interactive file should not have been created by this run
        if not existed_before:
            assert not interactive.exists(), \
                "interactive metrics.json should not be created by auto run"

    def test_auto_metrics_stream_records_to_agent_dir(self, tmp_path):
        """AC5: after a run, agent_dir/metrics.json is populated."""
        tasks = [_make_task("T-REAL"), _make_task("T-REAL2")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [
            _passed_outer("T-REAL"),
            _passed_outer("T-REAL2"),
        ])

        metrics_path = ctrl.agent_dir / "metrics.json"
        assert metrics_path.exists()
        import json
        records = json.loads(metrics_path.read_text())
        assert len(records) == 2

    def test_auto_metrics_stream_refuses_interactive_path(self, tmp_path):
        """AC9: AutoMetricsStream raises if given the interactive default path."""
        # Only runnable when CWD has no existing metrics.json collision
        # (the guard compares resolved paths)
        cwd_metrics = Path("metrics.json").resolve()
        fake_agent_dir = cwd_metrics.parent   # would resolve to project root
        # This should raise ValueError because the metrics path would collide
        # with the interactive default
        with pytest.raises(ValueError, match="collide"):
            AutoMetricsStream(fake_agent_dir)


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — hot-reload fired on promotion
# ─────────────────────────────────────────────────────────────────────────────

class TestHotReload:
    def test_prompt_store_push_called_on_promotion(self, tmp_path):
        """AC6: AutoTuner.prompt_store.push is called when a prompt is promoted."""
        from tools.auto.auto_tuner import AutoTuner
        from tools.prompt_store import PromptStore
        from tools.metrics_collector import MetricsCollector

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)

        prompt_store = MagicMock(spec=PromptStore)
        prompt_store.get_current.return_value = "old prompt"

        metrics_collector = MetricsCollector(
            metrics_path=agent_dir / "metrics.json"
        )

        fake_optimizer = MagicMock()
        fake_optimizer.generate_candidate.return_value = "new improved prompt"

        from tools.prompt_evaluator import PromptEvaluator
        fake_evaluator = MagicMock(spec=PromptEvaluator)
        fake_evaluator.evaluate.return_value = SimpleNamespace(
            promoted=True, score=0.88, reason="better"
        )

        reload_called = []
        tuner = AutoTuner(
            prompt_store=prompt_store,
            metrics_collector=metrics_collector,
            prompt_optimizer=fake_optimizer,
            prompt_evaluator=fake_evaluator,
            agent_name="validator",
            reload_agents_fn=lambda: reload_called.append(1),
            enabled=True,
            min_runs=0,          # trigger immediately
            trigger_avg_iter=0,  # always above threshold
            trigger_json_fail_rate=0,
        )

        outcome = tuner.maybe_tune()

        assert outcome.promoted is True
        prompt_store.push.assert_called_once_with("validator", "new improved prompt", 0.88)
        assert len(reload_called) == 1, "reload_agents_fn must be called on promotion"

    def test_reload_fn_not_called_when_not_promoted(self, tmp_path):
        """AC6: reload_agents_fn is NOT called when the candidate is discarded."""
        from tools.auto.auto_tuner import AutoTuner
        from tools.prompt_store import PromptStore
        from tools.metrics_collector import MetricsCollector

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)

        prompt_store = MagicMock(spec=PromptStore)
        prompt_store.get_current.return_value = "old prompt"
        metrics_collector = MetricsCollector(metrics_path=agent_dir / "metrics.json")

        fake_optimizer = MagicMock()
        fake_optimizer.generate_candidate.return_value = "candidate"

        from tools.prompt_evaluator import PromptEvaluator
        fake_evaluator = MagicMock(spec=PromptEvaluator)
        fake_evaluator.evaluate.return_value = SimpleNamespace(
            promoted=False, score=0.40, reason="not better"
        )

        reload_called = []
        tuner = AutoTuner(
            prompt_store=prompt_store,
            metrics_collector=metrics_collector,
            prompt_optimizer=fake_optimizer,
            prompt_evaluator=fake_evaluator,
            agent_name="validator",
            reload_agents_fn=lambda: reload_called.append(1),
            enabled=True,
            min_runs=0,
            trigger_avg_iter=0,
            trigger_json_fail_rate=0,
        )

        outcome = tuner.maybe_tune()

        assert outcome.promoted is False
        prompt_store.push.assert_not_called()
        assert len(reload_called) == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — metrics_stream=None / auto_tuner=None handled safely
# ─────────────────────────────────────────────────────────────────────────────

class TestNoneSafe:
    def test_metrics_stream_none_does_not_crash(self, tmp_path):
        """AC7: metrics_stream=None is silently skipped."""
        tasks = [_make_task("T-NM")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.metrics_stream = None

        _run_tasks(ctrl, [_passed_outer("T-NM")])   # must not raise

    def test_auto_tuner_none_does_not_crash(self, tmp_path):
        """AC7: auto_tuner=None is silently skipped."""
        tasks = [_make_task("T-NT")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.auto_tuner = None

        _run_tasks(ctrl, [_passed_outer("T-NT")])   # must not raise

    def test_both_none_does_not_crash(self, tmp_path):
        """AC7: both=None is silently skipped."""
        tasks = [_make_task("T-BOTH")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.metrics_stream = None
        ctrl.auto_tuner     = None

        _run_tasks(ctrl, [_passed_outer("T-BOTH")])   # must not raise

    def test_none_safe_with_exhausted_task(self, tmp_path):
        """AC7: both=None with an exhausted task still doesn't crash."""
        tasks = [_make_task("T-EXNONE")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.metrics_stream = None
        ctrl.auto_tuner     = None

        _run_tasks(ctrl, [_exhausted_outer("T-EXNONE")])   # must not raise

    def test_metrics_stream_none_run_still_completes(self, tmp_path):
        """AC7: run completes and tasks are DONE even with metrics_stream=None."""
        tasks = [_make_task("T-COMP")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.metrics_stream = None

        stop_reason, tasks_done = _run_tasks(ctrl, [_passed_outer("T-COMP")])

        assert stop_reason is None
        assert tasks_done == 1


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: real AutoMetricsStream + mock AutoTuner
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndMetricsWiring:
    def test_real_stream_records_correct_count(self, tmp_path):
        """Record all tasks via real stream, verify count in metrics.json."""
        import json

        tasks = [_make_task(f"T-{i}") for i in range(4)]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_passed_outer(f"T-{i}") for i in range(4)])

        metrics_path = ctrl.agent_dir / "metrics.json"
        records      = json.loads(metrics_path.read_text())
        assert len(records) == 4

    def test_real_stream_approved_field_correct(self, tmp_path):
        """Passed tasks have validator_status='approved' in the metrics file."""
        import json

        tasks = [_make_task("T-APR")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_passed_outer("T-APR")])

        records = json.loads(
            (ctrl.agent_dir / "metrics.json").read_text()
        )
        assert records[0]["validator_status"] == "approved"

    def test_real_stream_rejected_field_correct(self, tmp_path):
        """Exhausted tasks have validator_status='rejected' in the metrics file."""
        import json

        tasks = [_make_task("T-REJ")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_exhausted_outer("T-REJ")])

        records = json.loads(
            (ctrl.agent_dir / "metrics.json").read_text()
        )
        assert records[0]["validator_status"] == "rejected"

    def test_real_stream_improvement_json_ok_is_none(self, tmp_path):
        """AUTO-E2: improvement_json_ok is always None in auto records."""
        import json

        tasks = [_make_task("T-ISO")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_passed_outer("T-ISO")])

        records = json.loads(
            (ctrl.agent_dir / "metrics.json").read_text()
        )
        assert records[0]["improvement_json_ok"] is None

    def test_maybe_tune_called_after_real_record(self, tmp_path):
        """AC2 + real stream: maybe_tune is still called after real record_gate2."""
        tasks = [_make_task("T-TUNE")]
        ctrl  = _make_controller(tmp_path, tasks, real_metrics=True)

        _run_tasks(ctrl, [_passed_outer("T-TUNE")])

        ctrl.auto_tuner.maybe_tune.assert_called_once()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))