"""tests_bugfix/test_arena_lock_swap_failure_181.py — bug 181: a failed lock swap after spawn never orphans the runner."""

from __future__ import annotations

import os
from pathlib import Path

from tools.arena import rounds


class _Child:
    pid = 424242

    def __init__(self):
        self.waited = False

    def wait(self):
        self.waited = True
        return 0


def test_a_failed_pid_swap_still_waits_the_runner_out(tmp_path, monkeypatch):
    child = _Child()
    monkeypatch.setattr(rounds, "SPAWN", lambda line, cwd: child)
    real_replace = os.replace

    def broken_replace(src, dst):
        if str(src).endswith(".pid.tmp"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(rounds.os, "replace", broken_replace)
    code = rounds._run_child(tmp_path, 9, ["true"], tmp_path / "state.json")
    assert child.waited, "the running child was abandoned"
    assert code == rounds.EXIT_OK
    locks = tmp_path / ".arena" / "locks"
    assert not (locks / "9.pid").exists() and not (locks / "9.pid.tmp").exists()


def test_a_spawn_failure_is_still_cannot_start(tmp_path, monkeypatch, capsys):
    def boom(line, cwd):
        raise OSError(2, "No such file")

    monkeypatch.setattr(rounds, "SPAWN", boom)
    code = rounds._run_child(tmp_path, 9, ["nope"], tmp_path / "state.json")
    assert code != rounds.EXIT_OK
    assert "cannot start the runner" in capsys.readouterr().err
    assert not (tmp_path / ".arena" / "locks" / "9.pid").exists()


def test_an_exception_that_is_no_oserror_from_spawn_leaves_no_lock(tmp_path, monkeypatch):
    """The 157 guard: the lock goes whatever SPAWN raises, the swap fix does not narrow that."""
    import pytest

    def boom(line, cwd):
        raise RuntimeError("anything")

    monkeypatch.setattr(rounds, "SPAWN", boom)
    with pytest.raises(RuntimeError):
        rounds._run_child(tmp_path, 9, ["x"], tmp_path / "state.json")
    locks = tmp_path / ".arena" / "locks"
    assert not (locks / "9.pid").exists() and not (locks / "9.pid.tmp").exists()
