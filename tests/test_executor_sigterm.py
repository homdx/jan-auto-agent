"""tests/test_executor_sigterm.py — SIGTERM-1: a SIGTERM to the auto executor kills the active child's group, leaving no orphan."""

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

#: The child command: writes its own pid — the session/group leader, since
#: `start_new_session` makes the shell its own session — and a grandchild's pid
#: (the `sleep 60 &` a leader-only kill would leave behind, the shape of a
#: `pytest` with xdist workers), then waits. `$$` is the pgid `_kill_process_group`
#: targets; `$!` is the grandchild it must reach too.
_CHILD_CMD = "echo $$ > pid.pid; sleep 60 & echo $! > sleep.pid; wait"

#: Runs `Executor._execute` on *cwd* in a subprocess of its own, so the SIGTERM
#: handler installs in that subprocess's main thread — exactly the shape of the
#: real `--auto` process — and the test can send it a SIGTERM without touching
#: the pytest process's own signal disposition.
_EXECUTOR_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from tools.auto.executor import Executor\n"
    "cwd, repo = Path(sys.argv[1]), Path(sys.argv[2])\n"
    "sys.path.insert(0, str(repo))\n"
    "ex = Executor(base_dir=repo, timeout_sec=0)\n"
    "ex._execute(sys.argv[3], cwd=cwd, task_id='T1')\n"
)


def _spawn_executor(cwd: Path) -> subprocess.Popen:
    """Start `_EXECUTOR_SCRIPT` in a fresh python; *cwd* is where the child writes its pid files."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", _EXECUTOR_SCRIPT, str(cwd), str(REPO_ROOT), _CHILD_CMD],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pid_state(pid: int) -> str | None:
    """The single-letter state of a pid from `/proc`, or ``None`` when it is gone.
    ``Z`` is a zombie that has not been reaped — the child is dead but still held."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1].split(" ", 1)[0]
    except OSError:
        return None


def _assert_gone(pid: int, label: str, timeout: float = 5.0) -> None:
    """Fail unless *pid* is dead **and reaped** within *timeout* (poll: SIGKILL is
    synchronous but the corpse takes a moment to be reaped; see the same helper in
    test_executor_interrupt.py)."""
    deadline = time.monotonic() + timeout
    last = _pid_state(pid)
    while last is not None and time.monotonic() < deadline:
        time.sleep(0.02)
        last = _pid_state(pid)
    assert last is None, (
        f"{label} outlived the kill: ({pid}) is still {last!r} after {timeout:.0f}s"
    )


def _wait_for(path: Path, timeout: float = 10.0) -> int:
    """Block until *path* holds a pid and return it. The command writes the pid
    files itself, so a non-empty file means the member it names is up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared — the command never started")


def test_sigterm_while_a_child_runs_kills_the_whole_group(tmp_path: Path) -> None:
    """The reproduction: `--auto` is SIGTERMed while the acceptance check runs; the
    check's whole process group must die with it, not keep running after the parent."""
    pid_file = tmp_path / "pid.pid"
    sleep_file = tmp_path / "sleep.pid"
    proc = _spawn_executor(tmp_path)
    try:
        shell_pid = _wait_for(pid_file)
        sleep_pid = _wait_for(sleep_file)
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=10)
        assert code == 128 + signal.SIGTERM, (
            f"the executor did not die on SIGTERM: exit {code}"
        )
        # No orphan: the group leader (the shell) and the member it spawned (the
        # sleep — the xdist-worker shape) are both gone and reaped.
        _assert_gone(shell_pid, "the shell")
        _assert_gone(sleep_pid, "the grandchild")
    finally:
        # A failed assertion must not leave the child group running after the
        # executor subprocess. Kill the leader's group (the sleep is a member), then
        # the executor subprocess itself if it is somehow still up.
        try:
            os.killpg(int(pid_file.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
