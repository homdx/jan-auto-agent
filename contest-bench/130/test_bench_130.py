"""contest-bench/130 — the real bugs in tools/contest/harvest.py, as shared tests.

The agent writes runs/<agent>/PROGRESS.csv from its own shell, so the harvest
reads a file it does not control. Each case below is a file an agent can leave
behind; on the base every one raises out of `harvest()` and kills the round's
scoring of that agent. Weighted cases:
  * real   — reachable from an agent-written PROGRESS.csv (the ticket's point);
  * guard  — git that cannot run (OSError from subprocess), defensive only.
Run from a worktree of the entry: pytest contest-bench/130/test_bench_130.py
"""
from __future__ import annotations

import subprocess

import pytest

from tests.test_contest_harvest import (  # noqa: F401 — the round_ fixture
    _accepting, _codes, _record, _worktree, round_,
)
from tools.contest import harvest as hv

HEADER = "ticket,finding,outcome,commit,note\n"


def _write(ws, data: bytes) -> None:
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    ws.progress_csv.write_bytes(data)


@pytest.mark.real
def test_real_invalid_utf8_is_no_claim(round_, tmp_path):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    _write(ws, HEADER.encode() + b"01-r1.md,,DONE,\xff\xfe,\n")
    h = hv.harvest(ws, ticket)
    assert h.verdict != "READY"


@pytest.mark.real
def test_real_nul_byte_is_no_claim(round_, tmp_path):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    _write(ws, HEADER.encode() + b"01-r1.md,,DONE,\x00,\n")
    h = hv.harvest(ws, ticket)
    assert h.verdict != "READY"


@pytest.mark.real
def test_real_oversized_field_is_no_claim(round_, tmp_path):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    _write(ws, HEADER.encode() + b'01-r1.md,,DONE,,"' + b"x" * 200_000 + b'"\n')
    h = hv.harvest(ws, ticket)
    assert h.verdict != "READY"


@pytest.mark.real
def test_real_short_row_without_commit(round_, tmp_path):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    _write(ws, (HEADER + "01-r1.md,,DONE\n").encode())
    h = hv.harvest(ws, ticket)
    assert "no_commit" in _codes(h)
    assert "progress_not_done" not in _codes(h)


@pytest.mark.real
def test_real_short_row_ticket_only(round_, tmp_path):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    _write(ws, (HEADER + "01-r1.md\n").encode())
    h = hv.harvest(ws, ticket)
    assert "progress_not_done" in _codes(h)
    assert "no_commit" in _codes(h)


@pytest.mark.real
def test_real_good_claim_still_ready(round_, tmp_path):
    """No over-fix: a well-formed claim after the fix still scores READY."""
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    sha = _accepting(ws)
    _record(ws, "01-r1.md", "DONE", sha)
    h = hv.harvest(ws, ticket)
    assert h.verdict == "READY", _codes(h)


@pytest.mark.guard
def test_guard_git_cannot_run(round_, tmp_path, monkeypatch):
    repo, base, ticket = round_
    ws = _worktree(repo, base, tmp_path)
    sha = _accepting(ws)
    _record(ws, "01-r1.md", "DONE", sha)

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(hv.subprocess, "run", boom)
    import tools.contest.gates as gates
    monkeypatch.setattr(gates, "run_git", boom, raising=False)
    h = hv.harvest(ws, ticket)
    assert h.verdict != "READY"
