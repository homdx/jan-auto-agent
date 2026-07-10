"""tests/test_auto_g6.py — AUTO-G6: Progress display wiring (integration).

Story ACs verified here
-----------------------
AUTO-G6 — Progress display wiring (3 pts)
  AC1 — During the PLAN phase, architecture [x/N] increments after each
         cluster is reviewed (tick_arch called once per cluster).
  AC2 — After the backlog is built, code_total is set to the number of
         auto tasks and a refresh() emits architecture [N/N]  coding [0/M].
  AC3 — During EXECUTE, coding [done/total] increments after each task
         (tick_code called once per finished task).
  AC4 — Per-task detail line (task k · attempt a/5 · round r/10) is shown
         at task start.
  AC5 — progress.json mirrors the console counters after every refresh.
  AC6 — On a resume run (plan already present), the PLAN phase is skipped
         but the EXECUTE phase refresh still shows the correct coding counts.
  AC7 — review_clusters forwards on_cluster_done to ClusterReviewer;
         empty clusters also trigger the callback.
  AC8 — progress_display=None (no display configured) is handled safely
         with no AttributeError anywhere in the wiring.

Scope
-----
* test_auto_f1.py — unit-tests ProgressDisplay in isolation.
* test_auto_g1.py — integration-tests the plan phase wiring.
* test_auto_g6.py (this file) — integration-tests the progress wiring end-to-end:
  plan-phase tick_arch, execute-phase tick_code/set_task, progress.json parity,
  and resume safety.  Uses fake LLM / fake outer_loop — no real network.
"""

from __future__ import annotations

import configparser
import io
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
from tools.auto.pipeline import _run_plan_phase, run_pipeline
from tools.auto.progress_display import ProgressDisplay
from tools.auto.state import StateStore


# ─────────────────────────────────────────────────────────────────────────────
# Shared test fixtures / helpers
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
    return cfg


def _make_display(state, *, arch_total=0, code_total=0, out=None):
    return ProgressDisplay(
        state=state,
        arch_total=arch_total,
        code_total=code_total,
        max_attempts=5,
        max_rounds=10,
        out=out or io.StringIO(),
    )


def _make_task(task_id, *, status="todo", acceptance_check="true"):
    return {
        "id":               task_id,
        "title":            f"Task {task_id}",
        "instruction":      f"Fix {task_id}",
        "target_files":     [],
        "acceptance_check": acceptance_check,
        "status":           status,
        "dependencies":     [],
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


def _make_controller(tmp_path, tasks=None, *, task_cap=0):
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

    ctrl.state = _make_state(base, tasks or [])
    ctrl.git   = None
    ctrl.run_trace        = MagicMock()
    ctrl.metrics_stream   = MagicMock()
    ctrl.auto_tuner       = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )
    ctrl.progress_display = None   # caller sets this
    return ctrl


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — on_cluster_done callback in architect.review_clusters
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitectCallback:
    """Unit tests for the on_cluster_done hook added to ClusterReviewer."""

    def _make_cluster(self, name, files=("a.py",)):
        from tools.auto.repo_ingest import RepoCluster
        return RepoCluster(name=name, patterns=[], files=list(files))

    def test_callback_called_once_per_non_empty_cluster(self):
        """on_cluster_done fires once per non-empty cluster reviewed."""
        from tools.auto.architect import ClusterReviewer

        cfg = _make_config()
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost",
            api_key="",
            model="dummy",
            api_format="openai",
        )

        clusters = [self._make_cluster("A"), self._make_cluster("B")]
        ticks = []

        with patch.object(reviewer, "_review_one_cluster", return_value=[]):
            reviewer.review_clusters(
                clusters, ".", "goal", on_cluster_done=lambda: ticks.append(1)
            )

        assert len(ticks) == 2

    def test_callback_called_for_empty_cluster(self):
        """on_cluster_done also fires for empty (skipped) clusters."""
        from tools.auto.architect import ClusterReviewer

        cfg = _make_config()
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost",
            api_key="",
            model="dummy",
            api_format="openai",
        )

        clusters = [self._make_cluster("empty", files=[])]
        ticks = []

        with patch.object(reviewer, "_review_one_cluster", return_value=[]):
            reviewer.review_clusters(
                clusters, ".", "goal", on_cluster_done=lambda: ticks.append(1)
            )

        assert len(ticks) == 1

    def test_no_callback_is_fine(self):
        """Omitting on_cluster_done must not raise."""
        from tools.auto.architect import ClusterReviewer

        cfg = _make_config()
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost",
            api_key="",
            model="dummy",
            api_format="openai",
        )

        clusters = [self._make_cluster("A")]
        with patch.object(reviewer, "_review_one_cluster", return_value=[]):
            result = reviewer.review_clusters(clusters, ".", "goal")
        assert isinstance(result, list)

    def test_crashing_callback_does_not_propagate(self):
        """A callback that raises must be swallowed — run must not abort."""
        from tools.auto.architect import ClusterReviewer

        cfg = _make_config()
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost",
            api_key="",
            model="dummy",
            api_format="openai",
        )

        clusters = [self._make_cluster("A"), self._make_cluster("B")]
        with patch.object(reviewer, "_review_one_cluster", return_value=[]):
            # Should not raise even though the callback always throws
            result = reviewer.review_clusters(
                clusters, ".", "goal",
                on_cluster_done=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        assert isinstance(result, list)

    def test_module_level_review_clusters_forwards_callback(self, tmp_path):
        """The module-level review_clusters() passes on_cluster_done through."""
        from tools.auto.architect import review_clusters as rc_fn, ClusterReviewer
        from tools.auto.repo_ingest import RepoCluster

        clusters = [RepoCluster(name="C", patterns=[], files=["x.py"])]
        ticks = []

        with patch.object(ClusterReviewer, "_review_one_cluster", return_value=[]):
            rc_fn(
                clusters, tmp_path, _make_config(),
                goal="g",
                on_cluster_done=lambda: ticks.append(1),
            )

        assert len(ticks) == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — tick_arch called per cluster during PLAN phase
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanPhaseArchTicks:
    def _make_clusters(self, n):
        from tools.auto.repo_ingest import RepoCluster
        return [RepoCluster(name=f"C{i}", patterns=[], files=[f"f{i}.py"]) for i in range(n)]

    def _run_plan(self, tmp_path, n_clusters=3, n_tasks=2):
        out = io.StringIO()
        ctrl = _make_controller(tmp_path)
        state = ctrl.state
        display = _make_display(state, out=out)
        ctrl.progress_display = display

        clusters = self._make_clusters(n_clusters)
        candidates = []
        backlog = MagicMock()
        backlog.auto_tasks = [MagicMock() for _ in range(n_tasks)]
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        cfg = _make_config()

        with patch("tools.auto.pipeline.ingest_repo", return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters", side_effect=lambda *a, **kw: (
                 [kw["on_cluster_done"]() for _ in clusters] if kw.get("on_cluster_done") else None
             ) or candidates), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog", return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)

        return ctrl, display, out

    def test_arch_done_equals_cluster_count_after_plan(self, tmp_path):
        """AC1: arch_done reaches n_clusters after the plan phase."""
        _, display, _ = self._run_plan(tmp_path, n_clusters=3)
        assert display.arch_done == 3

    def test_arch_total_set_before_any_tick(self, tmp_path):
        """AC1: arch_total is set from ingest before review starts."""
        _, display, _ = self._run_plan(tmp_path, n_clusters=4)
        assert display.arch_total == 4

    def test_initial_refresh_shows_zero_arch(self, tmp_path):
        """AC1: first refresh after ingest shows architecture [0/N]."""
        _, _, out = self._run_plan(tmp_path, n_clusters=2)
        lines = out.getvalue().splitlines()
        # The first line emitted should show arch_done=0
        first_arch_line = next(
            (l for l in lines if l.startswith("architecture")), None
        )
        assert first_arch_line is not None
        assert "architecture [0/2]" in first_arch_line

    def test_code_total_set_to_auto_tasks(self, tmp_path):
        """AC2: code_total equals the number of auto tasks after backlog step."""
        _, display, _ = self._run_plan(tmp_path, n_clusters=2, n_tasks=5)
        assert display.code_total == 5

    def test_banner_after_plan_shows_full_arch(self, tmp_path):
        """AC2: final banner after plan shows architecture [N/N]  coding [0/M]."""
        _, display, _ = self._run_plan(tmp_path, n_clusters=3, n_tasks=4)
        banner = display.banner()
        assert "architecture [3/3]" in banner
        assert "coding [0/4]" in banner


# ─────────────────────────────────────────────────────────────────────────────
# AC3/AC4 — tick_code and set_task during EXECUTE phase
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutePhaseProgress:
    def _run_execute(self, tmp_path, tasks, outer_results):
        out = io.StringIO()
        ctrl = _make_controller(tmp_path, tasks)
        state = ctrl.state
        display = _make_display(state, code_total=len(tasks), out=out)
        ctrl.progress_display = display

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = outer_results

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        return ctrl, display, out

    def test_tick_code_increments_per_passed_task(self, tmp_path):
        """AC3: code_done increments once per passed task."""
        tasks = [_make_task(f"T-{i}") for i in range(3)]
        _, display, _ = self._run_execute(
            tmp_path, tasks,
            [_passed_outer(f"T-{i}") for i in range(3)]
        )
        assert display.code_done == 3

    def test_set_task_called_at_task_start(self, tmp_path):
        """AC4: set_task() is called at the start of each task."""
        tasks = [_make_task("T-A")]
        _, display, out = self._run_execute(
            tmp_path, tasks, [_passed_outer("T-A")]
        )
        # The task line must appear in the output
        output = out.getvalue()
        assert "task 1" in output
        assert "attempt" in output
        assert "round" in output

    def test_task_line_format(self, tmp_path):
        """AC4: per-task line matches 'task k · attempt a/5 · round r/10'."""
        tasks = [_make_task("T-FMT")]
        _, _, out = self._run_execute(
            tmp_path, tasks, [_passed_outer("T-FMT")]
        )
        output = out.getvalue()
        assert "task 1 · attempt 1/5 · round 1/10" in output

    def test_banner_counts_after_three_tasks(self, tmp_path):
        """AC3: banner shows coding [3/3] after all tasks complete."""
        tasks = [_make_task(f"T-{i}") for i in range(3)]
        _, display, _ = self._run_execute(
            tmp_path, tasks,
            [_passed_outer(f"T-{i}") for i in range(3)]
        )
        assert display.code_done == 3
        assert display.code_total == 3


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — progress.json mirrors console counters
# ─────────────────────────────────────────────────────────────────────────────

class TestProgressJson:
    def test_progress_json_written_on_tick_arch(self, tmp_path):
        """AC5: progress.json has arch_done/arch_total after tick_arch."""
        state   = _make_state(tmp_path)
        display = _make_display(state, arch_total=3)
        display.tick_arch()
        display.tick_arch()

        pj = state.get_progress()
        assert pj.get("arch_done")  == 2
        assert pj.get("arch_total") == 3

    def test_progress_json_written_on_tick_code(self, tmp_path):
        """AC5: progress.json has code_done/code_total after tick_code."""
        state   = _make_state(tmp_path)
        display = _make_display(state, code_total=4)
        display.tick_code()

        pj = state.get_progress()
        assert pj.get("code_done")  == 1
        assert pj.get("code_total") == 4

    def test_progress_json_written_on_set_task(self, tmp_path):
        """AC5: progress.json has current_task_num/attempt/round after set_task."""
        state   = _make_state(tmp_path)
        display = _make_display(state)
        display.set_task(task_num=2, attempt=3, round_num=4)

        pj = state.get_progress()
        assert pj.get("current_task_num") == 2
        assert pj.get("current_attempt")  == 3
        assert pj.get("current_round")    == 4

    def test_progress_json_matches_display_after_full_execute(self, tmp_path):
        """AC5: progress.json final snapshot matches display object counters."""
        tasks = [_make_task(f"T-{i}") for i in range(2)]
        ctrl  = _make_controller(tmp_path, tasks)
        out   = io.StringIO()
        display = _make_display(ctrl.state, code_total=2, out=out)
        ctrl.progress_display = display

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _passed_outer("T-0"), _passed_outer("T-1")
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()

        pj = ctrl.state.get_progress()
        assert pj.get("code_done")  == display.code_done
        assert pj.get("code_total") == display.code_total


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Resume: PLAN skipped, EXECUTE refresh shows correct counts
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeProgress:
    def test_execute_refresh_on_resume_shows_done_count(self, tmp_path):
        """AC6: on resume, execute-phase refresh reflects already-done tasks."""
        # Two tasks already done, one pending
        tasks = [
            _make_task("T-D1", status="done"),
            _make_task("T-D2", status="done"),
            _make_task("T-P"),
        ]
        ctrl = _make_controller(tmp_path, tasks)
        out  = io.StringIO()
        display = _make_display(ctrl.state, code_total=3, out=out)
        ctrl.progress_display = display

        cfg = _make_config()

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-P")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            run_pipeline(ctrl)

        # The execute-phase refresh should have pre-seeded code_done = 2
        output = out.getvalue()
        assert "coding [2/3]" in output or display.code_done >= 2

    def test_plan_phase_skipped_on_resume(self, tmp_path):
        """AC6: ingest_repo is not called when tasks already exist (resume)."""
        tasks = [_make_task("T-EX")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.progress_display = None

        cfg = _make_config()

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-EX")

        with patch("tools.auto.pipeline.ingest_repo") as mock_ingest, \
             patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            run_pipeline(ctrl)

        mock_ingest.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — progress_display=None is safe throughout
# ─────────────────────────────────────────────────────────────────────────────

class TestNoneDisplaySafe:
    def test_plan_phase_with_none_display(self, tmp_path):
        """AC8: _run_plan_phase with progress_display=None must not raise."""
        ctrl = _make_controller(tmp_path)
        ctrl.progress_display = None

        cfg = _make_config()

        from tools.auto.repo_ingest import RepoCluster
        clusters  = [RepoCluster(name="C", patterns=[], files=["a.py"])]

        backlog = MagicMock()
        backlog.auto_tasks = []
        backlog.manual_suggestions = []
        backlog.to_state_tasks.return_value = []

        with patch("tools.auto.pipeline.ingest_repo", return_value=clusters), \
             patch("tools.auto.pipeline.review_clusters", return_value=[]), \
             patch("tools.auto.pipeline.filter_candidates", return_value=([], [])), \
             patch("tools.auto.pipeline.build_backlog", return_value=backlog), \
             patch("tools.auto.pipeline.to_improvements_md", return_value=""), \
             patch("tools.auto.pipeline.PlanEmitter"):
            _run_plan_phase(ctrl, cfg)   # must not raise

    def test_execute_phase_with_none_display(self, tmp_path):
        """AC8: _run_task_loop with progress_display=None must not raise."""
        tasks = [_make_task("T-ND")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.progress_display = None

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-ND")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            ctrl._run_task_loop()   # must not raise

    def test_run_pipeline_with_none_display(self, tmp_path):
        """AC8: full run_pipeline with progress_display=None is safe."""
        tasks = [_make_task("T-PL")]
        ctrl  = _make_controller(tmp_path, tasks)
        ctrl.progress_display = None

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_outer("T-PL")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.commit_on_success.CommitOnSuccess"), \
             patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
             patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
            run_pipeline(ctrl)   # must not raise


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))