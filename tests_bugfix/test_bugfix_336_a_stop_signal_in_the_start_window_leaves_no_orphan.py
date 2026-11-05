"""336: a stop signal while `Popen` has not returned must not leave the command running.

The shell runs before `Popen` returns. `_sigterm_handler` found no active child there, ended
the parent, and the group lived on, in a session of its own, for as long as its command: the
orphan SIGTERM-1 was written to prevent. Seen on a busy disk as a shell still sleeping after
its parent exited 143. Here the signal is sent from inside that window, every time.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name == "nt", reason="process groups and /proc are POSIX-only")

_SCRIPT = """
import os, signal, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from tools.auto.executor import Executor
cwd, signum = Path(sys.argv[2]), int(sys.argv[3])
real = subprocess.Popen
def late(*a, **k):
    proc = real(*a, **k)
    while not ((cwd / 'sleep.pid').exists() and (cwd / 'sleep.pid').read_text().strip()):
        time.sleep(0.01)
    os.kill(os.getpid(), signum)
    return proc
subprocess.Popen = late
Executor(base_dir=Path(sys.argv[1]), timeout_sec=0)._execute(
    'echo $$ > pid.pid; sleep 60 & echo $! > sleep.pid; wait', cwd=cwd, task_id='T1')
"""


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def _gone_within(pid: int, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _alive(pid)


@pytest.mark.parametrize("signum,code", [(signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGINT, -signal.SIGINT)])
def test_a_signal_inside_popen_takes_the_group_down(tmp_path, signum, code):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    done = subprocess.run([sys.executable, "-c", _SCRIPT, str(REPO_ROOT), str(tmp_path), str(int(signum))],
                          cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
    shell = int((tmp_path / "pid.pid").read_text().strip())
    sleep = int((tmp_path / "sleep.pid").read_text().strip())
    try:
        assert done.returncode == code, done.stderr[-400:]
        assert _gone_within(shell), "the shell outlived its parent"
        assert _gone_within(sleep), "the command outlived its parent"
    finally:
        try:
            os.killpg(shell, signal.SIGKILL)
        except OSError:
            pass


def test_no_signal_runs_the_command_as_before(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    code = ("import sys; from pathlib import Path; from tools.auto.executor import Executor\n"
            f"r = Executor(base_dir=Path({str(tmp_path)!r}), timeout_sec=60)._execute('echo hello', cwd=Path({str(tmp_path)!r}), task_id='T')\n"
            "sys.exit(0 if r.passed and 'hello' in r.stdout else 3)")
    assert subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env, timeout=120).returncode == 0
