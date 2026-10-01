"""Round 132 acceptance bench — a failed rev-list/status is a refusal, not a clean tree.

Self-contained: the repo and config helpers are copied, not imported from the
entry's own tests. A failing git is made by wrapping ``workspace._git``: the
named subcommand answers rc 128 with empty stdout, everything else is real git.
Both workspace kinds (worktree, KC-59 clone) are covered.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest import workspace as ws_mod
from tools.contest.roster import AgentSpec, ContestConfig
from tools.contest.workspace import WorkspaceError, prepare_round


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "readme.txt").write_text("r0\n")
    (repo / "epic-tasks").mkdir()
    (repo / "epic-tasks" / "a.md").write_text("task\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "r0")
    (repo / "readme.txt").write_text("r1\n")
    _git(repo, "commit", "-q", "-am", "r1")
    return repo


def _config(tmp_path, kind):
    rounds = tmp_path / "rounds"
    rounds.mkdir(exist_ok=True)
    return ContestConfig(
        rounds_dir=str(rounds), workspace_kind=kind,
        tmp_roots=(str(tmp_path / "tmp") + "/*",),
        agents=(AgentSpec(name="laguna", provider_id="p", model_id="m"),),
    )


def _base(repo):
    return _git(repo, "rev-parse", "HEAD~1").strip()


def _fail(monkeypatch, subcommand):
    """Every ``git <subcommand>`` the workspace module runs exits 128, silently."""
    real = ws_mod._git

    def fake(cwd, args, *a, **kw):
        if args and args[0] == subcommand:
            proc = subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bench says no")
            if kw.get("check", True):
                raise WorkspaceError(f"git {' '.join(args)} failed (128): fatal: bench says no")
            return proc
        return real(cwd, args, *a, **kw)

    monkeypatch.setattr(ws_mod, "_git", fake)


KINDS = ["worktree", "clone"]


def _laguna(repo, config, base, **kw):
    (ws,) = prepare_round(repo, config, 40, base, **kw)
    return ws


# ── the unit level ────────────────────────────────────────────────────────────

def test_commits_above_raises_on_a_failed_rev_list(repo, monkeypatch):
    _fail(monkeypatch, "rev-list")
    with pytest.raises(WorkspaceError) as exc:
        ws_mod._commits_above(repo, _base(repo))
    assert "--fresh" in str(exc.value)
    assert str(repo) in str(exc.value)


def test_dirty_outside_runs_raises_on_a_failed_status(repo, monkeypatch):
    _fail(monkeypatch, "status")
    with pytest.raises(WorkspaceError) as exc:
        ws_mod._dirty_outside_runs(repo)
    assert "--fresh" in str(exc.value)
    assert str(repo) in str(exc.value)


def test_success_paths_unchanged(repo):
    base = _base(repo)
    assert ws_mod._commits_above(repo, base) == 1
    assert ws_mod._dirty_outside_runs(repo) == []
    (repo / "x.txt").write_text("x")
    (repo / "runs").mkdir()
    (repo / "runs" / "p.csv").write_text("p")
    assert ws_mod._dirty_outside_runs(repo) == ["?? x.txt"]


# ── prepare_round, both kinds ────────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_commit_kept_when_rev_list_fails(repo, tmp_path, monkeypatch, kind):
    config = _config(tmp_path, kind)
    base = _base(repo)
    ws = _laguna(repo, config, base)
    (ws.path / "thing.py").write_text("42\n")
    _git(ws.path, "add", "thing.py")
    _git(ws.path, "commit", "-q", "-m", "work")
    head = _git(ws.path, "rev-parse", "HEAD").strip()

    _fail(monkeypatch, "rev-list")
    with pytest.raises(WorkspaceError):
        prepare_round(repo, config, 40, base)
    assert _git(ws.path, "rev-parse", "HEAD").strip() == head
    assert (ws.path / "thing.py").exists()


@pytest.mark.parametrize("kind", KINDS)
def test_edit_kept_when_status_fails(repo, tmp_path, monkeypatch, kind):
    config = _config(tmp_path, kind)
    base = _base(repo)
    ws = _laguna(repo, config, base)
    (ws.path / "readme.txt").write_text("edited\n")
    (ws.path / "new.py").write_text("n\n")

    _fail(monkeypatch, "status")
    with pytest.raises(WorkspaceError):
        prepare_round(repo, config, 40, base)
    assert (ws.path / "readme.txt").read_text() == "edited\n"
    assert (ws.path / "new.py").exists()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("sub", ["rev-list", "status"])
def test_clean_workspace_still_refused_when_git_cannot_read_it(repo, tmp_path, monkeypatch, kind, sub):
    config = _config(tmp_path, kind)
    base = _base(repo)
    _laguna(repo, config, base)
    _fail(monkeypatch, sub)
    with pytest.raises(WorkspaceError) as exc:
        prepare_round(repo, config, 40, base)
    assert "--fresh" in str(exc.value)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("sub", ["rev-list", "status"])
def test_fresh_resets_even_when_git_cannot_read_it(repo, tmp_path, monkeypatch, kind, sub):
    config = _config(tmp_path, kind)
    base = _base(repo)
    ws = _laguna(repo, config, base)
    (ws.path / "thing.py").write_text("42\n")
    _git(ws.path, "add", "thing.py")
    _git(ws.path, "commit", "-q", "-m", "work")
    (ws.path / "readme.txt").write_text("edited\n")

    _fail(monkeypatch, sub)
    ws2 = _laguna(repo, config, base, force=True)
    monkeypatch.undo()
    assert _git(ws2.path, "rev-parse", "HEAD").strip() == base
    assert not (ws2.path / "thing.py").exists()
    assert _git(ws2.path, "status", "--porcelain").strip() == ""


@pytest.mark.parametrize("kind", KINDS)
def test_clean_rerun_still_resets_silently(repo, tmp_path, kind):
    config = _config(tmp_path, kind)
    base = _base(repo)
    ws = _laguna(repo, config, base)
    (ws.path / "runs" / "laguna").mkdir(parents=True, exist_ok=True)
    (ws.path / "runs" / "laguna" / "PROGRESS.csv").write_text("x\n")
    ws2 = _laguna(repo, config, base)
    assert _git(ws2.path, "rev-parse", "HEAD").strip() == base
    assert not list((ws2.path / "runs" / "laguna").iterdir())


@pytest.mark.parametrize("kind", KINDS)
def test_real_work_still_refused_without_fresh(repo, tmp_path, kind):
    config = _config(tmp_path, kind)
    base = _base(repo)
    ws = _laguna(repo, config, base)
    (ws.path / "thing.py").write_text("42\n")
    with pytest.raises(WorkspaceError) as exc:
        prepare_round(repo, config, 40, base)
    assert "thing.py" in str(exc.value)
