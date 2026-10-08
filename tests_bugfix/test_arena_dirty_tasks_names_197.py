"""tests_bugfix/test_arena_dirty_tasks_names_197.py — bug 209: `run start` names a half-eaten file for an unstaged change.

Bug: `git()` strips the whole output, so the first `status --porcelain` line lost
its leading space (` M epic-tasks/01-a.md` → `M epic-tasks/…`) and `line[3:]` cut
a letter — the refusal fired but named `pic-tasks/01-a.md`, a file that does not
exist. A non-ASCII name printed with its quote escapes, a rename as `old -> new`,
and a modified tracked file under `.arena/` was counted as the operator's work
because its name no longer started with `.arena/`, so a clean checkout read
`(1 modified)`.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import cli, gitref, rounds, tickets

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, env=GIT_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clean checkout: two landed tickets, one open, and a tracked `.arena/` file."""
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    (repo / ".arena" / "locks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "01-a.md").write_text("# A\n\n**Status:** landed\n", encoding="utf-8")
    (repo / "epic-tasks" / "02-b.md").write_text("# B\n\n**Status:** landed\n", encoding="utf-8")
    (repo / "epic-tasks" / "07-а.md").write_text("# A\n\n**Status:** open\n", encoding="utf-8")
    (repo / ".arena" / "locks" / "7.pid").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1: first")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def test_a_clean_checkout_is_clean(repo):
    assert rounds._dirty_tasks(repo) == []
    assert tickets._modified(repo) == []


def test_an_unstaged_change_keeps_its_whole_name(repo):
    (repo / "epic-tasks" / "01-a.md").write_text("# A\n\n**Status:** landed\nx\n",
                                                  encoding="utf-8")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/01-a.md"]


def test_an_untracked_ticket_is_named(repo):
    (repo / "epic-tasks" / "09-new.md").write_text("# N\n", encoding="utf-8")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/09-new.md"]


def test_a_non_ascii_name_is_not_printed_with_its_escapes(repo):
    (repo / "epic-tasks" / "07-а.md").write_text("# A\n\n**Status:** open\nx\n",
                                                  encoding="utf-8")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/07-а.md"]


def test_a_rename_is_its_new_name(repo):
    _git(repo, "mv", "epic-tasks/02-b.md", "epic-tasks/02-renamed.md")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/02-renamed.md"]


def test_status_paths_returns_its_record_path_and_the_rename_new_name(repo):
    (repo / "epic-tasks" / "01-a.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "mv", "epic-tasks/02-b.md", "epic-tasks/02-renamed.md")
    _git(repo, "add", "-A")
    assert gitref.status_paths(repo, "epic-tasks/") == [
        "epic-tasks/01-a.md", "epic-tasks/02-renamed.md"]


def test_a_modified_arena_file_is_not_the_operators_work(repo):
    """Bug 44 of the same ticket: the cut name escaped the `.arena/` exclusion."""
    (repo / ".arena" / "locks" / "7.pid").write_text("2\n", encoding="utf-8")
    assert tickets._modified(repo) == []
    assert "clean" in tickets._checkout_text(repo)


def _named(err: str) -> str:
    """The file names the refusal lists — the whole string between the words."""
    return err.split("uncommitted files: ", 1)[1].split(" — ", 1)[0]


def test_run_start_names_the_file_it_refuses_for(repo, capsys, monkeypatch):
    (repo / "epic-tasks" / "01-a.md").write_text("# A\n\n**Status:** landed\nx\n",
                                                  encoding="utf-8")

    def no_spawn(line, cwd):  # never start the real runner from a test
        raise OSError("no runner in tests")

    monkeypatch.setattr(rounds, "SPAWN", no_spawn)
    assert cli.main(["run", "start", "7"]) == 2
    err = capsys.readouterr().err
    assert "uncommitted files" in err
    assert _named(err) == "epic-tasks/01-a.md"


def test_run_start_names_a_renamed_file(repo, capsys, monkeypatch):
    _git(repo, "mv", "epic-tasks/02-b.md", "epic-tasks/02-renamed.md")

    def no_spawn(line, cwd):
        raise OSError("no runner in tests")

    monkeypatch.setattr(rounds, "SPAWN", no_spawn)
    assert cli.main(["run", "start", "7"]) == 2
    err = capsys.readouterr().err
    assert _named(err) == "epic-tasks/02-renamed.md"
