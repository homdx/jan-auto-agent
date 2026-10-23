"""tools/file_lock.py — one exclusive lock for a read-modify-write of a file.

AUDIT-11 / AUDIT-13 / AUDIT-15: three places did a read-modify-write of a shared
file — ``tools/claimcheck/truth.py``, ``scripts/append_verdict.py`` and (with no
lock at all) ``scripts/append_finding.py`` and
``tools/contest/context_memory.add``. The two that did lock carried their own
copy of the same code, and on Windows both retried ``msvcrt.locking`` forever on
*every* ``OSError``: a permanent error (a bad handle, a read-only share) hung the
run instead of failing it. This is that code once, with the Windows retry
bounded and limited to the one error that means "someone else holds it".

POSIX: ``flock`` on the file's *directory*. The file itself is usually replaced
by each write (temp file + ``os.replace``), so a lock on the file would be a
lock on the old inode, and a lock file beside it would be litter in the
operator's directory. A filesystem with no lock on a directory (some network
mounts, WSL) degrades to unlocked, as it always did.

Windows: ``msvcrt.locking`` on a sidecar ``.<name>.lock``. ``LK_LOCK`` itself
tries for about 10 s and then raises ``EDEADLOCK``; that is retried up to
*attempts* times, any other error is raised at once, and running out of
attempts raises ``TimeoutError``.
"""

from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path

#: ``LK_LOCK`` waits ~10 s per call, so 30 attempts is about five minutes.
DEFAULT_NT_ATTEMPTS = 30

#: The errors ``msvcrt.locking`` raises when the region is held by another process.
_CONTENTION_ERRNOS = frozenset(
    e for e in (getattr(errno, "EDEADLOCK", None), getattr(errno, "EDEADLK", None))
    if e is not None
)


def msvcrt_acquire(fileno: int, msvcrt_mod, attempts: int = DEFAULT_NT_ATTEMPTS) -> None:
    """Lock one byte of *fileno* with ``msvcrt_mod.locking``.

    Contention is retried at most *attempts* times, then ``TimeoutError``; any
    other ``OSError`` propagates on the first occurrence.
    """
    attempts = max(1, int(attempts))
    last: "OSError | None" = None
    for _ in range(attempts):
        try:
            msvcrt_mod.locking(fileno, msvcrt_mod.LK_LOCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _CONTENTION_ERRNOS:
                raise
            last = exc
    raise TimeoutError(f"lock still held by another process after {attempts} attempts: {last}")


@contextlib.contextmanager
def locked(path, *, attempts: int = DEFAULT_NT_ATTEMPTS):
    """Hold an exclusive lock for a read-modify-write of *path*.

    Symlinks are followed in every component, so two spellings of one file share one lock. The
    parent directory is created when missing.
    """
    path = Path(path)
    # REL-3: every component is resolved, not only the last — the lock is taken on the
    # parent directory, and a symlinked *directory* ("/data -> /mnt/vol/data") gave two
    # spellings of one file two different locks. `realpath` of a path with no symlink in it
    # is its absolute path, so nothing else changes.
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import msvcrt
        with open(real.parent / f".{real.name}.lock", "a+b") as fh:
            msvcrt_acquire(fh.fileno(), msvcrt, attempts)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    try:
        fd = os.open(real.parent, os.O_RDONLY)
    except OSError:
        fd = None
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                pass    # no lock on a directory here: unlocked, as before
        yield
    finally:
        if fd is not None:
            os.close(fd)    # closing it lets the lock go
