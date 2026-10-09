"""tests_bugfix/test_arena_digit_lookalikes_185.py — bug 185: `str.isdigit()` is not "`int()` takes it".

Bug: `'²'.isdigit()` is True and `int('²')` is a ValueError. Two places guarded an
`int()` with `isdigit()` and so raised on a name that merely looks numeric:

  * `rounds._matches` reads every process's command line for
    `tools.contest run --ticket NN`. One process on the box with `--ticket ²` made
    `round_alive` raise — and `run start`, `run list`, `run view`, `run rerun` and
    `issue list` all call `round_alive`, so they all ended in a traceback.
  * `tickets._ref_numbers` reads the `arena-round/NN` refs for the next free ticket
    number; a branch `arena-round/²` made `issue create` raise.

Both now skip what is not a number. (`_matches` reads what the runner's own
argparse `int` would: `07` is 7.)
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tools.arena import rounds, tickets

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _words(*tail):
    return ["python3", "-m", "tools.contest", "run", *tail]


@pytest.mark.parametrize("tail", [
    ("--ticket", "²"), ("--ticket=²"), ("--ticket", "³"), ("--ticket", "1²"), ("--ticket", "①"),
])
def test_a_ticket_that_only_looks_like_a_number_matches_no_round(tail):
    assert rounds._matches(_words(*([tail] if isinstance(tail, str) else tail)), 2) is False


def test_a_real_ticket_number_still_matches():
    assert rounds._matches(_words("--ticket", "07"), 7) is True
    assert rounds._matches(_words("--ticket=7"), 7) is True
    assert rounds._matches(_words("--ticket", "8"), 7) is False


def _fake_proc(root, pid, repo, *words):
    folder = root / str(pid)
    folder.mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"\0".join(w.encode() for w in words) + b"\0")
    os.symlink(repo, folder / "cwd")


def test_round_alive_does_not_raise_over_a_stray_process(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    proc = tmp_path / "proc"
    _fake_proc(proc, 101, repo, *_words("--ticket", "²"))
    assert rounds.round_alive(repo, 2, str(proc)) is False
    _fake_proc(proc, 102, repo, *_words("--ticket", "2"))
    assert rounds.round_alive(repo, 2, str(proc)) is True


def _git(repo, *args):
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=GIT_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_a_round_ref_that_is_not_a_number_does_not_break_the_next_number(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    for suffix in ("7", "12", "007", "²", "٣"):
        _git(repo, "branch", f"{rounds.REF_PREFIX}{suffix}")
    assert tickets._ref_numbers(repo) == {7, 12}
