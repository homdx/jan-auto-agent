"""REL reports: bounded command output, no inherited stdin, auth-scheme redaction, symlinked lock dirs, ticket and round-lock races."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools import file_lock
from tools.arena import rounds
from tools.auto import executor as executor_mod
from tools.auto.executor import Executor, _redact_command
from tools.auto.ticket_store import TicketStore, make_ticket

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell and /proc")


# REL-1 #1: a runaway writer is killed at the file cap, not buffered into memory.
@posix_only
def test_runaway_output_is_capped_and_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_mod, "_MAX_OUTPUT_FILE_BYTES", 2 * 1024 * 1024)
    start = time.monotonic()
    r = Executor(base_dir=tmp_path, timeout_sec=30)._execute("yes", cwd=tmp_path, task_id="T")
    assert time.monotonic() - start < 20
    assert not r.timed_out and r.exit_code != 0
    assert len(r.stdout) < executor_mod._MAX_OUTPUT_CHARS + 200
    assert "command killed: its output passed" in r.stderr


@posix_only
def test_output_text_exit_code_and_timeout_are_kept(tmp_path):
    ex = Executor(base_dir=tmp_path, timeout_sec=10)
    r = ex._execute("printf 'a\\r\\nb'; printf err >&2; exit 3", cwd=tmp_path, task_id="T")
    assert (r.stdout, r.stderr, r.exit_code) == ("a\nb", "err", 3)
    big = ex._execute("python3 -c \"print('x' * 100000)\"", cwd=tmp_path, task_id="T")
    assert big.stdout.startswith("x" * 1000) and "[truncated —" in big.stdout
    slow = Executor(base_dir=tmp_path, timeout_sec=1)._execute("echo hi; sleep 5", cwd=tmp_path, task_id="T")
    assert slow.timed_out and slow.stdout == "hi\n"


# REL-1 #2: the command never reads the agent's stdin.
@posix_only
def test_command_stdin_is_empty(tmp_path):
    r = Executor(base_dir=tmp_path, timeout_sec=10)._execute(
        "python3 -c \"input()\"", cwd=tmp_path, task_id="T")
    assert not r.timed_out and r.exit_code != 0


# REL-2: an authorization option hides its scheme and credential together.
@pytest.mark.parametrize("cmd,want", [
    ("tool --authorization Bearer xyz123", "tool --authorization [REDACTED]"),
    ('tool --authorization "Bearer xyz123" -v', 'tool --authorization "[REDACTED]" -v'),
    ("tool --auth Basic dXNlcjpwYXNz", "tool --auth [REDACTED]"),
    ("tool --auth=Basic dXNl x", "tool --auth=[REDACTED] x"),
    ("tool --authorization xyz123", "tool --authorization [REDACTED]"),
    ("curl --proxy-authorization 'Basic abc' http://x", "curl --proxy-authorization '[REDACTED]' http://x"),
    ("tool --auth-type basic", "tool --auth-type basic"),
])
def test_authorization_option_is_redacted_whole(cmd, want):
    assert _redact_command(cmd) == want


# REL-3: a symlinked parent directory shares the lock of the real one.
@posix_only
def test_lock_through_a_symlinked_directory_is_the_same_lock(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    entered = threading.Event()

    def _second():
        with file_lock.locked(real / "f.txt"):
            entered.set()

    with file_lock.locked(tmp_path / "linked" / "f.txt"):
        t = threading.Thread(target=_second, daemon=True)
        t.start()
        assert not entered.wait(0.5), "the second spelling took the lock while it was held"
    assert entered.wait(5)
    t.join(5)


# REL-5 TK-1: overlapping status and body updates both land.
def test_overlapping_ticket_updates_keep_both_fields(tmp_path):
    store = TicketStore(tmp_path / "tickets")
    for i in range(40):
        tid = f"T{i}"
        store.create(make_ticket(tid, "bug", "x", "t", "b0"))
        gate = threading.Barrier(2)

        def _status(tid=tid):
            gate.wait()
            store.update_status(tid, "fixed")

        def _body(tid=tid):
            gate.wait()
            store.update_body(tid, "b1")

        threads = [threading.Thread(target=_status), threading.Thread(target=_body)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = store.get(tid)
        assert (got["status"], got["body"]) == ("fixed", "b1")


# REL-7 AR-1: a holder still mid-start (its parent's words, or none) is waited on.
def _fake_proc(root: Path, pid: int, words: list, ppid: int, state: str = "S") -> None:
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "cmdline").write_bytes(b"\0".join(w.encode() for w in words) + b"\0")
    ticks = os.sysconf("SC_CLK_TCK")
    (root / str(pid) / "stat").write_text(
        f"{pid} (py) {state} {ppid} " + " ".join(["0"] * 17) + f" {100 * ticks} 0\n")
    (root / "uptime").write_text("100.5 0\n")


@posix_only
def test_a_holder_between_fork_and_exec_keeps_the_lock(tmp_path, monkeypatch):
    holder = subprocess.Popen(["sleep", "30"])
    try:
        proc = tmp_path / "proc"
        _fake_proc(proc, 1, ["pytest", "-q"], 0)
        _fake_proc(proc, holder.pid, ["pytest", "-q"], 1)          # the fork, before its exec
        monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
        lock = tmp_path / "7.pid"
        lock.write_text(str(holder.pid), encoding="utf-8")

        def _exec():
            time.sleep(0.3)
            _fake_proc(proc, holder.pid, [sys.executable, "-m", "tools.contest", "run", "--ticket", "7"], 1)

        threading.Thread(target=_exec, daemon=True).start()
        assert rounds._claim_lock(lock) is False
        assert lock.read_text(encoding="utf-8") == str(holder.pid)
    finally:
        holder.kill()
        holder.wait()


@posix_only
@pytest.mark.parametrize("words,state", [(["sleep", "30"], "S"), ([], "Z")])
def test_a_stranger_or_a_zombie_loses_the_lock_at_once(tmp_path, monkeypatch, words, state):
    holder = subprocess.Popen(["sleep", "30"])
    try:
        proc = tmp_path / "proc"
        _fake_proc(proc, 1, ["init"], 0)
        _fake_proc(proc, holder.pid, words, 1, state)
        monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
        lock = tmp_path / "7.pid"
        lock.write_text(str(holder.pid), encoding="utf-8")
        start = time.monotonic()
        assert rounds._claim_lock(lock) is True
        assert time.monotonic() - start < 1.0
    finally:
        holder.kill()
        holder.wait()
