"""tests_bugfix/test_contest_park_line_199.py — Bug 2: _park_line sed must match the whole status line."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.contest.cli as contest_cli


def _write_ticket(path, status_line):
    path.write_text(
        "**Size:** M\n**File:** foo\n**Symbol:** bar\n\n**Status:** " + status_line + "\n",
        encoding="utf-8",
    )


def test_park_line_sed_matches_two_spaces():
    body = "**Size:** M\n**File:** foo\n**Symbol:** bar\n\n**Status:**  Open\n"
    result = contest_cli._park_line(body, "t.md", 1, "epic-tasks")
    assert "sed -i " in result
    assert "**Status:** queued" in result


def test_park_line_sed_matches_capital():
    body = "**Size:** M\n**File:** foo\n**Symbol:** bar\n\n**Status:** Open\n"
    result = contest_cli._park_line(body, "t.md", 1, "epic-tasks")
    assert "sed -i " in result


def test_park_line_preserves_commit_on_real_repo():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        ticket = repo / "epic-tasks" / "t.md"
        ticket.parent.mkdir()
        _write_ticket(ticket, "Open")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        body = ticket.read_text(encoding="utf-8")
        cmd = contest_cli._park_line(body, "t.md", 1, "epic-tasks")
        subprocess.run(cmd, cwd=repo, shell=True, check=True)

        status = subprocess.run(
            ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
        )
        assert "queued" in status.stdout
        assert "**Status:** queued" in ticket.read_text(encoding="utf-8")
