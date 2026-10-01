"""Round 133 acceptance bench — four gates.py fixes.

Self-contained: no entry test helper is imported. pytest is never really run
by the gates under test: `_pytest`'s subprocess and `run_tests_detail`'s
`_pytest` are stubbed, so the bench reads only what the code decides.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest import gates


# ── 1. --timeout only with pytest-timeout installed ─────────────────────────

def _capture_cmd(monkeypatch, have_timeout):
    real = importlib.util.find_spec
    seen = []

    def find_spec(name, *a, **kw):
        if name == "pytest_timeout":
            return object() if have_timeout else None
        return real(name, *a, **kw)

    def run(cmd, *a, **kw):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(gates.subprocess, "run", run)
    gates._pytest("/nonexistent", "tests")
    assert seen, "pytest was not started"
    return seen[-1]


def test_no_timeout_flag_without_the_plugin(monkeypatch):
    assert not any(a.startswith("--timeout") for a in _capture_cmd(monkeypatch, False))


def test_timeout_flag_with_the_plugin(monkeypatch):
    assert "--timeout=180" in _capture_cmd(monkeypatch, True)


def test_requirements_mention_pytest_timeout():
    text = (REPO_ROOT / "requirements.txt").read_text()
    assert "pytest-timeout" in text
    assert "tree-sitter>=0.23" in text


# ── 2. a wrapped **Also touches:** line ─────────────────────────────────────

WRAPPED = """# 9 — t

**Status:** open
**File:** `tools/contest/gates.py`
**Symbol:** `x`
**Also touches:** `tests/test_contest_cli.py`,
  `contest-bench/kc76/`, `docs/a.md`
**Round:** 9 `not/declared.py`

## Why

`also/not_declared.py`
"""

BLANK_ENDS = """**File:** `a/b.py`
**Also touches:** `tests/test_b.py`
`c/d.py`

`e/not.py`
"""


def test_wrapped_also_touches_is_read_whole():
    assert gates._declared_paths(WRAPPED) == (
        "tools/contest/gates.py", "tests/test_contest_cli.py", "contest-bench/kc76/", "docs/a.md")


def test_blank_line_ends_the_field():
    assert gates._declared_paths(BLANK_ENDS) == ("a/b.py", "tests/test_b.py", "c/d.py")


def test_one_line_field_unchanged():
    body = "**File:** `a/b.py`\n**Also touches:** `tests/test_b.py`, `c/`\n**Round:** 1\n"
    assert gates._declared_paths(body) == ("a/b.py", "tests/test_b.py", "c/")


def test_plain_file_and_dash_unchanged():
    assert gates._declared_paths("**File:** a/b.py\n**Also touches:** —\n") == ("a/b.py",)
    assert gates._declared_paths("**File:** —\n") == ()


# ── 3. judge_worktree: dir prefix and renames ───────────────────────────────

def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "wt"
    (r / "tools").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@e")
    _git(r, "config", "user.name", "T")
    (r / "tools" / "old.py").write_text("".join(f"line {i}\n" for i in range(40)))
    (r / "readme").write_text("r\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _judge(repo, declared):
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    return gates.judge_worktree("a", str(repo), base, declared, False)


def test_file_under_a_declared_dir_is_on_ticket(repo):
    (repo / "contest-bench" / "kc76").mkdir(parents=True)
    (repo / "contest-bench" / "kc76" / "x.py").write_text("x\n")
    (repo / "tools" / "a.py").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "w")
    row = _judge(repo, ("tools/a.py", "contest-bench/kc76/"))
    assert row["off_ticket"] == 0, row


def test_a_sibling_dir_is_still_off_ticket(repo):
    (repo / "contest-bench" / "kc761").mkdir(parents=True)
    (repo / "contest-bench" / "kc761" / "x.py").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "w")
    row = _judge(repo, ("tools/a.py", "contest-bench/kc76/"))
    assert row["off_ticket"] == 1, row


def test_undeclared_file_still_off_ticket(repo):
    (repo / "tools" / "a.py").write_text("a\n")
    (repo / "readme").write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "w")
    row = _judge(repo, ("tools/a.py",))
    assert row["off_ticket"] == 1 and "readme" in row["off_ticket_files"], row


def test_renamed_into_a_declared_path_is_on_ticket(repo):
    _git(repo, "mv", "tools/old.py", "tools/a.py")
    _git(repo, "commit", "-q", "-m", "rename")
    row = _judge(repo, ("tools/a.py",))
    assert row["off_ticket"] == 0, row
    assert "=>" not in row["off_ticket_files"]


def test_renamed_test_file_counts_its_test_funcs(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_a():\n    pass\n" * 1 + "# pad\n" * 30)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "t")
    base_more = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "tests/test_old.py", "tests/test_new.py")
    _git(repo, "commit", "-q", "-m", "rename")
    row = gates.judge_worktree("a", str(repo), base_more, ("tools/a.py",), False)
    assert row["test_funcs"] == 1, row


# ── 4. a lost node id is not PASS* ──────────────────────────────────────────

def _detail(tmp_path, monkeypatch, summary_lines):
    (tmp_path / "tests").mkdir()
    calls = []

    def fake(cwd, *args, budget=0.0):
        calls.append(args)
        if len(calls) == 1:
            out = "\n".join(summary_lines)
            return subprocess.CompletedProcess(["pytest"], 1, out, "")
        return subprocess.CompletedProcess(["pytest"], 0, "1 passed in 0.1s", "")

    monkeypatch.setattr(gates, "_pytest", fake)
    summary, _tail = gates.run_tests_detail(str(tmp_path))
    return summary


def test_unparseable_id_is_red_not_flaky(tmp_path, monkeypatch):
    summary = _detail(tmp_path, monkeypatch, [
        "FAILED tests/test_x.py::test_a - boom",
        "FAILED tests/test_x.py::test_b[a b] - boom",
        "2 failed, 3 passed in 0.5s",
    ])
    assert "tests:2✗" in summary, summary
    assert "PASS*" not in summary


def test_all_ids_parsed_still_flaky(tmp_path, monkeypatch):
    summary = _detail(tmp_path, monkeypatch, [
        "FAILED tests/test_x.py::test_a - boom",
        "FAILED tests/test_x.py::test_b - boom",
        "2 failed, 3 passed in 0.5s",
    ])
    assert "tests:PASS*2" in summary, summary


def test_a_plain_deletion_of_an_undeclared_file_is_off_ticket(repo):
    # only the old side of a *rename* is exempt, not every deletion
    _git(repo, "rm", "-q", "readme")
    (repo / "tools" / "a.py").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "w")
    row = _judge(repo, ("tools/a.py",))
    assert row["off_ticket"] == 1 and "readme" in row["off_ticket_files"], row
