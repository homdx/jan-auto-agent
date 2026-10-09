"""tests_bugfix/test_arena_issue_git_reads_211.py — bug 211: AR-14's git readers repeat bugs 206 and 209.

`arena issue queue|open|close|reopen|edit` (AR-14) read git in three places the same
way the older code did before bugs 206 and 209 were fixed:

  * `_on_branch` listed a branch's tickets with `ls-tree --name-only`, which quotes a
    name holding a non-ASCII letter, so `arena issue queue 2 --branch B` on a ticket
    named `02-баг.md` said "no ticket 2 ... on B".
  * `_uncommitted` and `_modified` read `status --porcelain` through `git()`, which
    strips the whole output: an unstaged change is ` M path`, its first space was eaten
    and `line[3:]` cut a letter — the refusal read `uncommitted: pic-tasks/07-x.md`, and
    a modified tracked file under `.arena/` (excluded by name) was counted as the
    operator's work, so a clean checkout showed `(1 modified)`.
All three now go through the readers of `gitref` (`tree_names`, `changed_paths`).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import cli, rounds, tickets

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
TICKET = "# {t}\n\n**File:** a.py\n**Symbol:** f\n**Status:** open\n\n## Body\n"


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          env={**os.environ, **ENV})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    (repo / ".arena").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "01-a.md").write_text(TICKET.format(t="A"), encoding="utf-8")
    (repo / ".arena" / "state").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", "1: a")
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "epic-tasks" / "02-баг.md").write_text(TICKET.format(t="Баг"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "2: b")
    _git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def test_on_branch_finds_a_non_ascii_ticket_name(repo):
    assert tickets._on_branch(repo, "other", 2) == ["02-баг.md"]
    assert tickets._on_branch(repo, "other", 1) == ["01-a.md"]
    assert tickets._on_branch(repo, "no-such-branch", 2) == []


def test_issue_queue_reaches_a_non_ascii_ticket_on_another_branch(repo, capsys):
    assert cli.main(["issue", "queue", "2", "--branch", "other", "-y"]) == 0
    text = _git(repo, "show", "other:epic-tasks/02-баг.md")
    assert "**Status:** queued" in text and "## Body" in text


def test_uncommitted_names_a_modified_unstaged_file_whole(repo):
    with (repo / "epic-tasks" / "01-a.md").open("a", encoding="utf-8") as fh:
        fh.write("more\n")
    assert tickets._uncommitted(repo, "epic-tasks/01-a.md") == ["epic-tasks/01-a.md"]
    assert tickets._uncommitted(repo, "epic-tasks") == ["epic-tasks/01-a.md"]


def test_uncommitted_names_a_non_ascii_untracked_file_raw(repo):
    (repo / "epic-tasks" / "03-новый.md").write_text("x\n", encoding="utf-8")
    assert tickets._uncommitted(repo, "epic-tasks") == ["epic-tasks/03-новый.md"]


def test_a_clean_ticket_has_nothing_uncommitted(repo):
    assert tickets._uncommitted(repo, "epic-tasks/01-a.md") == []


def test_a_modified_tracked_file_under_arena_is_not_the_operators_work(repo):
    (repo / ".arena" / "state").write_text("two\n", encoding="utf-8")
    assert tickets._modified(repo) == []
    assert tickets._checkout_text(repo).endswith("on main (clean)")


def test_modified_counts_what_is_the_operators(repo):
    with (repo / "epic-tasks" / "01-a.md").open("a", encoding="utf-8") as fh:
        fh.write("more\n")
    (repo / ".arena" / "state").write_text("two\n", encoding="utf-8")
    assert tickets._modified(repo) == ["epic-tasks/01-a.md"]
    assert tickets._checkout_text(repo).endswith("on main (1 modified)")
