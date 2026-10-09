"""tests_bugfix/test_arena_dirty_tasks_names_209.py — bug 209: `run start`'s "uncommitted files" names the wrong files.

Bug: `rounds._dirty_tasks` read `git status --porcelain` through `git()`, which
strips the whole output — and a modified, not yet staged file is ` M path`, its
status column a SPACE. The strip ate that space from the first line, `line[3:]` then
cut the first letter of the path, and the refusal read `epic-tasks/ has uncommitted
files: pic-tasks/01-a.md`. A name with a non-ASCII letter or a `"` came back as git's
quoted, escaped text (`"epic-tasks/03-\\320\\275....md"`), and a rename as `old -> new`.
`status --porcelain -z` is the form made to be parsed: raw paths, NUL-terminated,
a rename's old path in a record of its own.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import cli, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
TICKET = "# {t}\n\n**Status:** open\n"


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    for name in ("01-a.md", "02-b.md", "05-e.md"):
        (repo / "epic-tasks" / name).write_text(TICKET.format(t=name), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1, 2, 5: init")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def test_a_clean_folder_has_no_dirty_files(repo):
    assert rounds._dirty_tasks(repo) == []


def test_a_modified_unstaged_file_keeps_its_whole_path(repo):
    with (repo / "epic-tasks" / "01-a.md").open("a", encoding="utf-8") as fh:
        fh.write("more\n")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/01-a.md"]


def test_every_kind_of_change_is_named_by_its_raw_path(repo):
    tasks = repo / "epic-tasks"
    with (tasks / "01-a.md").open("a", encoding="utf-8") as fh:        # ` M`
        fh.write("more\n")
    (tasks / "03-новый.md").write_text("x\n", encoding="utf-8")        # `??`, non-ASCII
    (tasks / '04-say "hi".md').write_text("x\n", encoding="utf-8")     # `??`, a double quote
    _git(repo, "rm", "-q", "epic-tasks/05-e.md")                        # `D ` staged delete
    # git lists the tracked changes first and the untracked after: the order is not the contract
    assert sorted(rounds._dirty_tasks(repo)) == sorted([
        "epic-tasks/01-a.md", "epic-tasks/03-новый.md", 'epic-tasks/04-say "hi".md',
        "epic-tasks/05-e.md"])


def test_a_rename_is_named_once_by_its_new_path(repo):
    _git(repo, "mv", "epic-tasks/02-b.md", "epic-tasks/02-bb.md")
    assert rounds._dirty_tasks(repo) == ["epic-tasks/02-bb.md"]


def test_run_start_names_the_file_it_refuses_over(repo, capsys):
    with (repo / "epic-tasks" / "01-a.md").open("a", encoding="utf-8") as fh:
        fh.write("more\n")
    assert cli.main(["run", "start", "1"]) == 2
    err = capsys.readouterr().err
    assert "uncommitted files: epic-tasks/01-a.md" in err
    assert "pic-tasks" not in err.replace("epic-tasks", "")
