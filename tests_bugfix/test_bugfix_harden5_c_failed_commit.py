"""tests_bugfix/test_bugfix_harden5_c_failed_commit.py

Pinned regression guard for HARDEN-5 C: a task whose commit failed was still
marked DONE.

OuterLoop.run_task sets a task DONE the moment it passes its acceptance check,
before any commit happens, and the controller's git_error branch did not set it
back. A DONE task whose commit failed was skipped by a resume (its work lived
only in a patch that nothing re-applies) and read as done by the dependency
check, so a dependent task ran and committed those files under its own id — the
sweep the patch was meant to prevent.

The test drives the REAL OuterLoop with a stub inner loop: the status write
OuterLoop makes is the one under test, so a stand-in that skipped it could not
have caught this.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import GitError, make_git_manager
from tools.auto.outer_loop import OuterLoop, OuterLoopResult
from tools.auto.state import StateStore, STATUS_BLOCKED, STATUS_DONE, STATUS_TODO, make_task

_AGENT_USER = "auto-agent-test"
_AGENT_EMAIL = "auto-agent-test@localhost"


def _cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": _AGENT_USER, "git_email": _AGENT_EMAIL}
    return cfg


class _PassingInnerLoop:
    """A stub inner loop that always passes: it writes the task's file and
    reports one approved attempt.

    ``OuterLoop.run_task`` inspects its signature for ``deadline`` and passes
    anything it finds, so the real OuterLoop drives this without a real coder.

    ``seen`` is the record of which tasks actually ran: a blocked dependent
    never reaches this class.
    """

    max_task_seconds = 0  # no wall-clock budget: the run must not sleep

    def __init__(self) -> None:
        self.seen: list[str] = []

    def run_task(self, task, base_dir, **kwargs) -> OuterLoopResult:
        self.seen.append(task["id"])
        (Path(base_dir) / task["target_files"][0]).write_text(
            f"# written by {task['id']}\n", encoding="utf-8")
        return OuterLoopResult(
            task_id=task["id"], passed=True, rounds_used=1, exhausted=False,
            feedback_files=[],
            inner_results=[SimpleNamespace(attempts_used=1, last_feedback="")])


def _git(cwd, *args):
    proc = subprocess.run(["git", "-c", "user.name=" + _AGENT_USER,
                           "-c", "user.email=" + _AGENT_EMAIL, *args],
                          cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _repo(base: Path) -> None:
    """A one-commit repo with .agent/ ignored BEFORE the run.

    ``discard_working_changes`` skips ``git clean -fd`` unless .agent/ is
    covered by .gitignore, so an untracked edit would survive a discard and the
    test could not tell whether the tree was cleaned.
    """
    base.mkdir()
    (base / ".gitignore").write_text(".agent/\n")
    (base / "README.md").write_text("# repo\n")
    _git(base, "init", "-q")
    _git(base, "add", "-A")
    _git(base, "commit", "-qm", "init")


def _controller(base: Path, tasks) -> tuple[AutoController, StateStore]:
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "t"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits = RunLimits()
    st = StateStore(ctrl.agent_dir)
    st.initialise("t", base)
    for t in tasks:
        st.upsert_task(t)
    ctrl.state = st
    ctrl.git = make_git_manager(base, _cfg())
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = MagicMock(
        promoted=False, triggered=False, new_prompt_score=0.0, reason="")
    return ctrl, st


def _outer(st: StateStore) -> tuple[OuterLoop, _PassingInnerLoop]:
    inner = _PassingInnerLoop()
    return OuterLoop(inner, st, max_rounds=1), inner


# ── the failed commit ────────────────────────────────────────────────────────


def test_c_failed_commit_is_not_done_and_leaves_the_tree_clean(tmp_path):
    """A task whose commit failed goes back to todo, its patch is on disk, and
    its work is out of the tree."""
    base = tmp_path / "repo"
    _repo(base)
    ctrl, st = _controller(base, [
        make_task(id="B2", title="t", instruction="i", target_files=["work.py"]),
    ])

    outer, _inner = _outer(st)
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
         patch.object(ctrl.git, "commit_task",
                      side_effect=GitError("simulated git failure")):
        reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 0
    # not DONE: the status write OuterLoop made is undone, so a resume and the
    # dependency check both read this task as not done
    assert st.get_task("B2")["status"] == STATUS_TODO
    patch_file = ctrl.agent_dir / "uncommitted-B2.patch"
    assert patch_file.exists()
    assert "work.py" in patch_file.read_text(encoding="utf-8")
    # the patch is the record, so the tree went back to HEAD
    assert _git(base, "status", "--porcelain") == ""
    assert not (base / "work.py").exists()
    # the patch survived the discard: it is gitignored, not swept
    assert patch_file.exists()
    # and the run did not stop on it
    assert reason is None


def test_c_failed_commit_does_not_reach_a_blocked_dependent(tmp_path):
    """The sweep: a dependent task must not run on top of a task whose commit
    failed, so it cannot commit those files under its own id."""
    base = tmp_path / "repo"
    _repo(base)
    ctrl, st = _controller(base, [
        make_task(id="B2", title="t", instruction="i", target_files=["work.py"]),
        make_task(id="C1", title="t", instruction="i",
                  target_files=["work2.py"], dependencies=["B2"]),
    ])

    outer, inner = _outer(st)
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
         patch.object(ctrl.git, "commit_task",
                      side_effect=GitError("simulated git failure")):
        reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 0
    assert st.get_task("B2")["status"] == STATUS_TODO
    # C1 read B2 as not done and was held, so it never ran
    assert st.get_task("C1")["status"] == STATUS_BLOCKED
    assert inner.seen == ["B2"]
    assert not (base / "work2.py").exists()
    assert _git(base, "status", "--porcelain") == ""
    assert reason is None


def test_c_a_dependent_run_after_a_failed_commit_commits_nothing_of_it(tmp_path):
    """Even if a dependent task did run, the sweep is what is prevented: the
    failed task's file must not appear in the dependent's commit."""
    base = tmp_path / "repo"
    _repo(base)
    ctrl, st = _controller(base, [
        make_task(id="B2", title="t", instruction="i", target_files=["work.py"]),
        make_task(id="C1", title="t", instruction="i", target_files=["work2.py"]),
    ])

    outer, _inner = _outer(st)
    # commit_task stages AND commits, so the second call must go through it
    # rather than a bare `git commit` (that one would have nothing staged).
    real_commit_task = ctrl.git.commit_task
    calls = {"n": 0}

    def _commit(*args, **kwargs):
        # the first commit fails, the second succeeds
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitError("simulated git failure")
        return real_commit_task(*args, **kwargs)

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
         patch.object(ctrl.git, "commit_task", side_effect=_commit):
        reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 1
    assert st.get_task("B2")["status"] == STATUS_TODO
    assert st.get_task("C1")["status"] == STATUS_DONE
    committed = _git(base, "show", "--name-only", "--format=", "HEAD")
    assert "work2.py" in committed
    assert "work.py" not in committed
    assert _git(base, "status", "--porcelain") == ""


# ── the patch is the record only when it was written ─────────────────────────


def test_c_no_patch_keeps_the_work_in_the_tree(tmp_path):
    """When the patch could not be written, the tree is the only copy and must
    not be discarded."""
    base = tmp_path / "repo"
    _repo(base)
    ctrl, st = _controller(base, [
        make_task(id="B2", title="t", instruction="i", target_files=["work.py"]),
    ])

    outer, _inner = _outer(st)
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
         patch.object(ctrl.git, "commit_task",
                      side_effect=GitError("simulated git failure")), \
         patch.object(ctrl, "_save_uncommitted_patch", return_value=""):
        reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 0
    assert st.get_task("B2")["status"] == STATUS_TODO
    assert not (ctrl.agent_dir / "uncommitted-B2.patch").exists()
    # nothing was saved as a patch, so the edit is still in the tree
    assert (base / "work.py").exists()
    assert reason is None


def test_c_a_normal_commit_is_unchanged(tmp_path):
    """A commit that succeeds is still a done task, on its own terms."""
    base = tmp_path / "repo"
    _repo(base)
    ctrl, st = _controller(base, [
        make_task(id="B2", title="t", instruction="i", target_files=["work.py"]),
    ])

    outer, _inner = _outer(st)
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
        reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 1
    assert st.get_task("B2")["status"] == STATUS_DONE
    assert (base / "work.py").exists()
    assert _git(base, "status", "--porcelain") == ""
    assert reason is None
