"""Round 134 acceptance bench — commit_ticket checks out the leg branch before copying scripts.

Self-contained: a plain git repo, no collect run, the helpers copied here. The
scripts come from a bench-owned ``scripts_dir`` so the entry's own scripts/
cannot change the outcome.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest import draft as draft_mod
from tools.contest.draft import CONTEST_SCRIPTS, LEG_BRANCH, commit_ticket


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


@pytest.fixture
def scripts(tmp_path):
    d = tmp_path / "src-scripts"
    d.mkdir()
    for rel in CONTEST_SCRIPTS:
        (d / Path(rel).name).write_text(f"# bench copy of {rel}\n")
    return d


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "readme.txt").write_text("r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "r0")
    return repo


def _ticket(repo, name):
    path = repo / "epic-tasks" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"# {name}\n")
    return path


def _first(repo, scripts):
    r = commit_ticket(repo, _ticket(repo, "01-t.md"), scripts_dir=scripts)
    assert r.ok, r
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == LEG_BRANCH
    committed = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()
    assert set(committed) == {"epic-tasks/01-t.md", *CONTEST_SCRIPTS}
    _git(repo, "checkout", "-q", "main")
    for rel in CONTEST_SCRIPTS:
        assert not (repo / rel).exists()


def test_second_draft_from_main_lands_the_ticket_alone(repo, scripts):
    _first(repo, scripts)
    r = commit_ticket(repo, _ticket(repo, "02-t.md"), scripts_dir=scripts)
    assert r.ok, r
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == LEG_BRANCH
    assert _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == ["epic-tasks/02-t.md"]
    assert _git(repo, "status", "--porcelain", "--", "scripts") == ""


def test_third_draft_still_lands_alone(repo, scripts):
    _first(repo, scripts)
    assert commit_ticket(repo, _ticket(repo, "02-t.md"), scripts_dir=scripts).ok
    _git(repo, "checkout", "-q", "main")
    r = commit_ticket(repo, _ticket(repo, "03-t.md"), scripts_dir=scripts)
    assert r.ok, r
    assert _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == ["epic-tasks/03-t.md"]


def test_failed_checkout_leaves_no_script_copy(repo, scripts, monkeypatch):
    _first(repo, scripts)
    real = draft_mod._git

    def fake(cwd, *args, **kw):
        if args and args[0] == "checkout":
            return 1, "", "bench says no"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(draft_mod, "_git", fake)
    r = commit_ticket(repo, _ticket(repo, "02-t.md"), scripts_dir=scripts)
    assert not r.ok
    assert "cannot check out" in r.message
    for rel in CONTEST_SCRIPTS:
        assert not (repo / rel).exists(), rel


def test_untracked_blocker_refused_without_script_copy(repo, scripts):
    _first(repo, scripts)
    # a file tracked on the leg branch, untracked on main: checkout must fail
    (repo / "epic-tasks").mkdir(exist_ok=True)
    (repo / "epic-tasks" / "01-t.md").write_text("in the way\n")
    r = commit_ticket(repo, _ticket(repo, "02-t.md"), scripts_dir=scripts)
    assert not r.ok
    for rel in CONTEST_SCRIPTS:
        assert not (repo / rel).exists(), rel


def test_first_draft_still_copies_both_scripts(repo, scripts):
    _first(repo, scripts)


def test_existing_script_on_leg_branch_is_kept(repo, scripts):
    _first(repo, scripts)
    _git(repo, "checkout", "-q", LEG_BRANCH)
    target = repo / CONTEST_SCRIPTS[0]
    target.write_text("# operator's own\n")
    _git(repo, "commit", "-q", "-am", "own")
    _git(repo, "checkout", "-q", "main")
    assert commit_ticket(repo, _ticket(repo, "02-t.md"), scripts_dir=scripts).ok
    assert target.read_text() == "# operator's own\n"


def test_dirty_repo_still_refused_before_anything(repo, scripts):
    (repo / "readme.txt").write_text("dirty\n")
    r = commit_ticket(repo, _ticket(repo, "01-t.md"), scripts_dir=scripts)
    assert not r.ok
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    for rel in CONTEST_SCRIPTS:
        assert not (repo / rel).exists()
