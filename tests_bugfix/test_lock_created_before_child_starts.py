"""tests_bugfix/test_lock_created_before_child_starts.py — regression guard for the lock timing bug.

When the lock is written after the child starts, the child cannot read it.
This test verifies the lock exists before the child starts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tools.arena import rounds, cli


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    INI = """[contest]
out_dir = contest-out

[contest.agent.agent-a]
model = test/agent-a
"""
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / ".gitignore").write_text(".arena/\ncontest-out/\n", encoding="utf-8")

    ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    proc = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo),
                          capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0

    proc = subprocess.run(["git", "add", "-A"], cwd=str(repo),
                          capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0

    proc = subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(repo),
                          capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0

    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])

    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", "/tmp/noproc")

    return repo


def test_lock_exists_before_child_reads_it(repo, monkeypatch):
    """The lock must exist before the child reads it.

    This tests the fix for the race condition where the lock was written
    after the child had already started. The child should see a valid PID
    in the lock file before it exits.
    """
    TICKET = """# Test Ticket

**Status:** open
**File:** test.py
**Symbol:** test
**Size:** S

body
"""
    (repo / ".arena" / "drafts" / "07-x.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / ".arena" / "drafts" / "07-x.md").write_text(TICKET, encoding="utf-8")

    seen_lock = repo / "seen-lock"

    def spawn(line, cwd):
        """SPAWN stub that sleeps before reading the lock."""
        proc = subprocess.Popen([sys.executable, "-c", """
import time
import sys
from pathlib import Path
time.sleep(0.2)
lock = Path.cwd() / ".arena" / "locks" / "7.pid"
pid = lock.read_text().strip()
Path("seen-lock").write_text(pid + "\\n")
print(f"child sees pid: {{pid}}", file=sys.stderr)
"""], cwd=cwd)
        return proc

    spawn.seen = []

    monkeypatch.setattr(rounds, "SPAWN", spawn)

    result = cli.main(["run", "start", "7"])

    assert result == 0
    assert seen_lock.exists()

    pid = seen_lock.read_text().strip()
    assert pid.isdigit()
    print(f"Child saw PID: {pid}")

    lock_file = repo / ".arena" / "locks" / "7.pid"
    assert not lock_file.exists(), "Lock should be removed after child exits"


def test_lock_cleanup_on_spawn_failure(repo, monkeypatch):
    """When SPAWN raises an OSError, the lock file should be cleaned up.

    This verifies that we don't leave a stale lock file behind when the
    child cannot be started.
    """
    TICKET = """# Test Ticket

**Status:** open
**File:** test.py
**Symbol:** test
**Size:** S

body
"""
    (repo / ".arena" / "drafts" / "07-x.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / ".arena" / "drafts" / "07-x.md").write_text(TICKET, encoding="utf-8")

    def spawn(line, cwd):
        """SPAWN stub that always raises OSError."""
        raise OSError("Mock spawn failure")

    spawn.seen = []

    monkeypatch.setattr(rounds, "SPAWN", spawn)

    result = cli.main(["run", "start", "7"])

    assert result in (1, 2)
    assert result == rounds.EXIT_FAILED or result == rounds.EXIT_USAGE

    lock_file = repo / ".arena" / "locks" / "7.pid"
    assert not lock_file.exists(), "Lock file should be cleaned up on spawn failure"
