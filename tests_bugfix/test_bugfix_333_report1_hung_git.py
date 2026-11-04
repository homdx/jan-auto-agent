"""333 (Report 1) F1: a git that hangs is a deadline that fires, and the deadline crashes nothing.

`test_333_report1_bugs.py` pins that `timeout=` is passed. `TimeoutExpired` is not an
`OSError`, so the callers that caught only that would turn a hung git into a crashed
harvest: these fire the deadline through each of them. Plus the guards around F2, F4.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.auto.executor import Executor
from tools.claimcheck import target
from tools.contest import draft, gates, harvest, workspace

ROOT = Path(__file__).resolve().parent.parent
REAL_RUN = subprocess.run


@pytest.fixture
def hung(monkeypatch):
    """Every git call's deadline fires; the list holds the deadlines they asked for."""
    deadlines = []

    def run(cmd, *a, **kw):
        if "git" not in Path(str(cmd[0])).name:
            return REAL_RUN(cmd, *a, **kw)
        deadlines.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
    monkeypatch.setattr(subprocess, "run", run)
    return deadlines


def _bounded(deadlines):
    return bool(deadlines) and all(d is not None for d in deadlines)


def _ws(path):
    return SimpleNamespace(path=path, agent="a", branch="b", base_sha="HEAD", kind="clone",
                           progress_csv=path / "runs" / "a" / "PROGRESS.csv")


def test_harvest_status_lines_is_empty_when_the_deadline_fires(tmp_path, hung):
    assert harvest._status_lines(_ws(tmp_path)) == [] and _bounded(hung)


def test_harvest_commit_worktree_reports_when_the_deadline_fires(tmp_path, hung):
    got = harvest._commit_worktree(_ws(tmp_path), "HEAD")
    assert got[0] is None and got[2] and _bounded(hung)


def test_harvest_drop_worktree_does_not_raise_when_the_deadline_fires(tmp_path, hung):
    harvest._drop_worktree(_ws(tmp_path), str(tmp_path / "gone"))
    assert _bounded(hung)


def test_draft_git_is_a_refusal_when_the_deadline_fires(tmp_path, hung):
    assert draft._git(tmp_path, "status")[0] != 0 and _bounded(hung)


@pytest.mark.parametrize("check", [True, False])
def test_workspace_git_is_a_workspace_error_when_the_deadline_fires(tmp_path, hung, check):
    with pytest.raises(workspace.WorkspaceError):
        workspace._git(tmp_path, ["status"], check=check)
    assert _bounded(hung)


def test_gates_extract_shrink_is_unknown_when_the_deadline_fires(tmp_path, hung):
    assert gates.extract_shrink(str(tmp_path), "HEAD") is None and _bounded(hung)


def test_gates_judge_worktree_survives_a_test_blob_deadline(tmp_path, monkeypatch):
    def git(*args):
        REAL_RUN(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True,
                 capture_output=True)
    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = REAL_RUN(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    git("add", "-A")
    git("commit", "-qm", "t")
    deadlines = []

    def run(cmd, *a, **kw):
        if list(cmd[:2]) == ["git", "show"]:
            deadlines.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return REAL_RUN(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    row = gates.judge_worktree("a", str(tmp_path), base, ["a.py"], want_tests=False)
    assert row["test_files"] == 0 and _bounded(deadlines)


def test_target_run_git_is_a_target_error_when_the_deadline_fires(hung):
    with pytest.raises(target.TargetError):
        target._run_git(None, "--version")
    assert _bounded(hung)


@pytest.mark.parametrize("rel", ["tools/contest/runner.py", "tools/contest/harvest.py", "tools/contest/draft.py",
                                 "tools/contest/workspace.py", "tools/contest/gates.py",
                                 "scripts/check_runbook.py", "scripts/collect_ab.py",
                                 "scripts/gate1_vote_layer2.py"])
def test_no_git_call_the_ticket_names_is_left_without_a_deadline(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(k.arg == "timeout" for k in node.keywords):
            continue
        direct = getattr(node.func, "id", "") == "run_git"
        sub = (isinstance(node.func, ast.Attribute) and node.func.attr == "run"
               and getattr(node.func.value, "id", "") == "subprocess"
               and node.args and "git" in ast.unparse(node.args[0]))
        if direct or sub:
            bad.append(node.lineno)
    assert not bad, f"{rel}: {bad}"


def _registry(tmp_path, entries):
    root = tmp_path / "ws"
    root.mkdir()
    ex = Executor.__new__(Executor)
    ex._workspace_root = root
    ex._registry_path().write_text(json.dumps(entries))
    return ex._read_workspace_registry()


@pytest.mark.parametrize("path", [r"C:\repo\.agent\workspace\T1", "d:/x/T2", r"\\srv\share\ws\T1"])
def test_f2_a_windows_absolute_path_is_read(tmp_path, path):
    assert path in _registry(tmp_path, [path])


@pytest.mark.parametrize("name", ["T1", "C:T1"])
def test_f2_guard_a_plain_name_is_still_the_old_format(tmp_path, name):
    assert _registry(tmp_path, [name]) == set()


def test_f4_guard_a_stale_lock_is_broken(tmp_path):
    path = tmp_path / "lock"
    path.mkdir()
    (path / "owner").write_text("stale")
    target._Lock(path, 1)._break("stale")
    assert not list(tmp_path.iterdir())


def test_f4_guard_a_live_lock_taken_in_between_is_put_back(tmp_path):
    path = tmp_path / "lock"
    path.mkdir()
    (path / "owner").write_text("live")
    target._Lock(path, 1)._break("stale")
    assert (path / "owner").read_text() == "live" and len(list(tmp_path.iterdir())) == 1


def test_f4_a_third_waiter_on_the_path_keeps_both_live_locks(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    path.mkdir()
    live, third = f"{os.getpid()}-a", f"{os.getpid()}-c"
    (path / "owner").write_text(live)
    real = Path.rename

    def rename(self, dst):
        if Path(dst) == path:             # the third waiter takes the path, so the put-back fails
            path.mkdir()
            (path / "owner").write_text(third)
        return real(self, dst)
    monkeypatch.setattr(Path, "rename", rename)
    target._Lock(path, 1)._break("stale")
    assert sorted(p.read_text() for p in tmp_path.rglob("owner")) == sorted([live, third])
