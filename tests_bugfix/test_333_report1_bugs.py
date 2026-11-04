"""tests_bugfix/test_333_report1_bugs.py — regression guards for 333 (Report 1) bugs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── F1: unbounded git subprocess calls (18 sites) ─────────────────────────────


def test_f1_extract_shrink_is_bounded(monkeypatch, tmp_path):
    """gates.extract_shrink reads `git show` — a hung git must be a None, not a hang."""
    import tools.contest.gates as gates

    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout", "__absent__")
        return subprocess.CompletedProcess(cmd, 0, "def _shrink(x):\n    return x\n", "")

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    assert gates.extract_shrink(str(tmp_path), "HEAD") is not None
    assert seen["timeout"] == gates.GIT_TIMEOUT_S


def test_f1_target_nonview_git_is_bounded(monkeypatch):
    """target._run_git(view=False) must carry the module's GIT_TIMEOUT_S, not None."""
    import tools.claimcheck.target as target

    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw.get("timeout", "__absent__"))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    target._run_git(None, "rev-parse", "HEAD")
    target._run_git(None, "rev-parse", "HEAD", view=True)
    assert seen == [target.GIT_TIMEOUT_S, target.VIEW_GIT_TIMEOUT]


def test_f1_harvest_git_calls_are_bounded(monkeypatch, tmp_path):
    """harvest's status / worktree add / remove / prune reads all carry the bound."""
    import tools.contest.gates as gates
    import tools.contest.harvest as harvest

    ws = SimpleNamespace(path=tmp_path, progress_csv=tmp_path / "runs" / "a" / "PROGRESS.csv")
    timeouts = []

    def fake_run_git(cmd, *a, **kw):
        timeouts.append(kw.get("timeout", "__absent__"))
        rc = 1 if "remove" in cmd else 0   # a failed remove forces the prune branch
        return subprocess.CompletedProcess(list(cmd), rc, "", "x" if rc else "")

    monkeypatch.setattr(harvest, "run_git", fake_run_git)
    assert harvest._status_lines(ws) == []
    target_dir, parent, err = harvest._commit_worktree(ws, "abc123")
    assert target_dir is None and err      # the fake never makes the checkout: error path, no raise
    harvest._drop_worktree(ws, parent)
    assert timeouts
    assert all(t == gates.GIT_TIMEOUT_S for t in timeouts)


def test_f1_draft_git_is_bounded(monkeypatch):
    import tools.contest.draft as draft
    import tools.contest.gates as gates

    timeouts = []

    def fake_run_git(cmd, *a, **kw):
        timeouts.append(kw.get("timeout", "__absent__"))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(draft, "run_git", fake_run_git)
    code, out, err = draft._git(Path("/tmp/x"), "status")
    assert code == 0
    assert timeouts == [gates.GIT_TIMEOUT_S]


def test_f1_workspace_git_is_bounded(monkeypatch):
    """workspace._git carries the bound and reads a timeout as a WorkspaceError."""
    import tools.contest.gates as gates
    import tools.contest.workspace as workspace

    timeouts = []

    def fake_run_git(cmd, *a, **kw):
        timeouts.append(kw.get("timeout", "__absent__"))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(workspace, "run_git", fake_run_git)
    assert workspace._git(Path("/tmp/x"), ["status"], check=False).returncode == 0
    assert timeouts == [gates.GIT_TIMEOUT_S]

    def fake_hang(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(str(cmd), kw.get("timeout", 0))

    monkeypatch.setattr(workspace, "run_git", fake_hang)
    with pytest.raises(workspace.WorkspaceError, match="timed out"):
        workspace._git(Path("/tmp/x"), ["status"])


def test_f1_runner_dirty_diff_and_deadline_calls_bounded(monkeypatch):
    """runner's status / add / diff / reset / commit git calls all carry the bound."""
    import tools.contest.gates as gates
    import tools.contest.runner as runner

    ws = SimpleNamespace(path=Path("/tmp/x"), agent="a", branch="b")
    timeouts = []

    def fake_run_git(cmd, *a, **kw):
        timeouts.append(kw.get("timeout", "__absent__"))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(runner, "run_git", fake_run_git)
    assert runner._dirty_tree(ws) == ""
    assert runner._diff_signature(ws) == ""

    # the deadline commit: a dirty tree, nothing committed above the base
    monkeypatch.setattr(runner, "_commits_above", lambda w: 0)
    monkeypatch.setattr(runner, "_dirty_tree", lambda w: "M f.py")
    monkeypatch.setattr(runner, "git", lambda repo, *a: "deadbeef" * 5)
    assert runner._deadline_commit(ws, reason="turn ended") == "deadbeef" * 5
    assert timeouts
    assert all(t == gates.GIT_TIMEOUT_S for t in timeouts)


# ── F2: the old-format registry detector misreads Windows drive paths ─────────


def test_f2_windows_drive_paths_are_not_old_format(tmp_path):
    from tools.auto.executor import Executor

    exe = Executor(base_dir=tmp_path, workspace_root=tmp_path / "ws")
    reg = exe._registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)

    win_path = "C:\\repo\\.agent\\workspace\\T1"
    reg.write_text(json.dumps([win_path]), encoding="utf-8")
    assert exe._read_workspace_registry() == {win_path}

    reg.write_text(json.dumps(["/abs/path/T1"]), encoding="utf-8")
    assert exe._read_workspace_registry() == {"/abs/path/T1"}

    # simple names still read as the old format: no candidates, nothing pruned
    reg.write_text(json.dumps(["T1", "T2"]), encoding="utf-8")
    assert exe._read_workspace_registry() == set()


# ── F3: a failed os.replace used to leak the mkstemp temp file ────────────────


def test_f3_no_temp_leak_when_replace_fails(tmp_path, monkeypatch):
    import tools.metrics_collector as mc

    col = mc.MetricsCollector(metrics_path=tmp_path / "metrics.json")
    record = mc.RunRecord("t", "i", "v", 1, "ok", "", None, 1.0)

    def boom(*a, **kw):
        raise OSError("simulated ENOSPC on the rename")

    monkeypatch.setattr(mc.os, "replace", boom)
    col.record(record)   # never raises: the outer handler logs the failed write
    assert list(tmp_path.glob(".metrics_tmp_*")) == []
    assert not (tmp_path / "metrics.json").exists()


# ── F4: _break must not rmtree a parked lock a live owner keeps ───────────────


def test_f4_parked_live_lock_is_not_deleted(tmp_path, monkeypatch):
    import tools.claimcheck.target as target

    lock_dir = tmp_path / "tree.lock"
    lock_dir.mkdir()
    live = f"{os.getpid()}-333f4"
    (lock_dir / "owner").write_text(live, encoding="utf-8")

    removed = []
    real_rmtree = target.shutil.rmtree

    def spy_rmtree(path_, *a, **kw):
        removed.append(str(path_))
        real_rmtree(path_, *a, **kw)

    monkeypatch.setattr(target.shutil, "rmtree", spy_rmtree)

    lock = target._Lock(lock_dir, wait=1.0)
    # the waiter's misjudgment: it read a live owner as dead and parked the lock
    lock._break(seen=live)

    parked = sorted(tmp_path.glob("tree.lock.stale-*"))
    assert len(parked) == 1, "the parked live lock must stay, not be deleted on the way out"
    assert (parked[0] / "owner").read_text(encoding="utf-8") == live
    assert not removed


def test_f4_stale_lock_is_still_deleted(tmp_path):
    import tools.claimcheck.target as target

    lock_dir = tmp_path / "tree.lock"
    lock_dir.mkdir()
    probe = subprocess.Popen(["true"])
    probe.wait()
    dead = f"{probe.pid}-dead"
    (lock_dir / "owner").write_text(dead, encoding="utf-8")

    target._Lock(lock_dir, wait=1.0)._break(seen=dead)
    assert list(tmp_path.glob("tree.lock.stale-*")) == []


# ── F5: _cached_paths must read its -z output unstripped ──────────────────────


def test_f5_cached_paths_keep_leading_space(tmp_path, monkeypatch):
    import tools.arena.merge as merge

    def fake_git(repo, *args, **kw):
        raw = " x.py\0y.py\0"   # -z output: the first path's leading space is data
        return raw.strip() if kw.get("strip", True) else raw

    monkeypatch.setattr(merge, "git", fake_git)
    assert merge._cached_paths(tmp_path) == [" x.py", "y.py"]


# ── F6: a non-integer attempt must not abort the whole analysis ───────────────


def test_f6_noninteger_attempt_does_not_kill_analysis():
    import analyze_logs

    events = [
        {"run_id": "r1", "kind": "decision", "source": "inner_loop", "target": "inner_loop",
         "ts": "2026-01-01T00:00:00Z", "content": "APPROVED",
         "params": {"task": "T1", "stage": "overall", "attempt": "garbage"}},
    ]
    res = analyze_logs.analyze(events)   # pre-fix: a ValueError out of the whole run
    assert res["r1"]["tasks"]["T1"]["iterations"] == 0


# ── F7: the boolean params read through _flag, the file's own helper ──────────


def test_f7_bool_params_read_through_flag():
    import analyze_logs

    res = analyze_logs.analyze([
        {"run_id": "r1", "kind": "probe_config", "source": "architect", "ts": "t0",
         "params": {"usable": "true", "reason": "x"}},
    ])
    assert res["r1"]["probe_config"]["usable"] is True

    res2 = analyze_logs.analyze([
        {"run_id": "r1", "kind": "probe_escalated", "source": "architect", "ts": "t0",
         "params": {"seeded": "1"}},
    ])
    assert res2["r1"]["probe_escalated"][0]["seeded"] is True


# ── F8: a second rejected event with a matching title is counted ──────────────


def test_f8_duplicate_gate1_rejections_are_counted():
    import analyze_logs

    def rej(i):
        return {"run_id": "r1", "kind": "rejected", "source": "gate1", "ts": f"t{i}",
                "params": {"title": "Fix the parser", "reason": "too speculative"}}

    res = analyze_logs.analyze([rej(1), rej(2)])
    rec = res["r1"]["tasks"]["gate1:Fix the parser"]
    assert rec["rejected"] == 2

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        analyze_logs.render_run_summary(res["r1"])
    line = next(l for l in buf.getvalue().splitlines() if "Gate-1 rejected" in l)
    assert "2 tasks filtered" in line


# ── F9: an explicit current_version: null is a missing value, not a version ───


def test_f9_null_current_version_falls_back_to_stack_top(tmp_path):
    from tools.prompt_store import PromptStore

    store_path = tmp_path / "prompts.json"
    store_path.write_text(json.dumps({
        "agent_x": {
            "stack": [
                {"prompt": "p1", "version": 1},
                {"prompt": "p2"},
                {"prompt": "p3", "version": 3},
            ],
            "current_version": None,
        }
    }), encoding="utf-8")
    store = PromptStore(store_path=store_path)
    assert store.get_current("agent_x") == "p3"
    assert "v3" in store.get_store_summary(["agent_x"])
