"""tests/test_harden30_c_plan_emission_resume.py — HARDEN-30 C.

A resume never takes a partial plan emission for a complete plan. The emitter
marks emission ``started`` then ``complete``; resume re-runs the plan phase
only when a ``started`` emission was interrupted, recovering the missing tasks
without duplicating or reopening tasks that already ran. Both the git and the
no-git paths are covered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.backlog_prioritiser import build_backlog
from tools.auto.pipeline import _run_plan_phase
from tools.auto.plan_emitter import PlanEmitter
from tools.auto.repo_ingest import RepoCluster
from tools.auto.state import StateStore, make_task

from test_auto_g1 import _FakeController, _fake_cfg, _git_init  # noqa: E402


def _make_candidates(tmp_path: Path):
    from tools.auto.architect import CandidateTask, CitedLocation

    c1 = CandidateTask(
        title="T1", instruction="first", target_files=["a.py"],
        acceptance_check="python -m pytest tests/ -q",
        cited_location=CitedLocation(file="a.py", symbol="f", line_start=1, line_end=2),
        cluster="agents",
    )
    c2 = CandidateTask(
        title="T2", instruction="second", target_files=["b.py"],
        acceptance_check="python -m pytest tests/ -q",
        cited_location=CitedLocation(file="b.py", symbol="g", line_start=1, line_end=2),
        cluster="agents",
    )
    return [c1, c2]


def _clusters() -> list[RepoCluster]:
    return [RepoCluster(name="agents", files=["a.py", "b.py"], patterns=[])]


def _run_plan_phase_with(tmp_path: Path, preexisting, emission: str, git=None):
    """Initialise state seeded with *preexisting* tasks and an emission marker,
    then return a fresh controller whose state holds that plan."""
    _git_init(tmp_path)
    ctrl = _FakeController(tmp_path)  # state initialised with goal "improve current code"
    state = ctrl.state
    if preexisting:
        state._plan = {
            "goal": "improve current code", "base_dir": str(tmp_path),
            "created_at": "x", "tasks": [dict(t) for t in preexisting],
        }
        state._save_plan()
    getattr(state, f"mark_emission_{emission}")()
    (tmp_path / "IMPROVEMENTS.md").write_text("backlog\n")
    from tools.auto.git_manager import make_git_manager
    ctrl.git = make_git_manager(tmp_path, _fake_cfg()) if git is not False else None
    return ctrl, state


def test_resume_reruns_interrupted_emission_recovers_all_tasks(tmp_path: Path) -> None:
    # Interrupted after the first upsert: plan.json holds only T1 (done),
    # IMPROVEMENTS.md lists T1+T2, marker is "started".
    t1 = make_task(id="AUTO-T1", title="T1", instruction="first",
                   target_files=["a.py"], acceptance_check="pytest", status="done",
                   commit="abc123")
    ctrl, state = _run_plan_phase_with(tmp_path, [t1], "started")

    candidates = _make_candidates(tmp_path)
    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=_clusters()),
        patch("tools.auto.pipeline.review_clusters", return_value=candidates),
        patch("tools.auto.pipeline.filter_candidates",
              return_value=(candidates, [])),
    ):
        _run_plan_phase(ctrl, _fake_cfg())

    tasks = {t["id"]: t for t in state.all_tasks()}
    assert set(tasks) == {"AUTO-T1", "AUTO-T2"}, tasks
    assert tasks["AUTO-T1"]["status"] == "done", "an already-done task stays done"
    assert tasks["AUTO-T1"].get("commit") == "abc123", "done task keeps its commit"
    assert tasks["AUTO-T2"]["status"] == "todo"
    assert state.emission_state() == "complete"


def test_resume_skips_complete_emission(tmp_path: Path) -> None:
    t1 = make_task(id="AUTO-T1", title="T1", instruction="first",
                   target_files=["a.py"], acceptance_check="pytest", status="todo")
    ctrl, state = _run_plan_phase_with(tmp_path, [t1], "complete")

    with (
        patch("tools.auto.pipeline.ingest_repo") as mi,
        patch("tools.auto.pipeline.review_clusters") as mr,
        patch("tools.auto.pipeline.filter_candidates") as mf,
    ):
        _run_plan_phase(ctrl, _fake_cfg())

    mi.assert_not_called()
    mr.assert_not_called()
    mf.assert_not_called()
    assert state.emission_state() == "complete"


def test_no_git_emission_writes_markers(tmp_path: Path) -> None:
    ctrl, state = _run_plan_phase_with(tmp_path, [], "started", git=False)
    candidates = _make_candidates(tmp_path)
    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=_clusters()),
        patch("tools.auto.pipeline.review_clusters", return_value=candidates),
        patch("tools.auto.pipeline.filter_candidates",
              return_value=(candidates, [])),
    ):
        _run_plan_phase(ctrl, _fake_cfg())

    assert state.emission_state() == "complete"
    tasks = {t["id"] for t in state.all_tasks()}
    assert tasks == {"AUTO-T1", "AUTO-T2"}, tasks


def test_emitter_writes_markers_around_upserts(tmp_path: Path) -> None:
    """PlanEmitter.emit itself transitions started -> complete."""
    _git_init(tmp_path)
    agent_dir = tmp_path / ".agent"
    state = StateStore(agent_dir)
    state.initialise("improve code", tmp_path)
    assert state.emission_state() is None
    from tools.auto.git_manager import make_git_manager
    git = make_git_manager(tmp_path, _fake_cfg())
    emitter = PlanEmitter(base_dir=tmp_path, state=state, git=git)
    emitter.emit(build_backlog(_make_candidates(tmp_path)), clusters=_clusters())
    assert state.emission_state() == "complete"
