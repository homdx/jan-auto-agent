"""tests_bugfix/test_basecheck_summary_prefers_stdout_210.py — ticket 210, bug 47.

``tools/arena/basecheck.py``'s ``_summary`` used to read
``f"{stdout}\\n{stderr}"`` and take its last non-empty line, so a pytest step
that printed its real summary to stdout and then a warning to stderr showed
the warning, not pytest's own ``N passed in ...s`` line. The fix (landed with
ticket 200) is: the summary is the last non-empty line of stdout when stdout
has any content at all, else of stderr. This file pins that for ticket 210, so
a later change cannot quietly bring the stderr-wins bug back.
"""
from __future__ import annotations

import subprocess

from tools.arena import basecheck


def _proc(stdout: str, stderr: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


def test_a_stdout_summary_wins_over_a_later_stderr_warning():
    step = basecheck.Step("tests", ["x"], pytest=True)
    proc = _proc(stdout="collecting...\n7262 passed, 52 skipped in 90.1s\n",
                 stderr="DeprecationWarning: something unrelated\n")
    assert basecheck._summary(step, proc) == "7262 passed, 52 skipped in 90.1s"


def test_empty_stdout_falls_back_to_stderrs_last_line():
    step = basecheck.Step("tiers", ["x"], pytest=False)
    proc = _proc(stdout="", stderr="checking tiers...\nall tiers in sync\n")
    assert basecheck._summary(step, proc) == "all tiers in sync"


def test_whitespace_only_stdout_also_falls_back_to_stderr():
    step = basecheck.Step("tiers", ["x"], pytest=False)
    proc = _proc(stdout="   \n\n", stderr="stderr line\n")
    assert basecheck._summary(step, proc) == "stderr line"
