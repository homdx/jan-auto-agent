"""tests_bugfix/test_arena_non_utf8_ticket_174.py — pins ticket 174: a non-UTF-8 ticket or commit subject is read, not a traceback."""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

from tools.arena import rounds, tickets
from tools.arena.gitref import GitRefError, git, printable

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
LATIN1 = b"# 2 \xe9t\xe9\n\n**Status:** open\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "epic-tasks" / "01-a.md").write_text("# 1 — a\n\n**Status:** open\n")
    (repo / "epic-tasks" / "02-b.md").write_bytes(LATIN1)
    _git(repo, "add", "-A")
    _git(repo, "-c", "i18n.commitEncoding=latin1", "commit", "-q", "-m", "2: caf\xe9")
    return repo


def _cfg():
    return types.SimpleNamespace(out_dir="contest-out")


def test_scan_reads_a_latin1_ticket_and_subject(tmp_path):
    repo = _repo(tmp_path)
    found = {t.number: t for t in tickets.scan(repo, _cfg(), "main", proc_root=str(tmp_path))}
    assert sorted(found) == [1, 2]
    assert found[2].title == "2 �t�"
    assert found[2].flags == ["commit 2: on main but status open"]


def test_scan_reads_the_branch_copy_too(tmp_path):
    repo = _repo(tmp_path)
    (repo / "epic-tasks" / "02-b.md").unlink()  # only the branch holds it now
    found = {t.number: t for t in tickets.scan(repo, _cfg(), "main", proc_root=str(tmp_path))}
    assert found[2].where == tickets.WHERE_BRANCH
    assert found[2].title == "2 �t�"


def test_round_ref_keeps_the_ticket_bytes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "epic-tasks" / "02-b.md").write_bytes(LATIN1.replace(b"open", b"queued"))
    name, text = rounds.find_ticket(repo, 2, "main")
    content = rounds.open_status(text)
    sha = rounds.build_round_ref(repo, 2, "main", name, content)
    blob = subprocess.run(["git", "show", f"{sha}:epic-tasks/{name}"], cwd=repo,
                          capture_output=True, check=True).stdout
    assert blob == LATIN1  # status forced back to open, the é bytes untouched
    # the ticket on the tip already says open: the ref is the tip, no new commit
    assert sha == git(repo, "rev-parse", "main")


def test_git_error_line_is_printable(tmp_path):
    repo = _repo(tmp_path)
    try:
        git(repo, "show", "main:epic-tasks/\udce9.md")
    except GitRefError as err:
        str(err).encode("utf-8")  # no lone surrogate reaches the screen
    else:
        raise AssertionError("expected a GitRefError")
    assert printable("a\udce9b") == "a�b"
