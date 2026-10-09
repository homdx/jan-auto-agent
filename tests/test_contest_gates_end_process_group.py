"""tests/test_contest_gates_end_process_group.py — the harvest's group end leaves no member alive, not only no leader."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from tools.contest import gates

# a child that ignores TERM and sleeps: the member a leader's own exit leaves behind
_DEAF_CHILD = ("import signal, time\n"
               "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
               "time.sleep(60)\n")

# a leader that dies on TERM (the default action) after starting a deaf member
_LEADER_DIES = ("import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {_DEAF_CHILD!r}])\n"
                "time.sleep(60)\n")

# a leader that is deaf too: only the KILL pass can end it
_LEADER_DEAF = ("import signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"subprocess.Popen([sys.executable, '-c', {_DEAF_CHILD!r}])\n"
                "time.sleep(60)\n")


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _start(code: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    time.sleep(0.5)                                     # the member is started and has set its handler
    return proc


@pytest.fixture
def started():
    procs: list[subprocess.Popen] = []

    def start(code: str) -> subprocess.Popen:
        procs.append(_start(code))
        return procs[-1]

    yield start
    for proc in procs:                                  # never leave a sleeper behind a failed test
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()


def test_a_member_that_ignores_term_does_not_outlive_the_end_of_the_group(started):
    proc = started(_LEADER_DIES)
    begin = time.monotonic()
    gates._end_process_group(proc)
    assert not _group_alive(proc.pid), "the leader exited on TERM, a deaf member was left running"
    assert time.monotonic() - begin < 5.0


def test_a_deaf_leader_and_a_deaf_member_are_both_ended(started):
    proc = started(_LEADER_DEAF)
    gates._end_process_group(proc)
    assert proc.poll() is not None
    assert not _group_alive(proc.pid)


def test_a_group_that_ends_on_term_is_not_waited_on(started):
    proc = started("import time\ntime.sleep(60)\n")
    begin = time.monotonic()
    gates._end_process_group(proc)
    assert not _group_alive(proc.pid)
    assert time.monotonic() - begin < 1.0               # no second pass, no polling delay


def test_a_group_that_is_already_gone_is_not_an_error():
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    gates._end_process_group(proc)                      # fail-open: nothing to signal, nothing raised
