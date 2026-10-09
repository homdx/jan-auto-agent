"""tests_bugfix/test_arena_issue_git_readers_209.py — ticket 209, bug 44 (the Sonnet 5 entry's cases; fixed by 197, pinned).

`tools/arena/tickets.py`'s `_on_branch`, `_uncommitted` and `_modified` used to
repeat two bugs ticket 197/206/209 already fixed in `rounds.py`:
- `_on_branch` listed a branch's tickets with `ls-tree --name-only`, which
  C-quotes a non-ASCII name (`"epic-tasks/\\320\\261...md"`), so a ticket
  named with a non-ASCII letter was invisible to `arena issue ... --branch B`.
- `_uncommitted`/`_modified` read `git status --porcelain` through a helper
  that stripped the whole output, cutting an unstaged entry's leading space
  (` M path` lost its first character) and missing a `.arena/`-excluded
  file's own exclusion once its name was cut.

By the time this ticket's code was touched, both readers already delegate to
the shared `tools/arena/gitref.py` helpers (`ls_tree_names`, `status_paths`)
that `rounds._dirty_tasks` uses too — this file pins that down so the three
readers cannot drift apart again.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """On branch `arena`: a non-ASCII-named ticket 2, plain ticket 7, tracked `.arena/`."""
    r = tmp_path / "repo"
    _write(r / "epic-tasks" / "02-баг.md", "# AR-2 — баг\n\n**Status:** open\n")
    _write(r / "epic-tasks" / "07-x.md", "# AR-7 — x\n\n**Status:** open\n")
    _write(r / ".arena" / "locks" / "7.pid", "1\n")
    _write(r / "contest.ini", INI)
    _write(r / ".gitignore", ".arena/\nout/\n")
    _git(r, "init", "-q", "-b", "arena")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "tickets")
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


# ── `_on_branch`: `ls-tree -z --name-only`, not the quoting `--name-only` ──

def test_on_branch_finds_a_non_ascii_named_ticket(repo):
    assert tickets._on_branch(repo, "arena", 2) == ["02-баг.md"]


def test_on_branch_finds_a_plain_ticket_too(repo):
    assert tickets._on_branch(repo, "arena", 7) == ["07-x.md"]


def test_on_branch_names_the_ticket_a_cli_call_needs_to_find_it(repo, capsys):
    """`arena issue queue 2 --branch arena` on a non-ASCII name — the whole
    bug: the old `ls-tree --name-only` quoting made `find_ticket` blind to it.
    """
    code = cli.main(["-y", "issue", "queue", "2", "--branch", "arena"])
    cap = capsys.readouterr()
    assert code == rounds.EXIT_OK, cap.err
    assert "баг" not in cap.err  # no refusal naming a ticket it could not find
    body = _git(repo, "show", "arena:epic-tasks/02-баг.md")
    assert "**Status:** queued" in body


def test_on_branch_sees_a_space_in_the_name(repo):
    _git(repo, "mv", "epic-tasks/07-x.md", "epic-tasks/07-x y.md")
    _git(repo, "commit", "-q", "-m", "rename with a space")
    assert tickets._on_branch(repo, "arena", 7) == ["07-x y.md"]


# ── `_uncommitted` / `_modified`: `status --porcelain -z`, read raw ────────

def test_uncommitted_names_an_unstaged_entry_whole(repo):
    _write(repo / "epic-tasks" / "07-x.md", "# AR-7 — x\n\n**Status:** open\nx\n")
    assert tickets._uncommitted(repo, "epic-tasks/07-x.md") == ["epic-tasks/07-x.md"]


def test_uncommitted_names_a_staged_entry_whole_too(repo):
    _write(repo / "epic-tasks" / "07-x.md", "# AR-7 — x\n\n**Status:** open\nx\n")
    _git(repo, "add", "epic-tasks/07-x.md")
    assert tickets._uncommitted(repo, "epic-tasks/07-x.md") == ["epic-tasks/07-x.md"]


def test_uncommitted_names_a_rename_by_its_new_name(repo):
    _git(repo, "mv", "epic-tasks/07-x.md", "epic-tasks/07-renamed.md")
    assert tickets._uncommitted(repo, "epic-tasks/07-renamed.md") == ["epic-tasks/07-renamed.md"]


def test_modified_is_empty_on_a_clean_checkout(repo):
    assert tickets._modified(repo) == []


def test_modified_counts_an_unstaged_ticket(repo):
    _write(repo / "epic-tasks" / "07-x.md", "# AR-7 — x\n\n**Status:** open\nx\n")
    assert tickets._modified(repo) == ["epic-tasks/07-x.md"]


def test_modified_does_not_count_a_tracked_arena_file(repo):
    """Bug 44: a cut name no longer started with `.arena/`, so this counted
    as the operator's own work and a clean checkout read `(1 modified)`."""
    _write(repo / ".arena" / "locks" / "7.pid", "2\n")
    assert tickets._modified(repo) == []
    assert "clean" in tickets._checkout_text(repo)


def test_modified_counts_an_untracked_file_under_epic_tasks(repo):
    _write(repo / "epic-tasks" / "09-new.md", "# N\n")
    assert tickets._modified(repo) == ["epic-tasks/09-new.md"]


def test_modified_sees_a_rename_with_a_space_in_the_new_name(repo):
    _git(repo, "mv", "epic-tasks/07-x.md", "epic-tasks/07-x y.md")
    assert tickets._modified(repo) == ["epic-tasks/07-x y.md"]


# ── the shared reader: `_dirty_tasks`, `_uncommitted` and `_modified` never
#    read `status --porcelain` on their own ──────────────────────────────────

def test_uncommitted_and_modified_share_gitref_status_paths():
    """Pins the fix to one implementation: both call into `gitref.status_paths`
    rather than parsing `git status --porcelain` themselves, so ticket 197/209's
    fix cannot drift away from this file's readers again."""
    import inspect
    assert "status_paths" in inspect.getsource(tickets._uncommitted)
    assert "status_paths" in inspect.getsource(tickets._modified)
    assert "ls_tree_names" in inspect.getsource(tickets._on_branch)
