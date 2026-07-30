"""tests/test_executor_timeout_process_group.py — a timed-out acceptance
check must not leave orphaned child processes running.

subprocess.run(shell=True, timeout=...) only kills the DIRECT child on
timeout: the shell (/bin/sh -c "command"). Any process that shell spawns —
a backgrounded job with `&`, a test runner's own subprocess, a daemon a
Makefile target starts — is not that child, so killing the shell does not
kill it. It keeps running, orphaned, past the timeout that exists
specifically to bound how long an acceptance_check can run.

This matters more here than at most subprocess call sites: acceptance_check
is an LLM-authored or operator-supplied shell string, executed under a
timeout precisely because it is not trusted to terminate on its own — a
backgrounded or forked grandchild process escapes the one enforcement
mechanism this class exists to provide.

Fixed with the standard pattern for this well-known Python subprocess
gotcha: start_new_session=True puts the shell and everything it spawns into
one new process group, and os.killpg on timeout signals that whole group.

All tests use real subprocesses and real `ps`/proc inspection — no mocking
of subprocess internals, since the bug is specifically about OS-level
process-group behaviour that a mock cannot exercise.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import Executor


def _still_running(marker: str) -> list:
    check = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True)
    return [l for l in check.stdout.splitlines() if marker in l]


def _cleanup(marker: str) -> None:
    for line in _still_running(marker):
        pid = line.strip().split()[0]
        subprocess.run(["kill", "-9", pid], capture_output=True)


class TestTimeoutKillsTheWholeProcessGroup:
    def test_backgrounded_child_is_killed_on_timeout(self, tmp_path):
        marker = "38217"   # a duration unlikely to collide with anything else
        executor = Executor(base_dir=tmp_path, timeout_sec=1.0)
        try:
            result = executor._execute(
                f"sleep {marker} & echo child started",
                cwd=tmp_path, task_id="t1",
            )
            assert result.timed_out is True
            time.sleep(1.0)
            assert _still_running(f"sleep {marker}") == []
        finally:
            _cleanup(f"sleep {marker}")

    def test_grandchild_via_nested_shell_is_also_killed(self, tmp_path):
        """A backgrounded job that itself spawns a shell (a more realistic
        stand-in for a test runner or build tool forking workers)."""
        marker = "38221"
        executor = Executor(base_dir=tmp_path, timeout_sec=1.0)
        try:
            result = executor._execute(
                f"sh -c 'sleep {marker}' & echo started",
                cwd=tmp_path, task_id="t2",
            )
            assert result.timed_out is True
            time.sleep(1.0)
            assert _still_running(f"sleep {marker}") == []
        finally:
            _cleanup(f"sleep {marker}")

    def test_normal_non_timing_out_command_still_works(self, tmp_path):
        """Sanity: the fix must not break the ordinary, non-timeout path."""
        executor = Executor(base_dir=tmp_path, timeout_sec=10.0)
        result = executor._execute("echo hello", cwd=tmp_path, task_id="t3")
        assert result.timed_out is False
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_nonzero_exit_code_still_reported_correctly(self, tmp_path):
        executor = Executor(base_dir=tmp_path, timeout_sec=10.0)
        result = executor._execute("exit 7", cwd=tmp_path, task_id="t4")
        assert result.timed_out is False
        assert result.exit_code == 7

    def test_stderr_captured_on_normal_completion(self, tmp_path):
        executor = Executor(base_dir=tmp_path, timeout_sec=10.0)
        result = executor._execute(
            "echo oops 1>&2", cwd=tmp_path, task_id="t5",
        )
        assert "oops" in result.stderr

    def test_partial_output_before_timeout_is_preserved(self, tmp_path):
        """The process's own stdout before it was killed must still be
        captured, not silently dropped by the process-group cleanup."""
        executor = Executor(base_dir=tmp_path, timeout_sec=1.0)
        result = executor._execute(
            "echo before_timeout; sleep 30",
            cwd=tmp_path, task_id="t6",
        )
        assert result.timed_out is True
        assert "before_timeout" in result.stdout

    def test_timeout_disabled_runs_to_completion(self, tmp_path):
        """timeout_sec=0 disables the timeout entirely — a slower-than-
        default but finite command must still complete normally."""
        executor = Executor(base_dir=tmp_path, timeout_sec=0)
        result = executor._execute("sleep 0.2 && echo done", cwd=tmp_path, task_id="t7")
        assert result.timed_out is False
        assert "done" in result.stdout
