"""Round 135 acceptance bench — a failed ``git status -- epic-tasks`` is refused, not read as clean.

Self-contained: a plain git repo and a bench-owned ``_git`` stand-in, so the
entry's own tests cannot change the outcome.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.contest.workspace as ws
from tools.contest.roster import AgentSpec, ContestConfig
from tools.contest.workspace import WorkspaceError

STATUS = ["status", "--porcelain", "--untracked-files=all", "--", "epic-tasks"]


def _g(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "config", "user.email", "t@example.com")
    _g(repo, "config", "user.name", "T")
    (repo / "epic-tasks" / "01.md").write_text("t\n")
    (repo / "readme.txt").write_text("r\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "r0")
    return repo


@pytest.fixture
def config(tmp_path):
    rounds = tmp_path / "rounds"
    rounds.mkdir()
    return ContestConfig(
        rounds_dir=str(rounds),
        workspace_kind="worktree",
        agents=(AgentSpec(name="a1", provider_id="p", model_id="m"),),
    )


def _fail_status(monkeypatch, *, code=128, stdout="", stderr="fatal: index.lock"):
    real = ws._git

    def fake(cwd, args, *, check=True):
        if list(args) == STATUS:
            return subprocess.CompletedProcess(list(args), code, stdout=stdout, stderr=stderr)
        return real(cwd, args, check=check)

    monkeypatch.setattr(ws, "_git", fake)


@pytest.mark.parametrize("code", [128, 1, 129])
def test_failed_status_raises_naming_epic_tasks_and_code(repo, monkeypatch, code):
    _fail_status(monkeypatch, code=code)
    with pytest.raises(WorkspaceError) as ei:
        ws._check_base_and_epic_tasks(repo, "HEAD")
    msg = str(ei.value)
    assert "epic-tasks" in msg
    assert str(code) in msg
    assert "index.lock" in msg
    assert msg != ws._EPIC_TASKS_DIRTY_REASON


def test_failed_status_with_stdout_only_still_raises(repo, monkeypatch):
    _fail_status(monkeypatch, stdout="", stderr="")
    with pytest.raises(WorkspaceError):
        ws._check_base_and_epic_tasks(repo, "HEAD")


def test_failed_status_stderr_empty_falls_back_to_stdout(repo, monkeypatch):
    _fail_status(monkeypatch, stdout="weird-out", stderr="")
    with pytest.raises(WorkspaceError) as ei:
        ws._check_base_and_epic_tasks(repo, "HEAD")
    assert "weird-out" in str(ei.value)


def test_prepare_round_refuses_and_creates_no_worktree(repo, config, monkeypatch):
    _fail_status(monkeypatch)
    with pytest.raises(WorkspaceError):
        ws.prepare_round(repo, config, 135, "HEAD")
    rounds = Path(config.rounds_dir)
    assert not any(p.is_dir() for p in rounds.rglob("*") if (p / ".git").exists())
    assert _g(repo, "worktree", "list").count("\n") == 0


def test_clean_repo_returns_base_sha(repo):
    assert ws._check_base_and_epic_tasks(repo, "HEAD") == _g(repo, "rev-parse", "HEAD")


def test_dirty_epic_tasks_still_dirty_reason(repo):
    (repo / "epic-tasks" / "x.md").write_text("x\n")
    with pytest.raises(WorkspaceError) as ei:
        ws._check_base_and_epic_tasks(repo, "HEAD")
    assert str(ei.value) == ws._EPIC_TASKS_DIRTY_REASON


def test_unresolvable_base_unchanged(repo):
    with pytest.raises(WorkspaceError, match="does not resolve"):
        ws._check_base_and_epic_tasks(repo, "no-such-ref")
