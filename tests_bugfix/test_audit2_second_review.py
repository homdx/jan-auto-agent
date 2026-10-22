"""tests_bugfix/test_audit2_second_review.py — second review round (MIMO-1..5, GEM-dup).

MIMO-1 kilo serve lost its port between _free_port() and its own bind: now retried.
MIMO-2 ticket_status round: the re-read at --base had no None / missing-file check.
MIMO-3 judge_worktree trusted a bare `.git` entry: an empty `.git` file scored "0 commits".
MIMO-4 harvest's porcelain parse only stripped quotes, leaving git's C escapes.
MIMO-5 gates.git raised OSError (no git binary) past its documented "" contract.
GEM    testcache's _VALUE_OPTIONS listed "--durations-min" twice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.contest import gates, harvest, kilo_client, testcache

REPO = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


# ── MIMO-1 ──────────────────────────────────────────────────────────────────

def test_spawn_retries_on_a_lost_port_only(monkeypatch, tmp_path):
    calls = []

    def once(cls, binary, **kw):
        calls.append(kw)
        if len(calls) < 3:
            raise kilo_client.KiloServerError("exited", log_tail="Error: EADDRINUSE 127.0.0.1:4242")
        return "server"

    monkeypatch.setattr(kilo_client.KiloServer, "_spawn_once", classmethod(once))
    assert kilo_client.KiloServer.spawn("kilo", log_path=str(tmp_path / "l")) == "server"
    assert len(calls) == 3

    calls.clear()

    def broken(cls, binary, **kw):
        calls.append(kw)
        raise kilo_client.KiloServerError("exited", log_tail="config error")

    monkeypatch.setattr(kilo_client.KiloServer, "_spawn_once", classmethod(broken))
    with pytest.raises(kilo_client.KiloServerError):
        kilo_client.KiloServer.spawn("kilo", log_path=str(tmp_path / "l"))
    assert len(calls) == 1, "a failure that is not a lost port is not retried"


# ── MIMO-2 ──────────────────────────────────────────────────────────────────

def test_round_at_a_base_without_a_status_line_is_a_clean_error(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "epic-tasks").mkdir()
    ticket = repo / "epic-tasks" / "48-x.md"
    ticket.write_text("# KC-9 x\n\nno status yet\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    ticket.write_text("# KC-9 x\n\n**Status:** queued\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "status")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "ticket_status.py"),
                        "round", "48", "--repo", str(repo), "--base", base, "--no-commit"],
                       capture_output=True, text=True, env=ENV)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr
    assert "no **Status:** line" in r.stderr


# ── MIMO-3 / MIMO-5 ─────────────────────────────────────────────────────────

def test_an_empty_dot_git_file_is_not_a_worktree(tmp_path):
    (tmp_path / ".git").write_text("")
    row = gates.judge_worktree("a", str(tmp_path), "HEAD", [], want_tests=False)
    assert row["notes"] == "not a git worktree"
    assert "commits" not in row


def test_a_real_worktree_is_still_judged(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "c")
    row = gates.judge_worktree("a", str(tmp_path), "HEAD", [], want_tests=False)
    assert row.get("commits") == 0


def test_gates_git_without_a_git_binary_answers_empty(monkeypatch, tmp_path):
    def no_git(*a, **k):
        raise FileNotFoundError(2, "git")

    monkeypatch.setattr(gates, "run_git", no_git)
    assert gates.git(str(tmp_path), "status") == ""
    with pytest.raises(RuntimeError):
        gates.git(str(tmp_path), "status", check=True)


# ── MIMO-4 ──────────────────────────────────────────────────────────────────

def test_non_ascii_ground_dir_is_filtered(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "seed").write_text("s")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "c")
    ground = tmp_path / "runs" / "agënt"
    ground.mkdir(parents=True)
    (ground / "PROGRESS.csv").write_text("x")
    (tmp_path / "mine.py").write_text("y")
    ws = SimpleNamespace(path=tmp_path, progress_csv=ground / "PROGRESS.csv")
    lines = harvest._status_lines(ws)
    assert any("mine.py" in line for line in lines)
    assert not any("PROGRESS" in line or "\\303" in line for line in lines), lines


def test_unquote_git_path():
    assert harvest._unquote_git_path('"runs/ag\\303\\253nt/P.csv"') == "runs/agënt/P.csv"
    assert harvest._unquote_git_path('"a\\"b\\\\c"') == 'a"b\\c'
    assert harvest._unquote_git_path("plain/x.py") == "plain/x.py"


# ── GEM: duplicate option ───────────────────────────────────────────────────

def test_value_options_source_has_no_duplicates():
    import ast
    src = Path(testcache.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_VALUE_OPTIONS" for t in node.targets):
            items = [e.value for e in node.value.args[0].elts]
            assert len(items) == len(set(items))
            return
    pytest.fail("_VALUE_OPTIONS not found")
