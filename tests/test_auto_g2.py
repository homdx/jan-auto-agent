"""tests/test_auto_g2.py — AUTO-G2/G3/G4: _run_task_loop execution wiring.

Covers the story ACs:

AUTO-G2 — EXECUTION loop wiring (replace the skeleton)
  AC1 — A task that the (fake) outer_loop can satisfy ends DONE with a commit;
         the skeleton pass-through is gone.
  AC2 — Caps still fire mid-loop and stop gracefully (A4 behaviour preserved).
  AC3 — Dependency guard: a task whose dep is not DONE is set BLOCKED and
         skipped; the run continues with independent tasks.

AUTO-G3 — Commit-on-success wiring
  AC: N validated tasks → N commits; hash stored (commit= in task record).
  AC: No-git path marks DONE without crash.

AUTO-G4 — Exhaustion → knowledge + ticket wiring
  AC: A permanently-failing task yields an ExhaustionOutcome with a ticket_id,
      the run continues (does not stall).

Additional:
  * run_trace methods are called at the right moments.
  * progress_display.tick_code() is called after each task (pass or exhausted).
  * auto_metrics.record_gate2 is called; auto_tuner.maybe_tune is called.
  * commit_helper uses the controller's existing GitManager (no second make_git_manager).

Test strategy
-------------
All external I/O is patched with lightweight fakes:
  - outer_loop is injected via make_outer_loop mock
  - CommitOnSuccess is injected via a spy
  - ExhaustionHandler is injected via a spy
  - state, git, run_trace, progress_display, metrics_stream, auto_tuner are all
    lightweight MagicMock or real StateStore instances backed by tmp_path.

Tests run fully offline; no real LLM or git subprocess calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import StateStore, STATUS_DONE, STATUS_BLOCKED
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.exhaustion_handler import ExhaustionOutcome


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_task(task_id: str, *, deps: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "instruction": "do something",
        "target_files": [],
        "acceptance_check": "true",
        "status": "todo",
        "dependencies": deps or [],
        "attempt": 0,
        "round": 0,
        "cited_locations": [],
    }


def _passed_result(task_id: str, rounds: int = 1, n_inner: int = 1) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=1, last_feedback="") for _ in range(n_inner)]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=rounds,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _exhausted_result(task_id: str, rounds: int = 10) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=5, last_feedback="still broken")]
    return OuterLoopResult(
        task_id=task_id,
        passed=False,
        rounds_used=rounds,
        exhausted=True,
        feedback_files=[],
        inner_results=inner,
    )


def _make_controller(tmp_path: Path, tasks: list[dict], *, runtime_cap: float = 0,
                     task_cap: int = 0) -> AutoController:
    """Build a minimal AutoController with a real StateStore backed by tmp_path."""
    base = tmp_path / "repo"
    base.mkdir()

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"

    import time
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()

    from tools.auto.controller import RunLimits
    ctrl.limits = RunLimits(
        max_runtime_sec=runtime_cap,
        max_tasks_per_run=task_cap,
    )

    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    for t in tasks:
        ctrl.state.upsert_task(t)

    ctrl.git = MagicMock()  # fake GitManager (non-None → commit path active)
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )

    return ctrl


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G2 AC1: skeleton is gone, outer_loop is called, task ends DONE
# ─────────────────────────────────────────────────────────────────────────────

class TestG2TaskPassesAndCommits:
    def test_passed_task_marked_done_with_commit(self, tmp_path):
        """AC1/G3: passed task → commit called → STATUS_DONE with commit hash."""
        task = _make_task("T-1")
        ctrl = _make_controller(tmp_path, [task])

        fake_result = _passed_result("T-1")
        fake_commit_hash = "abc123def456789"

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result

        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = fake_commit_hash

        fake_exhaustion = MagicMock()

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler", return_value=fake_exhaustion):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 1
        fake_outer.run_task.assert_called_once_with(task, ctrl.base_dir)
        fake_commit_helper.commit.assert_called_once_with(task, fake_result)
        fake_exhaustion.handle.assert_not_called()

    def test_commit_hash_prefix_in_log(self, tmp_path):
        """G3: log entry contains commit hash prefix and rounds."""
        task = _make_task("T-2")
        ctrl = _make_controller(tmp_path, [task])

        fake_result = _passed_result("T-2", rounds=3)
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "deadbeef1234"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        log_text = (ctrl.agent_dir / "run.log").read_text()
        assert "deadbeef1234" in log_text
        assert "rounds=3" in log_text

    def test_no_git_path_marks_done_without_crash(self, tmp_path):
        """G3 no-git: when self.git is None, task ends DONE without commit."""
        task = _make_task("T-3")
        ctrl = _make_controller(tmp_path, [task])
        ctrl.git = None  # simulate git unavailable

        fake_result = _passed_result("T-3")
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 1
        t = ctrl.state.get_task("T-3")
        assert t["status"] == STATUS_DONE

    def test_multiple_tasks_all_pass(self, tmp_path):
        """G2/G3: three passing tasks → tasks_done=3, commit called 3 times."""
        tasks = [_make_task(f"T-{i}") for i in range(1, 4)]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _passed_result(f"T-{i}") for i in range(1, 4)
        ]
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "cafebabe"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 3
        assert fake_commit_helper.commit.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G4: Exhaustion → knowledge + ticket, run continues
# ─────────────────────────────────────────────────────────────────────────────

class TestG4Exhaustion:
    def test_exhausted_task_calls_exhaustion_handler(self, tmp_path):
        """G4: failed task → exhaustion_handler.handle() called, run continues."""
        task = _make_task("T-X")
        ctrl = _make_controller(tmp_path, [task])

        fake_result = _exhausted_result("T-X")
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result

        fake_exhaustion = MagicMock()
        fake_outcome = ExhaustionOutcome(
            task_id="T-X",
            ticket_id="TICKET-T-X",
            knowledge_path=ctrl.agent_dir / "tasks/T-X/knowledge.md",
            ticket_path=ctrl.agent_dir / "tickets/TICKET-T-X.json",
        )
        fake_exhaustion.handle.return_value = fake_outcome

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler",
                   return_value=fake_exhaustion):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 0  # exhausted tasks don't count as done
        fake_exhaustion.handle.assert_called_once_with(task, fake_result)

    def test_exhausted_task_logged_with_ticket(self, tmp_path):
        """G4: log entry contains ticket_id after exhaustion."""
        task = _make_task("T-Y")
        ctrl = _make_controller(tmp_path, [task])

        fake_result = _exhausted_result("T-Y")
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result

        fake_exhaustion = MagicMock()
        fake_exhaustion.handle.return_value = ExhaustionOutcome(
            task_id="T-Y",
            ticket_id="TICKET-T-Y",
            knowledge_path=Path("/dev/null"),
            ticket_path=Path("/dev/null"),
        )

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler",
                   return_value=fake_exhaustion):
            ctrl._run_task_loop()

        log = (ctrl.agent_dir / "run.log").read_text()
        assert "TICKET-T-Y" in log

    def test_run_continues_after_exhausted_task(self, tmp_path):
        """G4: run continues with independent tasks after exhaustion."""
        tasks = [_make_task("T-FAIL"), _make_task("T-PASS")]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _exhausted_result("T-FAIL"),
            _passed_result("T-PASS"),
        ]
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abcdef"
        fake_exhaustion = MagicMock()
        fake_exhaustion.handle.return_value = ExhaustionOutcome(
            "T-FAIL", "TICKET-T-FAIL", Path("/dev/null"), Path("/dev/null")
        )

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler",
                   return_value=fake_exhaustion):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 1  # only T-PASS counted
        assert fake_outer.run_task.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G2 AC2: caps preserved
# ─────────────────────────────────────────────────────────────────────────────

class TestG2CapsPreserved:
    def test_task_cap_fires_before_second_task(self, tmp_path):
        """AC2: task_cap=1 stops after first task; second task not started."""
        tasks = [_make_task("T-1"), _make_task("T-2")]
        ctrl = _make_controller(tmp_path, tasks, task_cap=1)

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-1")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abcdef"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason == "task_cap"
        assert tasks_done == 1
        fake_outer.run_task.assert_called_once()  # T-2 never executed

    def test_runtime_cap_fires_immediately_when_already_exceeded(self, tmp_path):
        """AC2: runtime cap fires before any task when time already elapsed."""
        task = _make_task("T-1")
        ctrl = _make_controller(tmp_path, [task], runtime_cap=0.001)
        ctrl._start_time = ctrl._time_fn() - 10  # pretend 10s already elapsed

        fake_outer = MagicMock()

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason == "runtime_cap"
        assert tasks_done == 0
        fake_outer.run_task.assert_not_called()

    def test_no_cap_all_tasks_run(self, tmp_path):
        """AC2: no caps → all tasks run, stop_reason is None."""
        tasks = [_make_task(f"T-{i}") for i in range(5)]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [_passed_result(f"T-{i}") for i in range(5)]
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "000000"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 5


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G2 AC3: dependency guard
# ─────────────────────────────────────────────────────────────────────────────

class TestG2DependencyGuard:
    def test_blocked_by_incomplete_dep_skipped(self, tmp_path):
        """AC3: task with unsatisfied dep → BLOCKED, outer_loop not called."""
        dep_task = _make_task("T-DEP")
        # status is already "todo" (not DONE) — no override needed
        blocked_task = _make_task("T-BLOCKED", deps=["T-DEP"])

        ctrl = _make_controller(tmp_path, [dep_task, blocked_task])

        fake_outer = MagicMock()
        # T-DEP has no deps so it will run; T-BLOCKED will be blocked
        fake_outer.run_task.return_value = _passed_result("T-DEP")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abc"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        # T-DEP ran (no deps), T-BLOCKED was skipped (dep not done at loop time)
        # Note: loop order is pending order; T-DEP runs first → DONE,
        # but T-BLOCKED sees T-DEP as DONE only if commit_helper updated it.
        # commit_helper is mocked; state.set_task_status DONE is NOT called by mock.
        # So T-BLOCKED's dep T-DEP remains not-DONE at the dependency check.
        blocked = ctrl.state.get_task("T-BLOCKED")
        assert blocked["status"] == STATUS_BLOCKED

    def test_dep_done_allows_task_to_run(self, tmp_path):
        """Dep already DONE in state → dependent task is not blocked."""
        dep_task = _make_task("T-DEP")
        dep_task["status"] = STATUS_DONE
        child_task = _make_task("T-CHILD", deps=["T-DEP"])

        ctrl = _make_controller(tmp_path, [dep_task, child_task])
        ctrl.state.set_task_status("T-DEP", STATUS_DONE)

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-CHILD")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "cafebabe"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        # T-DEP is already DONE so not in pending; T-CHILD runs
        assert tasks_done == 1


# ─────────────────────────────────────────────────────────────────────────────
# Observability: run_trace, progress_display, metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestG2Observability:
    def test_run_trace_called_on_pass(self, tmp_path):
        """run_trace.log_task_start and log_task_done are called for passed task."""
        task = _make_task("T-OBS")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-OBS")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abcdef"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_start.assert_called_once_with("T-OBS", "Task T-OBS")
        ctrl.run_trace.log_task_done.assert_called_once_with("T-OBS", "abcdef")
        ctrl.run_trace.log_task_blocked.assert_not_called()

    def test_run_trace_called_on_exhaustion(self, tmp_path):
        """run_trace.log_task_blocked called for exhausted task (not log_task_done)."""
        task = _make_task("T-EX")
        ctrl = _make_controller(tmp_path, [task])

        fake_result = _exhausted_result("T-EX")
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = fake_result

        fake_exhaustion = MagicMock()
        fake_exhaustion.handle.return_value = ExhaustionOutcome(
            "T-EX", "TICKET-T-EX", Path("/dev/null"), Path("/dev/null")
        )

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler",
                   return_value=fake_exhaustion):
            ctrl._run_task_loop()

        ctrl.run_trace.log_task_done.assert_not_called()
        ctrl.run_trace.log_task_blocked.assert_called_once()

    def test_progress_display_ticked_after_each_task(self, tmp_path):
        """progress_display.tick_code() called once per task regardless of outcome."""
        tasks = [_make_task("T-P"), _make_task("T-E")]
        ctrl = _make_controller(tmp_path, tasks)

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _passed_result("T-P"),
            _exhausted_result("T-E"),
        ]
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abc"
        fake_exhaustion = MagicMock()
        fake_exhaustion.handle.return_value = ExhaustionOutcome(
            "T-E", "TICKET-T-E", Path("/dev/null"), Path("/dev/null")
        )

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler",
                   return_value=fake_exhaustion):
            ctrl._run_task_loop()

        assert ctrl.progress_display.tick_code.call_count == 2

    def test_metrics_and_tuner_called_per_task(self, tmp_path):
        """record_gate2 and maybe_tune are called once per task."""
        task = _make_task("T-M")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-M", n_inner=2)
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abc"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        ctrl.metrics_stream.record_gate2.assert_called_once_with(
            "T-M",
            approved=True,
            feedback="",       # last inner result on pass has empty feedback
            attempts=2,        # sum of attempts_used across inner_results
            prompt_store=ctrl.auto_tuner.prompt_store,
        )
        ctrl.auto_tuner.maybe_tune.assert_called_once()

    def test_auto_tuner_promotion_logged(self, tmp_path):
        """Promoted tuner outcome writes a log entry."""
        task = _make_task("T-TUNE")
        ctrl = _make_controller(tmp_path, [task])
        ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
            promoted=True, new_prompt_score=0.87
        )

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-TUNE")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = "abc"

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        log = (ctrl.agent_dir / "run.log").read_text()
        assert "auto_tuner promoted" in log
        assert "0.87" in log


# ─────────────────────────────────────────────────────────────────────────────
# G2: CommitOnSuccess re-uses controller.git (not a second GitManager)
# ─────────────────────────────────────────────────────────────────────────────

class TestG2GitManagerReuse:
    def test_commit_on_success_uses_controller_git(self, tmp_path):
        """G2 fix: CommitOnSuccess is constructed with controller.git, not a new one.

        We patch CommitOnSuccess at the class level and assert it receives
        ctrl.git (the controller's pre-existing GitManager).
        """
        task = _make_task("T-GIT")
        ctrl = _make_controller(tmp_path, [task])

        sentinel_git = ctrl.git  # remember the controller's git manager

        captured_args = {}

        class _CapturingCommitOnSuccess:
            def __init__(self, git_manager, state_store, **kwargs):
                captured_args["git_manager"] = git_manager
                captured_args["state_store"] = state_store
                captured_args.update(kwargs)

            def commit(self, task, result):
                return "captured_hash"

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-GIT")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", _CapturingCommitOnSuccess), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        assert captured_args["git_manager"] is sentinel_git, (
            "CommitOnSuccess must receive the controller's existing git manager, "
            "not a newly-constructed one."
        )


# ─────────────────────────────────────────────────────────────────────────────
# G2: empty task list
# ─────────────────────────────────────────────────────────────────────────────

class TestG2EdgeCases:
    def test_no_pending_tasks_returns_immediately(self, tmp_path):
        """No tasks → stop_reason None, tasks_done 0, outer_loop never called."""
        ctrl = _make_controller(tmp_path, [])

        fake_outer = MagicMock()

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 0
        fake_outer.run_task.assert_not_called()

    def test_commit_returns_none_on_git_error(self, tmp_path):
        """G3: git error → commit returns None → task still counted as done (outer_loop set DONE)."""
        task = _make_task("T-GITERR")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-GITERR")
        fake_commit_helper = MagicMock()
        fake_commit_helper.commit.return_value = None  # simulate git error

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess",
                   return_value=fake_commit_helper), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        # tasks_done is still incremented; outer_loop already set DONE
        assert tasks_done == 1
        assert stop_reason is None
        # run_trace.log_task_done called with None commit hash
        ctrl.run_trace.log_task_done.assert_called_once_with("T-GITERR", None)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-CR-14: controller wires SummaryMemory into CommitOnSuccess for creative
# ─────────────────────────────────────────────────────────────────────────────

class TestCR14SynopsisWiring:
    def test_creative_wires_summary_memory(self, tmp_path):
        """In creative mode the commit helper must receive a non-None
        summary_memory + task_mode='creative', or synopsis.md is never written.
        """
        task = _make_task("T-SYN")
        ctrl = _make_controller(tmp_path, [task])

        captured = {}

        class _CapturingCommitOnSuccess:
            def __init__(self, git_manager, state_store, **kwargs):
                captured.update(kwargs)

            def commit(self, task, result):
                return "h"

        sentinel = object()
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-SYN")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", _CapturingCommitOnSuccess), \
             patch("tools.auto.summary_memory.make_summary_memory", return_value=sentinel), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop(task_mode="creative")

        assert captured.get("summary_memory") is sentinel
        assert captured.get("task_mode") == "creative"

    def test_code_mode_does_not_wire_summary_memory(self, tmp_path):
        task = _make_task("T-CODE")
        ctrl = _make_controller(tmp_path, [task])

        captured = {}

        class _CapturingCommitOnSuccess:
            def __init__(self, git_manager, state_store, **kwargs):
                captured.update(kwargs)

            def commit(self, task, result):
                return "h"

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-CODE")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess", _CapturingCommitOnSuccess), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop(task_mode="code")

        assert captured.get("summary_memory") is None
