"""tests_bugfix/test_arena_tickets_review.py — bugs found in tools/arena's ticket handling.

Each test names the bug it pins. They fail on the code as found and pass once the
matching fix is in.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.arena import cli, rounds, tickets

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = """[contest]
out_dir = out

[contest.agent.a]
model = test/a
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "arena")
    _write(repo / "contest.ini", INI)
    _commit(repo, "init")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


# ── bug: `_dirty_tasks` cut the first path's leading character ────────────────
# gitref.git() strips its output; the first `git status --porcelain` line of a
# modified-but-unstaged file starts with the significant space of " M", so the
# strip ate it and `line[3:]` then ate the path's first letter.

def test_dirty_tasks_names_every_modified_file_in_full(repo):
    _write(repo / "epic-tasks" / "01-one.md", "# one\n")
    _write(repo / "epic-tasks" / "02-two.md", "# two\n")
    _commit(repo, "tickets")
    for name in ("01-one.md", "02-two.md"):
        with open(repo / "epic-tasks" / name, "a", encoding="utf-8") as handle:
            handle.write("edited\n")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/01-one.md", "epic-tasks/02-two.md"]


def test_dirty_tasks_names_a_lone_modified_file_in_full(repo):
    _write(repo / "epic-tasks" / "01-one.md", "# one\n")
    _commit(repo, "ticket")
    (repo / "epic-tasks" / "01-one.md").write_text("# one, edited\n", encoding="utf-8")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/01-one.md"]


def test_run_start_refusal_names_the_dirty_file_correctly(repo, capsys):
    _write(repo / "epic-tasks" / "01-one.md", "# one\n\n**Status:** open\n")
    _commit(repo, "ticket")
    with open(repo / "epic-tasks" / "01-one.md", "a", encoding="utf-8") as handle:
        handle.write("edited\n")
    code = cli.main(["run", "start", "1"])
    err = capsys.readouterr().err
    assert code != 0
    assert "epic-tasks/01-one.md" in err
    assert " pic-tasks/" not in err


# ── bug: `open_status` rewrote one line ending of a CRLF ticket ───────────────
# `**Status:**.*$` in MULTILINE mode: `.` takes the `\r`, so the status line was
# replaced without it while every other line kept `\r\n` (mixed endings, and a
# base-ref blob that is not the ticket's bytes).

def test_open_status_keeps_the_crlf_of_the_status_line():
    text = "# t\r\n\r\n**Status:** queued\r\n\r\nbody\r\n"
    assert rounds.open_status(text) == "# t\r\n\r\n**Status:** open\r\n\r\nbody\r\n"


def test_open_status_keeps_a_status_line_at_the_end_of_a_crlf_file():
    assert rounds.open_status("# t\r\n**Status:** landed\r\n") == "# t\r\n**Status:** open\r\n"


def test_open_status_on_lf_text_is_unchanged_behaviour():
    assert rounds.open_status("# t\n\n**Status:** queued — note\n\nbody\n") == \
        "# t\n\n**Status:** open\n\nbody\n"


def test_open_status_leaves_a_ticket_without_a_status_line_alone():
    assert rounds.open_status("# t\n\nbody\n") == "# t\n\nbody\n"


# ── bug: tickets with non-ASCII names were unreadable on the branch ──────────
# `git ls-tree --name-only` octal-quotes such names (`"epic-tasks/12-caf\303\251.md"`),
# which no ticket pattern matches: `run start 12` said "no ticket 12", and
# `next_number` handed 12 out again.

NAME = "12-café.md"
BODY = "# Café\n\n**Status:** open\n\nbody\n"


@pytest.fixture
def branch_only(repo) -> Path:
    """`arena` holds epic-tasks/12-café.md; the checkout is on `work`, which does not."""
    _git(repo, "branch", "work")
    _write(repo / "epic-tasks" / NAME, BODY)
    _commit(repo, "12: café")
    _git(repo, "checkout", "-q", "work")
    assert not (repo / "epic-tasks" / NAME).exists()
    return repo


def test_branch_names_lists_a_non_ascii_ticket_by_its_real_name(branch_only):
    assert tickets._branch_names(branch_only, "arena") == [NAME]


def test_find_ticket_reads_a_non_ascii_ticket_from_the_branch(branch_only):
    name, text = rounds.find_ticket(branch_only, 12, "arena")
    assert name == NAME
    assert text == BODY


def test_issue_list_shows_a_non_ascii_ticket_that_is_only_on_the_branch(branch_only, capsys):
    code = cli.main(["-o", "json", "issue", "list", "--branch", "arena"])
    out = capsys.readouterr().out
    rows = json.loads(out) if out.strip() else []
    assert code == 0
    assert [(row["number"], row["where"]) for row in rows] == [(12, "branch")]
    assert rows[0]["path"] == f"arena:epic-tasks/{NAME}"


def test_next_number_does_not_reuse_a_non_ascii_branch_ticket(branch_only):
    config = SimpleNamespace(out_dir="out")
    assert tickets.next_number(branch_only, config, "arena") == 13


# ── bug: `issue view` could not open a ticket whose file name holds a colon ───
# It recovered the name with `Path(path).name.split(":")[-1]`, which is meant to
# drop a `<branch>:` prefix and so also cut `12-fix: thing.md` down to ` thing.md`.

@pytest.mark.parametrize("where_branch", [False, True])
def test_issue_view_opens_a_ticket_with_a_colon_in_its_name(repo, capsys, where_branch):
    name = "12-fix: thing.md"
    _write(repo / "epic-tasks" / name, "# Fix thing\n\n**Status:** open\n\nthe body\n")
    _commit(repo, "12: ticket")
    args = ["issue", "view", "12"]
    if where_branch:
        _git(repo, "checkout", "-q", "-b", "work")
        _git(repo, "rm", "-q", f"epic-tasks/{name}")
        _commit(repo, "drop 12 in the checkout")
        args += ["--branch", "arena"]
    code = cli.main(args)
    cap = capsys.readouterr()
    assert code == 0, cap.err
    assert "the body" in cap.out
