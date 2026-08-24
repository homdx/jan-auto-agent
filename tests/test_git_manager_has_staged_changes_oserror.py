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
