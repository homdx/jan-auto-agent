"""tests_bugfix/test_git_run_retries_199.py — Bug 3: run_git(retries <= 0) must not sleep on a held index.lock."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import git_run


LOCK_TEXT = (
    "fatal: unable to create '.git/index.lock': File exists\n"
)


def _make_repo(tmp):
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_retries_zero_is_one_attempt_and_no_backoff(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    waits = []
    monkeypatch.setattr(git_run, "_backoff", waits.append)

    attempts = []
    def fake(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=LOCK_TEXT + "\n")

    monkeypatch.setattr(git_run.subprocess, "run", fake)

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo, retries=0)
    assert proc.returncode == 128
    assert len(attempts) == 1
    assert waits == []


def test_retries_negative_is_one_attempt_and_no_backoff(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    waits = []
    monkeypatch.setattr(git_run, "_backoff", waits.append)

    attempts = []
    def fake(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=LOCK_TEXT + "\n")

    monkeypatch.setattr(git_run.subprocess, "run", fake)

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo, retries=-1)
    assert proc.returncode == 128
    assert len(attempts) == 1
    assert waits == []


def test_retries_one_is_one_attempt_and_no_backoff(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    waits = []
    monkeypatch.setattr(git_run, "_backoff", waits.append)

    attempts = []
    def fake(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=LOCK_TEXT + "\n")

    monkeypatch.setattr(git_run.subprocess, "run", fake)

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo, retries=1)
    assert proc.returncode == 128
    assert len(attempts) == 1
    assert waits == []
