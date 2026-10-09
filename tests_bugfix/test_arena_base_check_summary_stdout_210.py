"""tests_bugfix/test_arena_base_check_summary_stdout_210.py — bug 47: a step's summary is stdout's last line, stderr's only when stdout is empty (ticket 200's fix, pinned)."""

from __future__ import annotations

import subprocess

from tools.arena import basecheck

STEP = basecheck.Step("tests", ["python3", "-m", "pytest", "tests"], pytest=True)


def _proc(out: str, err: str, code: int = 0):
    return subprocess.CompletedProcess(STEP.command, code, out, err)


def test_pytest_line_beats_a_stderr_warning():
    proc = _proc("collected 3 items\n3 passed, 1 skipped in 1.2s\n", "DeprecationWarning: x\n")
    assert basecheck._summary(STEP, proc) == "3 passed, 1 skipped in 1.2s"


def test_stderr_answers_when_stdout_is_empty():
    assert basecheck._summary(STEP, _proc("\n  \n", "boom\nlast words\n", 1)) == "last words"


def test_failed_lines_still_follow_the_summary():
    proc = _proc("FAILED tests/a.py::t1\n1 failed in 0.1s\n", "warn\n", 1)
    assert basecheck._summary(STEP, proc) == "1 failed in 0.1s; FAILED tests/a.py::t1"
