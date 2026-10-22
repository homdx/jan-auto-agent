"""tests_bugfix/test_audit_arena_lock_and_state.py — AUDIT-5, AUDIT-20.

AUDIT-5: two `arena run start NN` could both pass the liveness check, both
write the one lock file and start two runners; whichever exited first then
unlinked the lock the other still owned. The lock is now claimed with O_EXCL
and only its owner removes it.

AUDIT-20: `run rerun` wrote the live state.json in place; a failed write left
it truncated. It is now written beside it and swapped in.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds

INI = """[contest]
out_dir = out

[contest.agent.a]
model = test/a
"""


class _Child:
    pid = 4242

    def wait(self):
        return 0


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ── AUDIT-5 ─────────────────────────────────────────────────────────────────

def test_lock_held_by_a_live_process_refuses_a_second_runner(tmp_path, monkeypatch, capsys):
    locks = tmp_path / ".arena" / "locks"
    locks.mkdir(parents=True)
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (locks / "7.pid").write_text(str(holder.pid), encoding="utf-8")
        spawned = []
        monkeypatch.setattr(rounds, "SPAWN", lambda line, cwd: spawned.append(line) or _Child())
        code = rounds._run_child(tmp_path, 7, ["x"], tmp_path / "state.json")
        assert code != rounds.EXIT_OK
        assert spawned == [], "a second runner was started over a live one"
        assert (locks / "7.pid").read_text(encoding="utf-8") == str(holder.pid), \
            "another runner's lock was overwritten or removed"
        assert "already running" in capsys.readouterr().err
    finally:
        holder.kill()
        holder.wait()


def test_a_stale_lock_is_taken_over_and_released(tmp_path, monkeypatch):
    locks = tmp_path / ".arena" / "locks"
    locks.mkdir(parents=True)
    (locks / "7.pid").write_text(str(_dead_pid()), encoding="utf-8")
    monkeypatch.setattr(rounds, "SPAWN", lambda line, cwd: _Child())
    assert rounds._run_child(tmp_path, 7, ["x"], tmp_path / "state.json") == rounds.EXIT_OK
    assert not (locks / "7.pid").exists()


def test_exit_never_removes_a_lock_another_runner_now_owns(tmp_path, monkeypatch):
    locks = tmp_path / ".arena" / "locks"
    other = str(os.getppid())

    class _Child2:
        pid = 4343

        def wait(self):
            # meanwhile the lock was taken over by another runner
            (locks / "7.pid").write_text(other, encoding="utf-8")
            return 0

    monkeypatch.setattr(rounds, "SPAWN", lambda line, cwd: _Child2())
    rounds._run_child(tmp_path, 7, ["x"], tmp_path / "state.json")
    assert (locks / "7.pid").read_text(encoding="utf-8") == other


def test_claim_is_exclusive(tmp_path):
    lock = tmp_path / "1.pid"
    assert rounds._claim_lock(lock) is True
    assert rounds._claim_lock(lock) is False      # our own pid is alive
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


# ── AUDIT-20 ────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    (repo).mkdir()
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def test_a_failed_state_write_leaves_the_old_state_whole(repo, monkeypatch, capsys):
    folder = repo / "out" / "07"
    folder.mkdir(parents=True)
    state = folder / "state.json"
    state.write_text(json.dumps({
        "round_no": 1, "ticket": "t", "base_sha": "0" * 40,
        "agents": [{"agent": {"name": "a", "provider_id": "p", "model_id": "m"},
                    "state": "STALLED", "attempt": 1, "last_error": "x"}]}),
        encoding="utf-8")
    before = state.read_bytes()
    spawned = []
    monkeypatch.setattr(rounds, "SPAWN", lambda line, cwd: spawned.append(line) or _Child())
    real_replace = os.replace

    def full_disk(src, dst):
        if Path(dst) == state:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(rounds.os, "replace", full_disk)
    code = cli.main(["run", "rerun", "7", "--failed"])
    capsys.readouterr()
    assert code != 0
    assert spawned == []
    assert state.read_bytes() == before, "the live state.json was damaged"
    assert sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp")) == []
