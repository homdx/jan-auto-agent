"""Bug 44 (209): AR-14's git readers in tickets.py read git the way rounds.py does since 197.

The fix itself landed with ticket 197 (`gitref.ls_tree_names`, `gitref.status_paths`); this
file pins the three readers in `tickets.py` and that they share one reader with
`rounds._dirty_tasks`, so the two cannot drift again.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import gitref, rounds, tickets

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "arena")
    for rel in ("epic-tasks/02-баг.md", "epic-tasks/07-x.md", "epic-tasks/08 spaced name.md",
                ".arena/state.json"):
        _write(r, rel)
    _git(r, "add", "-f", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_a_non_ascii_ticket_is_found_on_a_branch(repo):
    assert tickets._on_branch(repo, "arena", 2) == ["02-баг.md"]


def test_an_unstaged_change_is_named_whole(repo):
    _write(repo, "epic-tasks/07-x.md", "changed\n")
    assert tickets._uncommitted(repo, "epic-tasks/07-x.md") == ["epic-tasks/07-x.md"]
    assert tickets._modified(repo) == ["epic-tasks/07-x.md"]


def test_a_modified_tracked_arena_file_is_not_the_operators_work(repo):
    _write(repo, ".arena/state.json", "changed\n")
    assert tickets._modified(repo) == []


def test_a_clean_checkout_has_nothing_modified(repo):
    assert tickets._modified(repo) == [] and tickets._uncommitted(repo, "epic-tasks/07-x.md") == []


def test_spaces_non_ascii_and_untracked_names_come_back_as_they_are(repo):
    _write(repo, "epic-tasks/08 spaced name.md", "changed\n")
    _write(repo, "epic-tasks/09-ещё.md")
    assert sorted(tickets._modified(repo)) == ["epic-tasks/08 spaced name.md", "epic-tasks/09-ещё.md"]


def test_a_rename_is_its_new_name_alone(repo):
    _git(repo, "mv", "epic-tasks/07-x.md", "epic-tasks/07-y.md")
    assert tickets._modified(repo) == ["epic-tasks/07-y.md"]


def test_the_tickets_and_the_rounds_readers_are_one(repo):
    _write(repo, "epic-tasks/07-x.md", "changed\n")
    assert rounds._dirty_tasks(repo) == gitref.status_paths(repo, "epic-tasks/") == \
        tickets._uncommitted(repo, "epic-tasks/")
