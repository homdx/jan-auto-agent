"""tests/test_executor_workspace_prune_order.py

Regression test for a bug in Executor._prepare_workspace / _prune_old_workspaces
(tools/auto/executor.py), introduced alongside the workspace_retain_count
fix ("Disk space fixes") and fixed by AUTO-FIX-2.

_prepare_workspace populates each per-task workspace with
``shutil.copytree(self._base_dir, workspace, ...)``. copytree's default
copy_function (copy2) copies *metadata* — including mtime — from the
source onto the destination directory itself. Since every task's workspace
is copied from the same base_dir, every workspace directory ended up with
base_dir's (static) mtime instead of a fresh one reflecting when it was
actually built.

_prune_old_workspaces sorts sibling workspaces by mtime to decide which
are "oldest" and should be evicted first. With every workspace sharing an
identical mtime, that sort was meaningless and could evict an effectively
arbitrary sibling instead of the actually-oldest one — silently breaking
the "most recently used workspaces survive" guarantee even though the
retention *count* was still enforced.

The fix stamps the workspace with a fresh mtime (os.utime) right after
population, before pruning runs, so eviction order is reliably oldest-first.
"""

from __future__ import annotations

import time

from tools.auto.executor import make_executor


def _run_task(executor, task_id: str) -> None:
    executor.run({"id": task_id, "acceptance_check": "true", "target_files": []})


def test_prune_evicts_oldest_workspace_first(tmp_path):
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "conftest.py").write_text("")

    executor = make_executor(base_dir=base_dir, timeout_sec=5, max_retained_workspaces=3)
    workspace_root = base_dir / ".agent" / "workspace"

    task_ids = [f"AUTO-T{i}" for i in range(5)]
    for task_id in task_ids:
        _run_task(executor, task_id)
        # Force distinct wall-clock times; the bug this guards against
        # manifested even with a real time gap between runs, since the
        # broken mtime came from copytree metadata, not from timing.
        time.sleep(1.05)

    remaining = sorted(p.name for p in workspace_root.iterdir())

    # Only the 3 most-recently-created task workspaces should survive, in
    # strict creation order — not an arbitrary subset caused by a broken
    # (all-equal) mtime sort.
    assert remaining == ["AUTO-T2", "AUTO-T3", "AUTO-T4"]


def test_workspace_mtime_is_not_clobbered_by_copytree(tmp_path):
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "conftest.py").write_text("")

    executor = make_executor(base_dir=base_dir, timeout_sec=5, max_retained_workspaces=5)
    workspace_root = base_dir / ".agent" / "workspace"

    _run_task(executor, "AUTO-T0")
    ws0_mtime = (workspace_root / "AUTO-T0").stat().st_mtime

    time.sleep(1.05)

    _run_task(executor, "AUTO-T1")
    ws1_mtime = (workspace_root / "AUTO-T1").stat().st_mtime

    # Before the fix these were identical, both inherited from base_dir's
    # own mtime via shutil.copytree's default metadata-preserving copy.
    assert ws1_mtime > ws0_mtime
