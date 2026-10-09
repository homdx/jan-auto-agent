"""tests_bugfix/test_retry_helpers_review.py — tools/backoff.py and tools/git_run.py edge contracts."""

from __future__ import annotations

import subprocess

import pytest

from tools import backoff, git_run

LOCKED = ("fatal: Unable to create '/repo/.git/index.lock': File exists.\n\n"
          "Another git process seems to be running in this repository.")


# ── bug: `retry_with_backoff(attempts=None)` raised TypeError ────────────────
# The docstring: "A falsy ``attempts`` means one try with no retry (still raises on
# failure, never sleeps)". `int(None)` is a TypeError, so a caller passing a
# missing config value got a crash instead of one try.

@pytest.mark.parametrize("attempts", [None, 0, False, ""])
def test_a_falsy_attempts_is_one_try_that_raises_the_calls_own_error(attempts):
    calls, sleeps = [], []

    def call():
        calls.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        backoff.retry_with_backoff(call, attempts=attempts, sleep_fn=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []


def test_a_falsy_attempts_still_returns_a_successful_result():
    assert backoff.retry_with_backoff(lambda: 42, attempts=None) == 42


def test_attempts_three_still_retries_twice():
    calls, sleeps = [], []

    def call():
        calls.append(1)
        raise ValueError("again")

    with pytest.raises(ValueError):
        backoff.retry_with_backoff(call, attempts=3, sleep_fn=sleeps.append)
    assert len(calls) == 3
    assert len(sleeps) == 2


# ── bug: `run_git(retries=0)` still slept once ───────────────────────────────
# "retries=1 is a single attempt with no waiting at all" — but the loop ran
# `range(max(1, retries))` while the last-attempt test compared the raw `retries`,
# so 0 (or a negative) never matched and a held index cost one useless backoff.

class _Runs:
    def __init__(self, monkeypatch):
        self.calls = 0
        monkeypatch.setattr(git_run.subprocess, "run", self._run)

    def _run(self, cmd, **kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=LOCKED)


@pytest.mark.parametrize("retries", [1, 0, -1, -5])
def test_a_single_attempt_never_waits(monkeypatch, retries):
    runs = _Runs(monkeypatch)
    sleeps = []
    proc = git_run.run_git(["git", "add", "x"], cwd=".", retries=retries, sleep=sleeps.append)
    assert proc.returncode == 128
    assert runs.calls == 1
    assert sleeps == []


def test_a_held_index_is_still_retried_the_requested_number_of_times(monkeypatch):
    runs = _Runs(monkeypatch)
    sleeps = []
    proc = git_run.run_git(["git", "add", "x"], cwd=".", retries=4, sleep=sleeps.append)
    assert proc.returncode == 128
    assert runs.calls == 4
    assert len(sleeps) == 3
