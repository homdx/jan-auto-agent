"""tests/test_executor_interrupt.py — CC-16 (Part D).

Fix 35 moved `Executor._execute`'s child into a session of its own
(`start_new_session=True`) and killed the whole group on `TimeoutExpired`. It also
dropped the `except: process.kill(); raise` `subprocess.run` used to have for *any*
exception — and `start_new_session` takes the child out of the terminal's foreground
process group, so a terminal Ctrl-C no longer reaches it either. Nothing then handled
an interruption but the timeout: after Ctrl-C the acceptance check (a `pytest` with
workers, minutes long) lived on and kept writing into the worktree the next `--auto`
run resets.

These tests pin the fix — every exception that leaves `communicate` kills the group,
reaps it, and goes on unchanged — and that the timeout and normal-exit paths keep
the behaviour fix 35 gave them.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tools.auto.executor import Executor

pytestmark = pytest.mark.skipif(os.name == "nt", reason="process groups and /proc are POSIX-only")


def _pid_state(pid: int) -> str | None:
    """The single-letter state of a pid from `/proc`, or ``None`` when it is gone.
    ``Z`` is a zombie that has not been reaped — the child is dead but still held."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0]
    except OSError:
        return None


def _wait_for(path: Path, timeout: float = 10.0) -> str:
    """Block until `path` holds a pid, and return it. The child is the one that
    writes it, so a non-empty file means it is up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared — the command never started")


@pytest.fixture
def executor(tmp_path: Path) -> Executor:
    """A real executor: these tests exercise `_execute`'s own process handling, so
    nothing about it is stubbed except the one call under test."""
    return Executor(base_dir=tmp_path, timeout_sec=0)


def _run_until_interrupt(executor: Executor, command: str, cwd: Path, pidfile: str = "pid.pid"):
    """Run `_execute(command)` in a thread whose `communicate` raises KeyboardInterrupt
    once the child is up — what Ctrl-C does to the agent process while the command
    runs. Returns `(exception, [pids written by the command])`."""
    pid_path = cwd / pidfile
    real_communicate = subprocess.Popen.communicate

    def _interrupting_communicate(self, *args, **kwargs):
        _wait_for(pid_path)
        raise KeyboardInterrupt

    box: list = []

    def _target():
        try:
            executor._execute(command, cwd=cwd, task_id="T1")
        except BaseException as exc:  # noqa: BLE001 — the point is that it escapes
            box.append(exc)

    subprocess.Popen.communicate = _interrupting_communicate
    try:
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=30)
    finally:
        subprocess.Popen.communicate = real_communicate
    assert not thread.is_alive(), "_execute never came back after the interruption"
    return (box[0] if box else None), _pids(cwd)


def _pids(cwd: Path) -> dict:
    out = {}
    for path in sorted(cwd.glob("*.pid")):
        out[path.stem] = int(path.read_text(encoding="utf-8").strip())
    return out


# ── the defect ────────────────────────────────────────────────────────────────

def test_keyboard_interrupt_kills_the_commands_whole_group(executor, tmp_path):
    """The reproduction: Ctrl-C during `_execute` leaves the command's process group
    dead and reaped, and the KeyboardInterrupt goes on out of `_execute` unchanged."""
    exception, pids = _run_until_interrupt(
        executor, "echo $$ > pid.pid; exec sleep 60", tmp_path,
    )
    assert isinstance(exception, KeyboardInterrupt), f"expected the interrupt to propagate, got {exception!r}"
    assert pids, "the command never wrote its pid"
    # `exec sleep` keeps the shell's pid, so the file names the very process that must be gone.
    assert _pid_state(pids["pid"]) is None, "the command outlived the interruption"


def test_keyboard_interrupt_kills_grandchildren_too(executor, tmp_path):
    """A shell pipeline's children are grandchildren of `_execute` and members of the
    same group — a `pytest` with xdist workers is exactly that shape, and killing the
    shell alone is what let them outlive the timeout before fix 35."""
    command = (
        "echo $$ > pid.pid; "
        "sleep 60 & echo $! > child1.pid; "
        "sleep 60 & echo $! > child2.pid; "
        "wait"
    )
    exception, pids = _run_until_interrupt(executor, command, tmp_path)
    assert isinstance(exception, KeyboardInterrupt)
    assert set(pids) == {"pid", "child1", "child2"}, pids
    for name, pid in pids.items():
        assert _pid_state(pid) is None, f"{name} ({pid}) outlived the interruption"


def test_other_exceptions_from_communicate_kill_the_group_too(executor, tmp_path):
    """The same for any exception that leaves `communicate`, not only KeyboardInterrupt —
    `subprocess.run`'s old blanket `except: process.kill(); raise` covered them all."""
    pid_path = tmp_path / "pid.pid"
    real_communicate = subprocess.Popen.communicate

    class Boom(RuntimeError):
        pass

    def _raising_communicate(self, *args, **kwargs):
        _wait_for(pid_path)
        raise Boom("reader died")

    box: list = []

    def _target():
        try:
            executor._execute("echo $$ > pid.pid; exec sleep 60", cwd=tmp_path, task_id="T1")
        except BaseException as exc:  # noqa: BLE001
            box.append(exc)

    subprocess.Popen.communicate = _raising_communicate
    try:
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=30)
    finally:
        subprocess.Popen.communicate = real_communicate
    assert not thread.is_alive()
    assert isinstance(box[0], Boom)
    assert _pid_state(int(pid_path.read_text(encoding="utf-8").strip())) is None


# ── the paths that must not change ─────────────────────────────────────────────

def test_a_command_that_finishes_by_itself_is_untouched(executor, tmp_path):
    """A command that ends on its own still returns its output and its exit code."""
    result = executor._execute("echo hello", cwd=tmp_path, task_id="T1")
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


def test_a_command_that_times_out_is_untouched(executor, tmp_path):
    """The timeout branch keeps fix 35's behaviour: the group is killed, the partial
    output is returned, and the result says it timed out."""
    pid_path = tmp_path / "pid.pid"
    ex = Executor(base_dir=tmp_path, timeout_sec=1)
    result = ex._execute("echo $$ > pid.pid; echo partial; sleep 60", cwd=tmp_path, task_id="T1")
    assert result.timed_out is True
    assert result.exit_code == -1
    assert "partial" in result.stdout
    pid = int(pid_path.read_text(encoding="utf-8").strip())
    assert _pid_state(pid) is None, "the timed-out command outlived its timeout"
