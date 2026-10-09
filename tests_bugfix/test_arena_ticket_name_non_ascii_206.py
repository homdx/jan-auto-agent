"""tests_bugfix/test_arena_ticket_name_non_ascii_206.py — bug 206: a ticket named with non-ASCII letters is invisible on a branch.

Bug: ticket names on a branch were read from `git ls-tree --name-only`, one name per
line. Without `-z` git QUOTES a name holding a byte above 0x7f (unless the reader's
`core.quotePath` says otherwise) or a `"`, a `\\` or a control character:
`"epic-tasks/02-\\320\\261\\320\\260\\320\\263.md"`. The reader cut at the last `/`, so the
name it held was `02-\\320\\261\\320\\260\\320\\263.md"` — a stray quote, escaped bytes, no
`.md` at the end — and no ticket pattern matched it. Nothing failed: `arena issue
list --branch B` simply left the ticket out, `arena run start 2 --branch B` said
"no ticket 2 ... on B", and the contest runner's `_tickets(at=…)` / `ticket_file(…)`
(which read the round's base commit, the one `arena run start` builds) did not see
it either. `git ls-tree -z` names every entry raw, NUL-terminated, whatever the config.
`slug_for` writes ASCII names, so this bites a ticket file named by hand — a Cyrillic
`NN-баг.md` is the case that was reported.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tools.arena import cli, rounds, tickets
from tools.contest import cli as contest_cli

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
RUSSIAN = "02-баг.md"
QUOTED = '03-say "hi".md'
TICKET = "# {title}\n\n**Status:** open\n"


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """`other` holds tickets 1, 2 (Cyrillic) and 3 (a double quote); the checkout (main) only 1."""
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "01-a.md").write_text(TICKET.format(title="First"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1: first")
    _git(repo, "checkout", "-q", "-b", "other")
    for name, title in ((RUSSIAN, "Баг"), (QUOTED, "Quoted")):
        (repo / "epic-tasks" / name).write_text(TICKET.format(title=title), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "2, 3: the odd names")
    _git(repo, "checkout", "-q", "main")
    assert sorted(p.name for p in (repo / "epic-tasks").iterdir()) == ["01-a.md"]
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def test_git_really_does_quote_these_names(repo):
    """The premise: without -z the listing is not the names."""
    listed = _git(repo, "-c", "core.quotepath=true", "ls-tree", "--name-only", "other", "epic-tasks/")
    assert '"' in listed and "баг" not in listed


def test_find_ticket_finds_a_non_ascii_name_on_a_branch(repo):
    name, text = rounds.find_ticket(repo, 2, "other")
    assert name == RUSSIAN
    assert "Баг" in text


def test_find_ticket_finds_a_name_with_a_double_quote(repo):
    name, text = rounds.find_ticket(repo, 3, "other")
    assert name == QUOTED and "Quoted" in text


def test_branch_names_lists_every_ticket_raw(repo):
    assert sorted(tickets._branch_names(repo, "other")) == sorted(["01-a.md", RUSSIAN, QUOTED])


def test_issue_list_shows_the_tickets_instead_of_leaving_them_out(repo, capsys):
    assert cli.main(["-o", "json", "issue", "list", "--branch", "other"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert sorted(int(r["number"]) for r in rows) == [1, 2, 3]


def test_the_contest_runner_reads_the_names_of_a_base_commit_raw(repo):
    tasks = repo / "epic-tasks"
    assert [t[0] for t in contest_cli._tickets(tasks, at="other")] == [1, 2, 3]


def test_ticket_file_finds_a_base_only_ticket_with_a_non_ascii_name(repo):
    tasks = repo / "epic-tasks"
    name, path = contest_cli.ticket_file(repo, tasks, 2, "other")
    try:
        assert name == RUSSIAN
        assert path is not None and "Баг" in path.read_text(encoding="utf-8")
    finally:
        for folder in contest_cli._TEMP_TICKET_DIRS:
            shutil.rmtree(folder, ignore_errors=True)
        contest_cli._TEMP_TICKET_DIRS.clear()
