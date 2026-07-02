"""tests/test_auto_g1.py — AUTO-G0 / AUTO-G1: Pipeline skeleton + PLAN phase wiring.

Covers the story ACs:

AUTO-G0
  * ``controller.run()`` delegates to ``pipeline.run_pipeline(self)``; the
    existing A4 cap/resume tests still pass unchanged (verified by running
    test_auto_4.py separately; this file ensures the import is wired).

AUTO-G1
  AC1 — Fresh ``--auto`` run produces a non-empty ``plan.json`` and a
         committed ``IMPROVEMENTS.md``.
  AC2 — Re-running skips the PLAN phase (resume); ``plan.json`` is not
         rebuilt when tasks already exist in state.
  AC3 — Check-less tasks land in the "Manual suggestions" section and are
         excluded from the auto-run task list (validated via the backlog
         that flows into plan_emitter).

Test strategy
-------------
All LLM calls (repo_ingest's walk is real, but architect/gate1 LLM I/O) are
patched with lightweight fakes so the suite is fully offline.  A real temporary
git repo is used so the commit path is exercised end-to-end.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.backlog_prioritiser import build_backlog
from tools.auto.pipeline import _run_plan_phase, run_pipeline
from tools.auto.repo_ingest import RepoCluster
from tools.auto.state import StateStore


# ─────────────────────────────────────────────────────────────────────────────
# Shared factories
# ─────────────────────────────────────────────────────────────────────────────


def _git_init(path: Path) -> None:
    """Initialise a bare git repo with an initial empty commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agent@test"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agent"],
        cwd=path, check=True, capture_output=True,
    )
    # Seed at least one file so HEAD exists
    dummy = path / "README.md"
    dummy.write_text("# test repo\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )


def _make_candidate(
    *,
    title: str = "Fix something",
    instruction: str = "Improve the code.",
    file: str = "README.md",
    symbol: str = "main",
    acceptance_check: str = "python -m pytest tests/ -q",
) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction=instruction,
        target_files=[file],
        acceptance_check=acceptance_check,
        cited_location=CitedLocation(
            file=file, symbol=symbol, line_start=1, line_end=5
        ),
        cluster="agents",
    )


def _make_clusters() -> list[RepoCluster]:
    return [RepoCluster(name="agents", files=["README.md"], patterns=[])]


def _fake_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {}
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Minimal fake controller for unit tests
# ─────────────────────────────────────────────────────────────────────────────


class _FakeController:
    """Thin stand-in for AutoController — only the fields pipeline reads."""

    def __init__(self, tmp_path: Path) -> None:
        self.base_dir = tmp_path
        self.config_path = str(tmp_path / "agents.ini")
        self.goal = "improve current code"

        agent_dir = tmp_path / ".agent"
        self.state = StateStore(agent_dir)
        self.state.initialise(self.goal, tmp_path)

        self.run_trace = None
        self.progress_display = None
        self.git = None

    def _run_task_loop(self, **kwargs):
        """Stand-in execution loop — returns immediately (no tasks to run)."""
        return None, 0


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G0 — controller.run() delegates to pipeline.run_pipeline
# ─────────────────────────────────────────────────────────────────────────────


class TestG0Delegation:
    """Verify that pipeline.run_pipeline is the orchestration entry point."""

    def test_run_pipeline_is_importable(self) -> None:
        from tools.auto.pipeline import run_pipeline  # noqa: F401 — import check

    def test_run_pipeline_calls_plan_phase_and_task_loop(
        self, tmp_path: Path
    ) -> None:
        """run_pipeline calls _run_plan_phase then controller._run_task_loop."""
        ctrl = _FakeController(tmp_path)

        plan_phase_called = []
        task_loop_called = []

        def fake_plan_phase(c, cfg):
            plan_phase_called.append(True)

        def fake_task_loop(**kwargs):
            task_loop_called.append(True)
            return None, 0

        ctrl._run_task_loop = fake_task_loop

        with patch("tools.auto.pipeline._run_plan_phase", fake_plan_phase):
            stop, done = run_pipeline(ctrl)

        assert plan_phase_called, "_run_plan_phase was not called"
        assert task_loop_called, "_run_task_loop was not called"
        assert stop is None
        assert done == 0

    def test_run_pipeline_returns_task_loop_result(self, tmp_path: Path) -> None:
        """run_pipeline propagates whatever (stop_reason, tasks_done) the loop returns."""
        ctrl = _FakeController(tmp_path)
        ctrl._run_task_loop = lambda **kwargs: ("task_cap", 3)

        with patch("tools.auto.pipeline._run_plan_phase", lambda c, cfg: None):
            stop, done = run_pipeline(ctrl)

        assert stop == "task_cap"
        assert done == 3


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G1 AC1 — Fresh run: plan.json populated + IMPROVEMENTS.md committed
# ─────────────────────────────────────────────────────────────────────────────


class TestG1FreshRun:
    """AC1: fresh --auto run produces plan.json + committed IMPROVEMENTS.md."""

    def _run_plan_phase_with_fakes(
        self,
        tmp_path: Path,
        candidates: list[CandidateTask],
    ) -> _FakeController:
        """Helper: run _run_plan_phase with patched LLM calls + real git."""
        _git_init(tmp_path)

        ctrl = _FakeController(tmp_path)
        cfg = _fake_cfg()
        clusters = _make_clusters()

        from tools.auto.git_manager import make_git_manager

        ctrl.git = make_git_manager(tmp_path, cfg)

        with (
            patch("tools.auto.pipeline.ingest_repo", return_value=clusters),
            patch("tools.auto.pipeline.review_clusters", return_value=candidates),
            patch(
                "tools.auto.pipeline.filter_candidates",
                return_value=(candidates, []),
            ),
        ):
            _run_plan_phase(ctrl, cfg)

        return ctrl

    def test_plan_json_non_empty_after_fresh_run(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        ctrl = self._run_plan_with_real_candidates(tmp_path, candidates)

        tasks = ctrl.state.all_tasks()
        assert len(tasks) >= 1, "plan.json must have at least one task after fresh run"

    def test_improvements_md_written(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        self._run_plan_with_real_candidates(tmp_path, candidates)

        md_path = tmp_path / "IMPROVEMENTS.md"
        assert md_path.exists(), "IMPROVEMENTS.md must be written to repo root"
        assert md_path.read_text(encoding="utf-8").strip(), "IMPROVEMENTS.md must not be empty"

    def test_improvements_md_committed(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        self._run_plan_with_real_candidates(tmp_path, candidates)

        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        commits = log.stdout.strip().splitlines()
        # There should be at least 2 commits: the seed "init" + plan commit
        assert len(commits) >= 2, "Plan commit was not created"

    def test_commit_message_contains_plan(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        self._run_plan_with_real_candidates(tmp_path, candidates)

        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert "plan" in log.stdout.lower(), (
            f"Latest commit message should mention 'plan': {log.stdout}"
        )

    # ── shared helper ─────────────────────────────────────────────────────────

    def _run_plan_with_real_candidates(
        self, tmp_path: Path, candidates: list[CandidateTask]
    ) -> _FakeController:
        """Run _run_plan_phase patching LLM but using real git + state + emitter."""
        _git_init(tmp_path)

        ctrl = _FakeController(tmp_path)
        cfg = _fake_cfg()
        clusters = _make_clusters()

        from tools.auto.git_manager import make_git_manager

        ctrl.git = make_git_manager(tmp_path, cfg)

        with (
            patch("tools.auto.pipeline.ingest_repo", return_value=clusters),
            patch("tools.auto.pipeline.review_clusters", return_value=candidates),
            patch(
                "tools.auto.pipeline.filter_candidates",
                return_value=(candidates, []),
            ),
        ):
            _run_plan_phase(ctrl, cfg)

        return ctrl


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G1 AC2 — Resume: plan phase skipped when tasks exist
# ─────────────────────────────────────────────────────────────────────────────


class TestG1Resume:
    """AC2: re-running skips the PLAN phase; plan.json is not rebuilt."""

    def test_plan_phase_skipped_when_tasks_exist(self, tmp_path: Path) -> None:
        """_run_plan_phase does nothing if state already has tasks."""
        from tools.auto.state import make_task

        ctrl = _FakeController(tmp_path)
        ctrl.state.upsert_task(
            make_task(
                id="AUTO-T1",
                title="Existing task",
                instruction="already there",
                target_files=["README.md"],
            )
        )

        # Patch the pipeline modules — they must NOT be called on resume
        with (
            patch("tools.auto.pipeline.ingest_repo") as mock_ingest,
            patch("tools.auto.pipeline.review_clusters") as mock_review,
            patch("tools.auto.pipeline.filter_candidates") as mock_filter,
        ):
            _run_plan_phase(ctrl, _fake_cfg())

        mock_ingest.assert_not_called()
        mock_review.assert_not_called()
        mock_filter.assert_not_called()

    def test_existing_tasks_preserved_after_resume_plan_phase(
        self, tmp_path: Path
    ) -> None:
        """Tasks in state are untouched when plan phase is skipped."""
        from tools.auto.state import make_task

        ctrl = _FakeController(tmp_path)
        task = make_task(
            id="AUTO-T1",
            title="Keep me",
            instruction="do not overwrite",
            target_files=["README.md"],
        )
        ctrl.state.upsert_task(task)

        with patch("tools.auto.pipeline.ingest_repo"):
            _run_plan_phase(ctrl, _fake_cfg())

        tasks = ctrl.state.all_tasks()
        assert any(t["id"] == "AUTO-T1" for t in tasks), (
            "Existing task was lost after resume plan phase"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G1 AC3 — Check-less tasks → manual suggestions only
# ─────────────────────────────────────────────────────────────────────────────


class TestG1ChecklessTasks:
    """AC3: tasks without runnable acceptance checks are excluded from auto-run."""

    def test_checkless_candidate_excluded_from_auto_tasks(
        self, tmp_path: Path
    ) -> None:
        """A candidate with no acceptance_check lands in manual suggestions."""
        auto_cand = _make_candidate(
            title="Auto task", acceptance_check="python -m pytest tests/ -q"
        )
        manual_cand = _make_candidate(
            title="Manual task",
            acceptance_check="",  # no runnable check → manual suggestion
        )
        candidates = [auto_cand, manual_cand]

        backlog = build_backlog(candidates)

        auto_titles = {t.title for t in backlog.auto_tasks}
        manual_titles = {c.title for c in backlog.manual_suggestions}

        assert "Auto task" in auto_titles, "Runnable task must be in auto_tasks"
        assert "Manual task" in manual_titles, (
            "Check-less task must be in manual_suggestions, not auto_tasks"
        )
        assert "Manual task" not in auto_titles, (
            "Check-less task must NOT appear in auto_tasks"
        )

    def test_na_acceptance_check_is_also_excluded(self, tmp_path: Path) -> None:
        """LLM-hedge strings like 'N/A' are treated as non-runnable."""
        cand = _make_candidate(title="N/A task", acceptance_check="N/A")
        backlog = build_backlog([cand])

        assert len(backlog.auto_tasks) == 0, "N/A acceptance check must not be auto"
        assert len(backlog.manual_suggestions) == 1

    def test_manual_review_string_is_excluded(self) -> None:
        # "manual review" (without extra words) is a recognised LLM hedge
        cand = _make_candidate(
            title="Manual review task", acceptance_check="manual review"
        )
        backlog = build_backlog([cand])
        assert len(backlog.auto_tasks) == 0
        assert len(backlog.manual_suggestions) == 1


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G1 — Git-unavailable fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestG1GitUnavailable:
    """When git is None, plan phase still writes files and upserts tasks."""

    def test_tasks_upserted_without_git(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        clusters = _make_clusters()
        cfg = _fake_cfg()

        ctrl = _FakeController(tmp_path)
        # ctrl.git remains None

        with (
            patch("tools.auto.pipeline.ingest_repo", return_value=clusters),
            patch("tools.auto.pipeline.review_clusters", return_value=candidates),
            patch(
                "tools.auto.pipeline.filter_candidates",
                return_value=(candidates, []),
            ),
        ):
            _run_plan_phase(ctrl, cfg)

        tasks = ctrl.state.all_tasks()
        assert len(tasks) >= 1, "Tasks must be upserted even without git"

    def test_improvements_md_written_without_git(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        clusters = _make_clusters()
        cfg = _fake_cfg()

        ctrl = _FakeController(tmp_path)

        with (
            patch("tools.auto.pipeline.ingest_repo", return_value=clusters),
            patch("tools.auto.pipeline.review_clusters", return_value=candidates),
            patch(
                "tools.auto.pipeline.filter_candidates",
                return_value=(candidates, []),
            ),
        ):
            _run_plan_phase(ctrl, cfg)

        md_path = tmp_path / "IMPROVEMENTS.md"
        assert md_path.exists(), "IMPROVEMENTS.md must be written even without git"


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-G1 — run_trace integration (optional; no-op when run_trace is None)
# ─────────────────────────────────────────────────────────────────────────────


class TestG1RunTrace:
    """Plan phase calls run_trace.log_phase when a tracer is present."""

    def test_run_trace_log_phase_called(self, tmp_path: Path) -> None:
        candidates = [_make_candidate()]
        clusters = _make_clusters()
        cfg = _fake_cfg()

        ctrl = _FakeController(tmp_path)
        ctrl.run_trace = MagicMock()

        with (
            patch("tools.auto.pipeline.ingest_repo", return_value=clusters),
            patch("tools.auto.pipeline.review_clusters", return_value=candidates),
            patch(
                "tools.auto.pipeline.filter_candidates",
                return_value=(candidates, []),
            ),
        ):
            _run_plan_phase(ctrl, cfg)

        # log_phase must have been called at least for "done"
        calls = [str(c) for c in ctrl.run_trace.log_phase.call_args_list]
        assert any("done" in c for c in calls), (
            f"run_trace.log_phase('plan', 'done') was not called; calls={calls}"
        )

    def test_resume_logs_phase_skipped(self, tmp_path: Path) -> None:
        from tools.auto.state import make_task

        ctrl = _FakeController(tmp_path)
        ctrl.state.upsert_task(
            make_task(
                id="AUTO-T1", title="t", instruction="i", target_files=["f.py"]
            )
        )
        ctrl.run_trace = MagicMock()

        _run_plan_phase(ctrl, _fake_cfg())

        calls = [str(c) for c in ctrl.run_trace.log_phase.call_args_list]
        assert any("skipped" in c for c in calls), (
            f"run_trace.log_phase('plan', 'skipped') was not called; calls={calls}"
        )
