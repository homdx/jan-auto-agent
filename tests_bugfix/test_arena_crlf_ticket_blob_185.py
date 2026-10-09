"""tests_bugfix/test_arena_crlf_ticket_blob_185.py — bug 185: a CRLF ticket read from a branch keeps its bytes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.arena import gitref, rounds


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (repo / "epic-tasks" / "05-t.md").write_bytes(b"# 5 t\r\nbody\rmore\r\n")
    run("add", ".")
    run("commit", "-qm", "x")
    return repo


def test_show_keeps_crlf_and_lone_cr(tmp_path):
    repo = _repo(tmp_path)
    text = gitref.git(repo, "show", "main:epic-tasks/05-t.md", strip=False)
    assert text == "# 5 t\r\nbody\rmore\r\n"


def test_a_crlf_ticket_on_the_tip_needs_no_extra_commit(tmp_path):
    repo = _repo(tmp_path)
    name = "05-t.md"
    content = gitref.git(repo, "show", f"main:epic-tasks/{name}", strip=False)
    tip = gitref.git(repo, "rev-parse", "main")
    sha = rounds.build_round_ref(repo, 5, "main", name, content)
    assert sha == tip, "the ticket already on the tip was re-committed with LF endings"


def test_stdin_round_trips_the_same_blob(tmp_path):
    repo = _repo(tmp_path)
    on_tip = gitref.git(repo, "rev-parse", "main:epic-tasks/05-t.md")
    content = gitref.git(repo, "show", "main:epic-tasks/05-t.md", strip=False)
    assert gitref.git(repo, "hash-object", "--stdin", stdin=content) == on_tip
