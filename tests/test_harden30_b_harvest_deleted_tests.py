"""tests/test_harden30_b_harvest_deleted_tests.py — HARDEN-30 B.

A test file counts as shipped only when it exists in the scored commit
(added or modified there, never deleted). A submission that ships no test
file gets ``no_test_file``; a run in which no root actually ran is blocked
with ``no_tests_ran``; a valid submission with a passing test stays READY.
"""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path

import pytest

from tools.contest.gates import declared_files, judge_worktree
from tools.contest.harvest import Harvest, harvest
from tools.contest.workspace import Workspace

BRIDGE = "class CollectBridge:\n    def _shrink(self, text):\n    return text[:10]\n"

TICKET = """\
# R1 — deleted tests

**File:** `pkg/thing.py`

**Also touches:** `tests/test_app.py`
"""

COLUMNS = ["ticket", "finding", "outcome", "commit", "note"]


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr.strip()}"
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(BRIDGE, encoding="utf-8")
    (repo / "epic-tasks").mkdir()
    ticket = repo / "epic-tasks" / "01-r1.md"
    ticket.write_text(TICKET, encoding="utf-8")
    (repo / "pkg").mkdir()
    _write(repo / "pkg" / "thing.py", "def thing():\n    return 1\n")
    (repo / "tests").mkdir()
    _write(repo / "tests" / "test_app.py", "def test_app():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD"), ticket


def _worktree(repo: Path, base_sha: str, tmp_path: Path, agent: str = "a") -> Workspace:
    path = tmp_path / f"wt-{agent}"
    _git(repo, "worktree", "add", "-q", str(path), base_sha)
    _git(path, "checkout", "-q", "-b", f"contest/01/{agent}", base_sha)
    return Workspace(agent=agent, path=path, branch=f"contest/01/{agent}",
                     base_sha=base_sha, kind="worktree")


def _commit(ws: Workspace, message: str) -> str:
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", message)
    return _git(ws.path, "rev-parse", "HEAD")


def _record(ws: Workspace, ticket: Path, commit: str) -> None:
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    existed = ws.progress_csv.is_file() and ws.progress_csv.stat().st_size > 0
    with open(ws.progress_csv, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not existed:
            w.writeheader()
        w.writerow({"ticket": ticket.name, "finding": "", "outcome": "FIXED",
                    "commit": commit, "note": "t"})


def _codes(h: Harvest) -> list[str]:
    return [r.code for r in h.reasons]


def _blocking(h: Harvest) -> list[str]:
    return [r.code for r in h.reasons if r.blocking]


def _reason(h: Harvest, code: str) -> object:
    return next(r for r in h.reasons if r.code == code)


def test_judge_worktree_deleted_test_not_counted(tmp_path: Path) -> None:
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _git(wt.path, "rm", "-q", "tests/test_app.py")
    _commit(wt, "delete the only test and its directory")
    declared = list(declared_files(ticket))
    row = judge_worktree(wt.agent, str(wt.path), base, declared, False)
    assert row["test_files"] == 0, row
    assert row["test_funcs"] == 0


def test_harvest_delete_only_test_rework_no_test_file(tmp_path: Path) -> None:
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _git(wt.path, "rm", "-q", "tests/test_app.py")
    _record(wt, ticket, _commit(wt, "delete the only test"))
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    assert "no_test_file" in _blocking(h)


def test_harvest_delete_one_add_one_judged_on_added(tmp_path: Path) -> None:
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _git(wt.path, "rm", "-q", "tests/test_app.py")
    _write(wt.path / "tests" / "test_new.py",
           "def test_new():\n    assert 1 + 1 == 2\n")
    _record(wt, ticket, _commit(wt, "replace the test"))
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "READY", _blocking(h)
    assert "no_test_file" not in _codes(h)


def test_harvest_rename_counts_destination_once(tmp_path: Path) -> None:
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _git(wt.path, "mv", "tests/test_app.py", "tests/test_moved.py")
    _record(wt, ticket, _commit(wt, "rename the test"))
    row = judge_worktree(wt.agent, str(wt.path), base, list(declared_files(ticket)), False)
    assert row["test_files"] == 1, row
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "READY", _blocking(h)


def test_harvest_passing_test_stays_ready(tmp_path: Path) -> None:
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    # Ship a (modified) passing test with the commit.
    _write(wt.path / "tests" / "test_app.py",
           "def test_app():\n    assert True\n\ndef test_app_two():\n    assert True\n")
    _record(wt, ticket, _commit(wt, "keep a passing test"))
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "READY", _blocking(h)


def test_harvest_all_absent_is_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test file is shipped but no root actually ran -> blocking no_tests_ran."""
    repo, base, ticket = _make_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _write(wt.path / "tests" / "test_app.py",
           "def test_app():\n    assert True\n\ndef test_app_two():\n    assert True\n")
    _record(wt, ticket, _commit(wt, "ship a test"))

    import tools.contest.harvest as harvest_mod

    def no_roots(cwd, **kwargs):
        return ("tests:absent tests_bugfix:absent .smoke_tests:absent .regression_tests:absent",
                [])

    monkeypatch.setattr(harvest_mod, "run_tests_detail", no_roots)
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    assert "tests_failed" in _blocking(h)
    assert "no tests ran" in _reason(h, "tests_failed").text
