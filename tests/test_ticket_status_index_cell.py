"""178: ticket_status.py rewrites the INDEX row's whole status cell, so no stale sha survives a flip."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ticket_status.py"


def _repo(tmp_path, word="landed `abc1234`"):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks" / "48-x.md").write_text(
        "# KC-9 x\n\n**Status:** landed `abc1234` — won\n", encoding="utf-8")
    (tmp_path / "epic-tasks" / "INDEX.md").write_text(
        f"| # | id | status |\n|---|---|---|\n| 48 | `KC-9` | {word} | HIGH | S | [x](48-x.md) |\n",
        encoding="utf-8")
    return tmp_path


def _run(repo, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args, "48", "--repo", str(repo), "--no-commit"],
                          capture_output=True, text=True)


def _row(repo):
    return (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8").splitlines()[-1]


def test_open_drops_the_old_sha_from_the_row_and_the_ticket(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "open").returncode == 0
    assert _row(repo) == "| 48 | `KC-9` | open | HIGH | S | [x](48-x.md) |"
    assert "abc1234" not in (repo / "epic-tasks" / "48-x.md").read_text(encoding="utf-8")


def test_landed_again_replaces_the_sha(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "landed", "--sha", "def5678").returncode == 0
    assert _row(repo) == "| 48 | `KC-9` | landed `def5678` | HIGH | S | [x](48-x.md) |"


def test_bold_and_dated_cells_are_replaced_whole(tmp_path):
    repo = _repo(tmp_path, "**landed** (`abc1234`) — round 48 winner")
    assert _run(repo, "queued").returncode == 0
    assert _row(repo) == "| 48 | `KC-9` | queued | HIGH | S | [x](48-x.md) |"
