"""336: a stop signal inside `Popen.wait` must not hang the agent's cleanup.

`wait(timeout=…)` takes `_waitpid_lock` before its `try`, so a raising handler can land
between the two and leave the lock held; the cleanup's plain `proc.wait()` then blocked
on it for good, and a runner's abort hung for the length of the command (1 run in 1200).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from tools.contest import backend
from tools.process_reap import reap_killed


def _dies_holding_the_lock(monkeypatch, pid_file):
    """The first timed wait, once the command is up: the lock taken, then the handler's
    exception, as the race does it."""
    real = subprocess.Popen.wait

    def wait(self, timeout=None):
        if timeout == 60:
            deadline = time.monotonic() + 60
            while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()):
                assert time.monotonic() < deadline, "the command never started"
                time.sleep(0.01)
            self._waitpid_lock.acquire()
            raise SystemExit(143)
        return real(self, timeout=timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)


def test_the_abort_ends_and_kills_the_command_with_the_lock_leaked(tmp_path, monkeypatch):
    pid_file = tmp_path / "bash.pid"
    _dies_holding_the_lock(monkeypatch, pid_file)
    box = {}

    def go():
        try:
            backend._run_bash_group(f"echo $$ > {pid_file}; sleep 30", str(tmp_path), 60)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(90)
    assert not thread.is_alive(), "the cleanup hung on the leaked lock"
    assert isinstance(box.get("exc"), SystemExit), box
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text(encoding="utf-8").strip()), 0)


def test_reap_collects_a_killed_child_and_keeps_its_status():
    proc = subprocess.Popen(["sleep", "30"])
    proc.kill()
    reap_killed(proc)
    assert proc.returncode == -9


def test_reap_collects_it_past_a_leaked_lock_without_a_zombie():
    proc = subprocess.Popen(["sleep", "30"])
    proc._waitpid_lock.acquire()
    proc.kill()
    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    reap_killed(proc)
    assert proc.returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


def test_reap_never_raises_for_a_child_already_gone():
    proc = subprocess.Popen(["true"])
    proc.wait()
    reap_killed(proc)
    assert proc.returncode == 0
