"""A command's background processes end with it: a server it started does not outlive the check."""
import os
import subprocess
import time

import pytest

from tools.auto.executor import Executor

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _alive(tag: str) -> list:
    out = subprocess.run(["pgrep", "-f", f"^sleep {tag}$"], capture_output=True, text=True).stdout
    return out.split()


@posix_only
@pytest.mark.parametrize("command", [
    "sleep {tag} & echo started",
    "(sleep {tag} &) ; echo started",
    "nohup sleep {tag} >/dev/null 2>&1 & echo started",
])
def test_a_background_process_does_not_outlive_its_command(tmp_path, command):
    tag = str(5_000_000 + os.getpid() % 100_000)
    start = time.monotonic()
    try:
        r = Executor(base_dir=tmp_path, timeout_sec=30)._execute(
            command.format(tag=tag), cwd=tmp_path, task_id="T")
        # REL-RUNTIME kept: the shell's own exit is the result, at once — no wait for the child.
        assert (r.exit_code, r.timed_out, r.stdout) == (0, False, "started\n")
        assert time.monotonic() - start < 10
        deadline = time.monotonic() + 5
        while _alive(tag) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _alive(tag) == [], "the command's background sleep outlived it"
    finally:
        for pid in _alive(tag):
            subprocess.run(["kill", "-9", pid])
