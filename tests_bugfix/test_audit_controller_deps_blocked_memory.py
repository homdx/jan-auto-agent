"""tests_bugfix/test_audit_controller_deps_blocked_memory.py — AUDIT-4, AUDIT-7, AUDIT-8.

AUDIT-4: a task listed before its own dependency was marked BLOCKED on sight
and — since a BLOCKED task is never pending again — never ran, even after the
dependency completed later in the same run.

AUDIT-7: a creative task CommitOnSuccess left BLOCKED (nothing staged behind a
no-op acceptance check) was still counted done by the controller.

AUDIT-8: synopsis.md / the story bible were rewritten after the task's commit
and left dirty, so the NEXT task's stage_all() committed them under its id.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.auto.commit_on_success import (
    COMMIT_NOTHING_TO_COMMIT, CommitOnSuccess, CommitResult,
)
from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import STATUS_BLOCKED, STATUS_DONE, StateStore, make_task

_USER, _EMAIL = "auto-agent-test", "auto-agent-test@localhost"


def _git(base: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=base, check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    base.mkdir()
    _git(base, "init", "-q")
    _git(base, "config", "user.name", _USER)
    _git(base, "config", "user.email", _EMAIL)
    (base / "README.md").write_text("# repo\n")
    (base / ".gitignore").write_text(".agent/\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "init")
    return base


def _git_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": _USER, "git_email": _EMAIL}
    return cfg


def _controller(tmp_path: Path, tasks: list[dict]) -> AutoController:
    import time
    base = _repo(tmp_path)
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits = RunLimits()
    st = StateStore(base / ".agent")
    st.initialise("test goal", base)
    for t in tasks:
        st.upsert_task(t)
    ctrl.state = st
    ctrl.git = make_git_manager(base, _git_config())
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0)
    return ctrl


def _passed(task_id: str) -> OuterLoopResult:
    return OuterLoopResult(
        task_id=task_id, passed=True, rounds_used=1, exhausted=False,
        feedback_files=[],
        inner_results=[SimpleNamespace(attempts_used=1, last_feedback="")],
    )


def _task(tid: str, deps=None) -> dict:
    return make_task(id=tid, title=f"Task {tid}", instruction="do it",
                     target_files=[f"{tid}.py"], dependencies=deps or [])


def _run(ctrl, outer):
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
        return ctrl._run_task_loop()


# ── AUDIT-4 ─────────────────────────────────────────────────────────────────

def test_task_before_its_dependency_runs_after_it(tmp_path):
    ctrl = _controller(tmp_path, [_task("B", ["A"]), _task("A")])
    ran: list[str] = []

    def run_task(t, base_dir):
        ran.append(t["id"])
        (Path(base_dir) / f"{t['id']}.py").write_text("x = 1\n")
        return _passed(t["id"])

    outer = MagicMock()
    outer.run_task.side_effect = run_task
    _reason, done = _run(ctrl, outer)

    assert ran == ["A", "B"]
    assert done == 2
    assert ctrl.state.get_task("B")["status"] == STATUS_DONE


def test_dependency_cycle_is_blocked_not_looped(tmp_path):
    ctrl = _controller(tmp_path, [_task("A", ["B"]), _task("B", ["A"])])
    outer = MagicMock()
    outer.run_task.side_effect = AssertionError("nothing in a cycle can run")
    _reason, done = _run(ctrl, outer)

    assert done == 0
    assert ctrl.state.get_task("A")["status"] == STATUS_BLOCKED
    assert ctrl.state.get_task("B")["status"] == STATUS_BLOCKED


def test_missing_dependency_still_blocks(tmp_path):
    ctrl = _controller(tmp_path, [_task("A", ["NOPE"])])
    outer = MagicMock()
    _reason, done = _run(ctrl, outer)
    assert done == 0
    assert ctrl.state.get_task("A")["status"] == STATUS_BLOCKED
    outer.run_task.assert_not_called()


# ── AUDIT-7 ─────────────────────────────────────────────────────────────────

def test_blocked_by_commit_helper_is_not_counted_done(tmp_path):
    ctrl = _controller(tmp_path, [_task("C1")])
    outer = MagicMock()
    outer.run_task.return_value = _passed("C1")

    def blocked_outcome(helper, task, result):
        ctrl.state.set_task_status(task["id"], STATUS_BLOCKED)
        return CommitResult(None, COMMIT_NOTHING_TO_COMMIT)

    with patch.object(AutoController, "_commit_outcome",
                      side_effect=lambda h, t, r: blocked_outcome(h, t, r)), \
         patch.object(AutoController, "_check_regressions") as regress:
        _reason, done = _run(ctrl, outer)

    assert done == 0
    assert ctrl.state.get_task("C1")["status"] == STATUS_BLOCKED
    regress.assert_not_called()


def test_plain_nothing_to_commit_still_counts_done(tmp_path):
    ctrl = _controller(tmp_path, [_task("C2")])
    outer = MagicMock()
    outer.run_task.return_value = _passed("C2")
    _reason, done = _run(ctrl, outer)
    assert done == 1
    assert ctrl.state.get_task("C2")["status"] == STATUS_DONE


# ── AUDIT-8 ─────────────────────────────────────────────────────────────────

class _FakeSummary:
    def __init__(self, base: Path):
        self._synopsis_path = base / "synopsis.md"

    def update(self, chapter_file, base_dir=None):
        with open(self._synopsis_path, "a", encoding="utf-8") as fh:
            fh.write(f"- {chapter_file} happened\n")


def test_synopsis_update_is_committed_under_its_own_task(tmp_path):
    base = _repo(tmp_path)
    st = StateStore(base / ".agent")
    st.initialise("novel", base)
    st.upsert_task(_task("CH1"))
    st.upsert_task(_task("CH2"))
    gm = make_git_manager(base, _git_config())
    cos = CommitOnSuccess(gm, st, summary_memory=_FakeSummary(base),
                          task_mode="creative", base_dir=base)

    (base / "CH1.py").write_text("chapter one\n")
    first = cos.commit_result({"id": "CH1", "title": "one", "target_files": ["CH1.py"]})
    assert first.committed
    assert _git(base, "status", "--porcelain").strip() == "", \
        "synopsis.md was left dirty after the task's commit"

    (base / "CH2.py").write_text("chapter two\n")
    second = cos.commit_result({"id": "CH2", "title": "two", "target_files": ["CH2.py"]})
    files_in_ch2 = _git(base, "show", "--name-only", "--format=", second.sha).split()
    assert files_in_ch2 == ["CH2.py"], files_in_ch2
    log = _git(base, "log", "--format=%s", "--", "synopsis.md")
    assert "auto(CH1)" in log


def test_commit_paths_ignores_unchanged_and_outside_paths(tmp_path):
    base = _repo(tmp_path)
    gm = make_git_manager(base, _git_config())
    assert gm.commit_paths("m", [base / "README.md"]) is None
    assert gm.commit_paths("m", [tmp_path / "elsewhere.md"]) is None
    (base / "notes.md").write_text("n\n")
    (base / "other.md").write_text("o\n")
    sha = gm.commit_paths("m", ["notes.md"])
    assert sha
    assert _git(base, "show", "--name-only", "--format=", sha).split() == ["notes.md"]
    assert "other.md" in _git(base, "status", "--porcelain")
