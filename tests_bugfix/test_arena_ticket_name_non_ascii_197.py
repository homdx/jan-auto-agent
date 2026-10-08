r"""tests_bugfix/test_arena_ticket_name_non_ascii_197.py — bug 206: a ticket name git quotes is invisible.

Bug: `git ls-tree --name-only` without `-z` C-quotes a name holding a non-ASCII
letter, a `"` or a `\` (`"epic-tasks/\320\260.md"`), and no ticket pattern knows
such a string. Silent in every place it read: `arena issue list` said there was no
ticket, `rounds.find_ticket` said "no ticket 7", `contest.ticket_file` did not see
it on the base and intake read an empty `epic-tasks/` there. Only hand-named files
hit it — `slug_for` writes ASCII.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import cli as arena_cli, gitref, rounds, tickets
from tools.contest import cli as contest_cli

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"

QUOTED = [("07-а.md", 7, "# Cyrillic\n\n**Status:** open\n"),
          ('08-"q".md', 8, '# Quoted\n\n**Status:** open\n')]


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, env=GIT_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout whose `main` holds ticket 01 only, and a branch `quoted` that
    adds the two hand-named tickets — the base a round would be built on."""
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "01-a.md").write_text("# A\n\n**Status:** open\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1: first")
    base = _git(repo, "rev-parse", "HEAD")
    for name, _nn, text in QUOTED:
        (repo / "epic-tasks" / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "7: hand-named tickets")
    _git(repo, "branch", "quoted")
    _git(repo, "reset", "-q", "--hard", base)
    return repo


def test_ls_tree_names_returns_the_names_git_would_quote(repo):
    names = gitref.ls_tree_names(repo, "quoted", "epic-tasks/")
    assert sorted(names) == sorted(
        [f"epic-tasks/{name}" for name, _n, _t in QUOTED] + ["epic-tasks/01-a.md"])


@pytest.mark.parametrize("name,nn,text", QUOTED)
def test_find_ticket_reads_a_branch_name_git_quotes(repo, name, nn, text):
    found, body = rounds.find_ticket(repo, nn, "quoted")
    assert found == name
    assert body == text


@pytest.mark.parametrize("name,nn", [(name, nn) for name, nn, _t in QUOTED])
def test_the_branch_readers_find_the_quoted_name(repo, name, nn):
    assert name in tickets._branch_names(repo, "quoted")
    assert tickets._on_branch(repo, "quoted", nn) == [name]


def test_a_lower_numbered_quoted_name_is_a_blocker(repo):
    assert [b["name"] for b in tickets.blocking_tickets(repo, "quoted", 8)] == [
        "01-a.md", "07-а.md"]


@pytest.mark.parametrize("name,nn,text", QUOTED)
def test_ticket_file_reads_the_name_git_quotes_off_a_base(repo, name, nn, text):
    found, path = contest_cli.ticket_file(repo, repo / "epic-tasks", nn, "quoted")
    assert found == name
    assert path is not None
    assert path.read_text(encoding="utf-8") == text


def test_the_base_listing_sees_both_quoted_names(repo):
    listed = contest_cli._tickets(repo / "epic-tasks", at="quoted")
    assert [number for number, _path, _status, _body in listed] == [1, 7, 8]
    assert sorted(path.name for _n, path, _s, _b in listed) == sorted(
        [name for name, _nn, _t in QUOTED] + ["01-a.md"])


def test_issue_list_shows_the_branch_tickets(repo, monkeypatch, capsys):
    monkeypatch.setattr("tools.arena.cli.REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(repo / "noproc"))
    assert arena_cli.main(["issue", "list", "--branch", "quoted"]) == 0
    out = capsys.readouterr().out
    # the two titles come off the branch's tree; before the fix neither row existed
    assert "Cyrillic" in out and "Quoted" in out
