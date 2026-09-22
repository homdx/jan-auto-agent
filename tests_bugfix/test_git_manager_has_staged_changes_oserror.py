"""tests/test_git_manager_has_staged_changes_oserror.py — report §4, item 10.

GitManager.has_staged_changes() ran `git diff --cached --quiet` via
subprocess.run() and only caught subprocess.TimeoutExpired. If the `git`
binary itself is missing or unusable (PATH misconfigured, git uninstalled
in a stripped-down container, etc.) subprocess.run() raises OSError
(FileNotFoundError is a subclass) — previously uncaught, and inconsistent
with the sibling method _agent_state_is_ignored() defined immediately
above it in the same file, which already catches
(subprocess.TimeoutExpired, OSError) for the identical class of failure.

After the fix, has_staged_changes() catches OSError and raises a clean
GitError instead — deliberately NOT a fail-closed `return False` like
_agent_state_is_ignored(): that method guards a best-effort optional
cleanup step, whereas has_staged_changes() gates whether commit() actually
commits. commit() calls self._run(["git", "commit", ...]) right after
(which already raises GitError, not caught, if git is genuinely unusable),
so silently returning False here would just relabel "git is broken" as
"nothing to commit — skipping" and mask the real problem instead of
surfacing it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.git_manager import GitError, GitManager


def _repo(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    base.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=base, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=base, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=base, capture_output=True, check=True)
    return base


def test_missing_git_binary_raises_clean_git_error(tmp_path, monkeypatch, tmp_path_factory):
    """Simulates a missing `git` executable by pointing PATH at an empty
    directory. Must raise GitError, not an uncaught FileNotFoundError."""
    repo = _repo(tmp_path)
    gm = GitManager(repo)

    empty_bin = tmp_path_factory.mktemp("empty_bin")
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(GitError):
        gm.has_staged_changes()


def test_missing_git_binary_does_not_raise_bare_oserror(tmp_path, monkeypatch, tmp_path_factory):
    """Extra safety net: even if the GitError wrapping regresses, this must
    never surface as a raw, unhelpful OSError/FileNotFoundError."""
    repo = _repo(tmp_path)
    gm = GitManager(repo)

    empty_bin = tmp_path_factory.mktemp("empty_bin2")
    monkeypatch.setenv("PATH", str(empty_bin))

    try:
        gm.has_staged_changes()
    except GitError:
        pass  # expected
    except OSError as exc:  # pragma: no cover - only hit if bug regresses
        pytest.fail(f"has_staged_changes() leaked a bare OSError: {exc}")


def test_normal_operation_unaffected_no_staged_changes(tmp_path):
    """No regression on the happy path: a clean repo reports no staged
    changes without raising."""
    repo = _repo(tmp_path)
    gm = GitManager(repo)
    assert gm.has_staged_changes() is False


def test_normal_operation_unaffected_with_staged_changes(tmp_path):
    """No regression on the happy path: a staged file is detected."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, capture_output=True, check=True)
    gm = GitManager(repo)
    assert gm.has_staged_changes() is True


# ─────────────────────────────────────────────────────────────────────────────
# FL-1 (round 84) — `.git/index.lock` contention is a transient, not a failure
# ─────────────────────────────────────────────────────────────────────────────
#
# git takes `.git/index.lock` for the whole of any command that writes the
# index, and a second git that finds it there exits 128 without waiting.
# GitManager named stale locks in its error text but never waited for a held
# one, so a single collision cost a task its commit: the operator's
# 64-worker stress run surfaced it as `CommitOnSuccess: git error for task
# T1 — git add -u failed` in
# tests_bugfix/test_collect_bridge_stale_after_task_commit.py, and in a real
# autonomous run the same collision loses the agent's work.

def _repo_with_a_commit(tmp_path: Path):
    """A one-commit repo and a GitManager over it."""
    import subprocess as sp
    from tools.auto.git_manager import GitManager

    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("1\n", encoding="utf-8")
    gm = GitManager(tmp_path)
    gm.configure_identity()
    sp.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return gm


def test_a_held_index_lock_is_waited_out_not_raised(tmp_path):
    """Another git holds the index and releases it: the command goes through.

    Without the retry this raises `GitError` on the first attempt — the lock
    is still held when `git add -u` starts, which is the whole point.
    """
    import threading
    import time

    gm = _repo_with_a_commit(tmp_path)
    lock = tmp_path / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")          # somebody else has the index

    released = threading.Event()

    def release():
        time.sleep(0.5)
        lock.unlink()
        released.set()

    threading.Thread(target=release, daemon=True).start()
    (tmp_path / "a.txt").write_text("2\n", encoding="utf-8")

    gm._run(["git", "add", "-u"], "git add -u failed")

    assert released.is_set(), "the add went through before the lock was released"
    assert gm.has_staged_changes() is True


def test_a_stale_index_lock_still_raises_with_the_original_message(tmp_path):
    """The retry is bounded: a lock nobody will ever release is still an
    error, and still the same one — the message already tells the operator
    that a stale `.git/index.lock` is a likely cause."""
    import time

    from tools.auto.git_manager import GitError

    gm = _repo_with_a_commit(tmp_path)
    (tmp_path / ".git" / "index.lock").write_text("", encoding="utf-8")
    (tmp_path / "a.txt").write_text("2\n", encoding="utf-8")

    started = time.monotonic()
    with pytest.raises(GitError) as exc:
        gm._run(["git", "add", "-u"], "git add -u failed")
    elapsed = time.monotonic() - started

    assert "git add -u failed" in str(exc.value)
    assert "index.lock" in str(exc.value)
    # it gave up rather than hanging: the whole ladder is a couple of seconds
    assert elapsed < 30, elapsed


def test_a_real_git_error_is_not_retried(tmp_path):
    """Only lock contention repeats. An ordinary failure raises at once, so a
    broken command does not pay the whole backoff ladder."""
    import time

    from tools.auto.git_manager import GitError

    gm = _repo_with_a_commit(tmp_path)
    started = time.monotonic()
    with pytest.raises(GitError):
        gm._run(["git", "checkout", "no-such-branch-here"], "git checkout failed")
    assert time.monotonic() - started < 1.0
