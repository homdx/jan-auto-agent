"""tests_bugfix/test_arena_branch_revision_syntax_186.py — bug 186: a revision expression is not a branch.

Bug: `rounds.integration_branch` proved the branch exists with
`git rev-parse --verify -q refs/heads/<branch>`. rev-parse evaluates revision
syntax, so `--branch main~1`, `main^`, `main@{1}` or `main^{commit}` "existed" —
and were then used as the branch name: `arena run start NN --branch main~1`
built the round's base ref from the PARENT of main's tip, and `arena issue list
--branch main~1` listed the tickets of an older commit, with no word about it. A
branch is a ref, so the check is `git show-ref --verify`, which takes only an
exact ref name.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import cli, rounds

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=GIT_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "01-a.md").write_text("# A\n\n**Status:** open\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1: first")
    (repo / "epic-tasks" / "02-b.md").write_text("# B\n\n**Status:** open\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "2: second")
    _git(repo, "branch", "feature/x")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


@pytest.mark.parametrize("spelling", [
    "main~1", "main^", "main@{1}", "main^{commit}", "main~0", "feature/x^", "main:epic-tasks",
])
def test_a_revision_expression_is_not_a_branch(repo, spelling):
    with pytest.raises(rounds.RoundError, match="does not exist"):
        rounds.integration_branch(repo, spelling, {})


def test_a_profile_branch_gets_the_same_check(repo):
    with pytest.raises(rounds.RoundError, match="does not exist"):
        rounds.integration_branch(repo, None, {"branch": "main~1"})


@pytest.mark.parametrize("spelling", ["main", "feature/x"])
def test_a_real_branch_is_still_found(repo, spelling):
    assert rounds.integration_branch(repo, spelling, {}) == spelling


def test_issue_list_refuses_instead_of_reading_an_older_commit(repo, capsys):
    assert cli.main(["issue", "list", "--branch", "main~1"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_run_start_builds_no_round_ref_for_it(repo, capsys, monkeypatch):
    def no_spawn(line, cwd):  # never start the real runner from a test, fixed or not
        raise OSError("no runner in tests")

    monkeypatch.setattr(rounds, "SPAWN", no_spawn)
    for name, value in GIT_ENV.items():  # the unfixed code gets as far as `commit-tree`
        if name.startswith("GIT_"):
            monkeypatch.setenv(name, value)
    assert cli.main(["run", "start", "2", "--branch", "main~1"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert _git(repo, "for-each-ref", "refs/heads/arena-round/") == ""
