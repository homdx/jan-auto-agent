"""tests_bugfix/test_arena_land_git_names_220.py — bug 220: `arena issue land` reads git's quoted names, and a refusal after the writes leaves the writes.

`merge.py` (AR-8) repeats bugs 206 and 209 and breaks its own promise ("a refused land
leaves ... the checkout exactly as it was"):

  * `_bench_paths` and `_tracked_changes` read `status --porcelain` without `-z`, and git
    QUOTES a name with a space or a non-ASCII letter. `issue land 5` with a bench file
    `my case.txt` ran `git add -- "\\"contest-bench/5/my case.txt\\""` — no such path —
    and a follow-up edit `my file.py` passed after `--` was refused as "changes not in
    the commit" because `"my file.py"` (with its quotes) is not `my file.py`.
  * `read_ticket` listed `arena-round/NN`'s tickets with `ls-tree --name-only`, so a
    ticket with a non-ASCII name was not found in its ref.
  * The ticket and `INDEX.md` are written BEFORE `git add` and `git commit`; when either
    refused (the case above, or a `pre-commit` hook saying no) the refusal came back
    with the ticket already `landed` on disk and staged, and the next `issue land` said
    "has uncommitted changes". The files are put back, the index cleared.
"""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from tools.arena import cli, merge, rounds

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
TICKET = "# T\n\n**Status:** open\n\n## Body\n"


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
    _git(repo, "init", "-q", "-b", "main")
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    (repo / "epic-tasks" / "05-t.md").write_text(TICKET, encoding="utf-8")
    (repo / "my file.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "5: ticket")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _bench(repo, *names):
    folder = repo / "contest-bench" / "5"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_text("ok\n", encoding="utf-8")


def _committed(repo):
    return set(_git(repo, "show", "--name-only", "--format=", "-z", "HEAD").split("\0")) - {""}


@pytest.mark.parametrize("name", ["my case.txt", "случай.txt", 'say "hi".txt'])
def test_a_bench_file_with_an_odd_name_lands(repo, capsys, name):
    _bench(repo, name, "plain.txt")
    assert cli.main(["issue", "land", "5"]) == 0
    assert _committed(repo) == {"epic-tasks/05-t.md", f"contest-bench/5/{name}", "contest-bench/5/plain.txt"}
    assert _git(repo, "status", "--porcelain") == ""


def test_a_follow_up_edit_with_a_space_in_its_name_lands(repo, capsys):
    (repo / "my file.py").write_text("x = 2\n", encoding="utf-8")
    assert cli.main(["issue", "land", "5", "--", "my file.py"]) == 0
    assert _committed(repo) == {"epic-tasks/05-t.md", "my file.py"}


def test_a_change_that_is_not_listed_is_still_refused_by_its_whole_name(repo, capsys):
    (repo / "my file.py").write_text("x = 2\n", encoding="utf-8")
    assert cli.main(["issue", "land", "5"]) == 2
    err = capsys.readouterr().err
    assert "changes not in the commit: my file.py" in err and '"' not in err


def test_tracked_changes_names_the_paths_raw(repo):
    (repo / "my file.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new file.txt").write_text("untracked\n", encoding="utf-8")
    assert merge._tracked_changes(repo) == ["my file.py"]


def test_read_ticket_finds_a_ticket_with_a_non_ascii_name_in_the_round_ref(repo):
    odd = "07-баг.md"
    (repo / "epic-tasks" / odd).write_text(TICKET, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "7: odd")
    _git(repo, "branch", f"{rounds.REF_PREFIX}7")
    _git(repo, "rm", "-q", "-f", f"epic-tasks/{odd}")
    _git(repo, "commit", "-q", "-m", "drop it from the branch")
    found = merge.read_ticket(repo, 7)
    assert found is not None and found[0] == odd and "**Status:** open" in found[1]


def _refuse_commits(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'no commits today' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_a_refused_commit_leaves_the_checkout_as_it_was(repo, capsys):
    _bench(repo, "plain.txt")
    _refuse_commits(repo)
    before = _git(repo, "rev-parse", "HEAD")
    assert cli.main(["issue", "land", "5"]) == 2
    assert _git(repo, "rev-parse", "HEAD") == before
    assert (repo / "epic-tasks" / "05-t.md").read_text(encoding="utf-8") == TICKET
    assert _git(repo, "status", "--porcelain") == "?? contest-bench/", "nothing staged, the bench untracked"


def test_the_land_can_be_run_again_once_the_refusal_is_gone(repo, capsys):
    _bench(repo, "plain.txt")
    _refuse_commits(repo)
    assert cli.main(["issue", "land", "5"]) == 2
    (repo / ".git" / "hooks" / "pre-commit").unlink()
    assert cli.main(["issue", "land", "5"]) == 0
    assert _committed(repo) == {"epic-tasks/05-t.md", "contest-bench/5/plain.txt"}
