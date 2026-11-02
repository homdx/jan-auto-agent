"""GYRO-01 follow-up: the standalone `locked()` fallback in append_task.py degrades to
unlocked when the filesystem takes no lock, as tools/file_lock.py does, instead of crashing
the agent's one write — and the Windows branch takes a real lock, not a silent no-op.

Before this fix: the fallback's `os.open`/`fcntl.flock` calls were unguarded, so a
filesystem that refuses a directory flock (some network mounts, a sandboxed FS) crashed
the standalone script instead of recording the row unlocked; and on Windows (`os.name ==
"nt"`) the fallback took no lock at all, unlike tools/file_lock.py's msvcrt-based one. A
Windows lock another agent holds is waited for (30 tries, then TimeoutError), never written
through: only a share that takes no lock at all degrades to unlocked.
"""

from __future__ import annotations

import errno
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "append_task.py"


def _load_standalone_module(monkeypatch):
    """Import append_task.py fresh with tools.file_lock unavailable, as in an external
    --target repo (GYRO-01) — this is what makes the module define its own `locked()`."""
    monkeypatch.setitem(sys.modules, "tools.file_lock", None)
    spec = importlib.util.spec_from_file_location("append_task_standalone_fallback", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_posix_lock_degrades_to_unlocked_when_open_raises(monkeypatch, tmp_path):
    module = _load_standalone_module(monkeypatch)
    monkeypatch.setattr(module.os, "name", "posix")

    def _raising_open(path, flags):
        raise OSError("flock not supported on this mount")

    monkeypatch.setattr(module.os, "open", _raising_open)

    ran = []
    with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
        ran.append(True)
    assert ran == [True]


def test_posix_lock_degrades_to_unlocked_when_flock_raises(monkeypatch, tmp_path):
    module = _load_standalone_module(monkeypatch)
    monkeypatch.setattr(module.os, "name", "posix")

    import fcntl

    def _raising_flock(fd, op):
        raise OSError("lock held by another process")

    monkeypatch.setattr(fcntl, "flock", _raising_flock)

    ran = []
    with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
        ran.append(True)
    assert ran == [True]


def test_windows_path_locks_and_unlocks_in_order(monkeypatch, tmp_path):
    module = _load_standalone_module(monkeypatch)
    monkeypatch.setattr(module.os, "name", "nt")
    calls = []

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 0

        @staticmethod
        def locking(fileno, mode, nbytes):
            calls.append(mode)

    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt)

    with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
        calls.append("yielded")
    assert calls == [_FakeMsvcrt.LK_LOCK, "yielded", _FakeMsvcrt.LK_UNLCK]


def _windows_module(monkeypatch, locking):
    module = _load_standalone_module(monkeypatch)
    monkeypatch.setattr(module.os, "name", "nt")

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 0

    _FakeMsvcrt.locking = staticmethod(locking)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt)
    return module


def test_windows_path_degrades_to_unlocked_when_the_share_refuses_any_lock(monkeypatch, tmp_path):
    def _refusing(fileno, mode, nbytes):
        raise OSError(errno.EACCES, "this share takes no lock")

    module = _windows_module(monkeypatch, _refusing)
    ran = []
    with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
        ran.append(True)
    assert ran == [True]


def test_windows_path_waits_for_a_lock_another_agent_holds(monkeypatch, tmp_path):
    calls = []

    def _busy_twice(fileno, mode, nbytes):
        calls.append(mode)
        if mode == 1 and calls.count(1) <= 2:
            raise OSError(errno.EDEADLOCK, "held by another agent")

    module = _windows_module(monkeypatch, _busy_twice)
    with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
        calls.append("yielded")
    assert calls == [1, 1, 1, "yielded", 0]


def test_windows_path_never_writes_through_a_lock_that_stays_held(monkeypatch, tmp_path):
    calls = []

    def _always_busy(fileno, mode, nbytes):
        calls.append(mode)
        raise OSError(errno.EDEADLOCK, "held by another agent")

    module = _windows_module(monkeypatch, _always_busy)
    ran = []
    with pytest.raises(TimeoutError):
        with module.locked(str(tmp_path / "runs" / "a" / "PROGRESS.csv")):
            ran.append(True)
    assert ran == [] and len(calls) == 30
