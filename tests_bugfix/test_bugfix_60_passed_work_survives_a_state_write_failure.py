"""tests_bugfix/test_bugfix_60_passed_work_survives_a_state_write_failure.py — AUTO-T60 follow-up.

When the DONE write fails after the inner loop accepted the work, `OuterLoop.run_task` reported a plain
exhaustion; the controller then reset the tree and the accepted edit was gone, with no patch and no commit.
The result now says `work_passed`, and the controller saves the edit as `.agent/uncommitted-<id>.patch`
before it cleans the tree — and only cleans it once the patch is on disk.
"""

from __future__ import annotations

import contextlib
import io
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.auto.outer_loop import OuterLoop
from tools.auto.state import STATUS_DONE

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_audit_controller_deps_blocked_memory.py")))


def _run(tmp_path, *, fail_done: bool = False, fail_counter: bool = False,
         fail_patch: bool = False, tasks=("T1",)):
    """Run *tasks* through the real controller, outer loop and git; T1's recording fails as asked."""
    ctrl = HELPERS["_controller"](tmp_path, [HELPERS["_task"](t) for t in tasks])
    for t in tasks:
        (ctrl.base_dir / f"{t}.py").write_text("answer = 0\n")
    HELPERS["_git"](ctrl.base_dir, "add", *[f"{t}.py" for t in tasks])
    HELPERS["_git"](ctrl.base_dir, "commit", "-qm", "baseline file")
    target = ctrl.base_dir / "T1.py"
    real_set = ctrl.state.set_task_status

    def maybe_fail(task_id, status, **kwargs):
        if fail_done and task_id == "T1" and status == STATUS_DONE:
            raise OSError("plan.json write failure after acceptance passed")
        return real_set(task_id, status, **kwargs)

    ctrl.state.set_task_status = maybe_fail
    if fail_counter:
        def no_count(*a, **k):
            raise OSError("counter write failure after acceptance")
        ctrl.state.increment_task_counters = no_count

    class Inner:
        max_task_seconds = 0

        def run_task(self, task, base_dir, **kwargs):
            (Path(base_dir) / f"{task['id']}.py").write_text("answer = 42\n")
            return SimpleNamespace(task_id=task["id"], passed=True, attempts_used=1, last_feedback="ok")

    if fail_patch:
        def deny(*a, **k):
            raise OSError("patch write failure")
        save = ctrl._save_uncommitted_patch

        def failing_save(*a, **k):
            with patch("tools.auto.controller.atomic_write_text", side_effect=deny):
                return save(*a, **k)
        ctrl._save_uncommitted_patch = failing_save

    outer = OuterLoop(Inner(), ctrl.state, max_rounds=1)
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=outer), \
            contextlib.redirect_stdout(io.StringIO()):
        ctrl.stopped = ctrl._run_task_loop()
    return ctrl, target


def test_the_result_names_accepted_work_whose_record_failed(tmp_path) -> None:
    class Inner:
        max_task_seconds = 0

        def run_task(self, task, base_dir, **kwargs):
            return SimpleNamespace(task_id="T1", passed=True, attempts_used=1, last_feedback="ok")

    ctrl = HELPERS["_controller"](tmp_path, [HELPERS["_task"]("T1")])
    real_set = ctrl.state.set_task_status

    def fail_done(task_id, status, **kw):
        if status == STATUS_DONE:
            raise OSError("disk full")
        return real_set(task_id, status, **kw)

    ctrl.state.set_task_status = fail_done
    result = OuterLoop(Inner(), ctrl.state, max_rounds=1).run_task({"id": "T1"}, ctrl.base_dir)
    assert result.work_passed is True and result.passed is False
    assert "not recorded" in result.summary()


def test_the_accepted_edit_is_saved_as_a_patch_before_the_tree_is_reset(tmp_path) -> None:
    ctrl, target = _run(tmp_path, fail_done=True)
    patches = list(ctrl.agent_dir.rglob("uncommitted-*.patch"))
    assert patches and "+answer = 42" in patches[0].read_text()
    # the tree is back at HEAD, so no later task's commit can sweep the edit in
    assert target.read_text() == "answer = 0\n"
    assert HELPERS["_git"](ctrl.base_dir, "status", "--porcelain").strip() == ""


def test_no_exhaustion_ticket_is_filed_for_work_that_passed(tmp_path) -> None:
    ctrl, _ = _run(tmp_path, fail_done=True)
    log = "".join(p.read_text() for p in ctrl.agent_dir.rglob("run.log"))
    assert "accepted but NOT recorded" in log and "exhausted" not in log


def test_a_run_whose_state_write_works_is_unchanged(tmp_path) -> None:
    ctrl, target = _run(tmp_path, fail_done=False)
    assert not list(ctrl.agent_dir.rglob("uncommitted-*.patch"))
    assert "answer = 42" in HELPERS["_git"](ctrl.base_dir, "show", "HEAD:T1.py")


def test_a_counter_write_failure_after_acceptance_keeps_the_work(tmp_path) -> None:
    """The attempt counter is written before the DONE write; its failure is no exhaustion either."""
    ctrl, target = _run(tmp_path, fail_counter=True)
    patches = list(ctrl.agent_dir.rglob("uncommitted-*.patch"))
    assert patches and "+answer = 42" in patches[0].read_text()
    assert target.read_text() == "answer = 0\n"


def test_work_that_could_not_be_saved_stops_the_run_and_stays_in_the_tree(tmp_path) -> None:
    """With no patch, the edit is the only copy: T2 must not start and commit it under its own id."""
    ctrl, target = _run(tmp_path, fail_done=True, fail_patch=True, tasks=("T1", "T2"))
    reason, tasks_done = ctrl.stopped
    assert reason == "unsaved_work" and tasks_done == 0
    assert target.read_text() == "answer = 42\n"                    # nothing was reset
    log = HELPERS["_git"](ctrl.base_dir, "log", "--format=%s")
    assert "auto(T2)" not in log and "auto(T1)" not in log
    assert ctrl.state.get_task("T2")["status"] != STATUS_DONE
    assert ctrl.state.get_task("T1")["status"] != STATUS_DONE


def test_a_saved_patch_lets_the_next_task_run_over_a_clean_tree(tmp_path) -> None:
    ctrl, target = _run(tmp_path, fail_done=True, tasks=("T1", "T2"))
    reason, _ = ctrl.stopped
    assert reason is None
    assert target.read_text() == "answer = 0\n"
    changed = HELPERS["_git"](ctrl.base_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
    assert "T1.py" not in changed
