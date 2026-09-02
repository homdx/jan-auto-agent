"""tests/test_git_discard_safety.py — discard must never destroy run state.

GitManager.discard_working_changes() runs `git reset --hard` followed by
`git clean -fd`.  Its docstring assumes .gitignore shields internal agent
state from the clean:

    "git clean without -x respects .gitignore, so internal agent state
    (.agent/) and coder backups (*.coder.bak) are preserved."

But ensure_gitignore_committed() is best-effort — on OSError (read-only
.gitignore, permissions, full disk) it logs a warning and returns, leaving
".agent/" un-ignored.  In that state the clean deleted the entire run:
plan.json, tickets, feedback rounds, run.log.  Reproduced before the fix:

    .agent/plan.json exists before: True
    .agent/plan.json exists after : False

`git reset --hard` alone already reverts every tracked edit, which is the
residue the method exists to remove, so the clean is skipped when the shield
is not verifiably in place.  Leaving untracked residue is a far smaller harm
than destroying the run's own bookkeeping mid-flight.

This mattered more after BugFixLoop began calling discard on its five
no-commit exits: one call site became six, all inside the regression path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.git_manager import GitManager


def _repo(tmp_path: Path, gitignore: str | None) -> Path:
    base = tmp_path / "repo"
    base.mkdir()
    for cmd in (
        ["git", "init", "-q", "."],
        ["git", "config", "user.email", "a@b.c"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=base, capture_output=True, check=True)
    (base / "f.py").write_text("original\n", encoding="utf-8")
    if gitignore is not None:
        (base / ".gitignore").write_text(gitignore, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=base, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=base,
                   capture_output=True, check=True)
    # run state + a dirty tracked edit standing in for a failed attempt
    (base / ".agent").mkdir()
    (base / ".agent" / "plan.json").write_text('{"tasks":[]}', encoding="utf-8")
    (base / "f.py").write_text("BROKEN half-finished fix\n", encoding="utf-8")
    return base


class TestDiscardPreservesRunState:
    def test_state_survives_when_gitignore_is_missing(self, tmp_path):
        base = _repo(tmp_path, gitignore=None)
        GitManager(base).discard_working_changes()
        assert (base / ".agent" / "plan.json").exists(), "run state destroyed"

    def test_state_survives_when_gitignore_lacks_the_entry(self, tmp_path):
        base = _repo(tmp_path, gitignore="__pycache__/\n")
        GitManager(base).discard_working_changes()
        assert (base / ".agent" / "plan.json").exists()

    def test_state_survives_when_properly_ignored(self, tmp_path):
        base = _repo(tmp_path, gitignore=".agent/\n")
        GitManager(base).discard_working_changes()
        assert (base / ".agent" / "plan.json").exists()

    @pytest.mark.parametrize("gitignore", [None, "__pycache__/\n", ".agent/\n"])
    def test_tracked_residue_is_always_reverted(self, tmp_path, gitignore):
        """The method's actual job must still happen in every configuration."""
        base = _repo(tmp_path, gitignore=gitignore)
        GitManager(base).discard_working_changes()
        assert (base / "f.py").read_text(encoding="utf-8").strip() == "original"

    def test_untracked_residue_cleaned_when_shield_present(self, tmp_path):
        base = _repo(tmp_path, gitignore=".agent/\n")
        (base / "stray.py").write_text("junk\n", encoding="utf-8")
        GitManager(base).discard_working_changes()
        assert not (base / "stray.py").exists()
        assert (base / ".agent" / "plan.json").exists()

    def test_check_ignore_fails_closed(self, tmp_path, monkeypatch):
        """Anything unexpected must skip the clean, not risk it."""
        base = _repo(tmp_path, gitignore=".agent/\n")
        gm = GitManager(base)

        def boom(*a, **kw):
            raise OSError("git missing")

        monkeypatch.setattr(subprocess, "run", boom)
        assert gm._agent_state_is_ignored() is False
