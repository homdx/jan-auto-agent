"""A child's start and its end, on the paths a stop signal can interrupt."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading

#: How long a SIGKILLed child gets to be collected by `Popen.wait` (seconds).
_REAP_WAIT_SEC = 2.0


_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@contextlib.contextmanager
def stop_signals_held():
    """Hold a stop signal that arrives inside the block; yields the list of those that did.

    The caller re-raises them (``signal.raise_signal``) once it can clean up, so the
    handler that was in place still runs, just later. Off the main thread, where
    no handler can be set, nothing is held; neither is a signal whose handler is not
    Python's (ignored, or the default that ends the process anyway).
    """
    held: list = []
    if threading.current_thread() is not threading.main_thread():
        yield held
        return
    previous = {}
    for signum in _STOP_SIGNALS:
        handler = signal.getsignal(signum)
        if callable(handler):
            previous[signum] = handler
            signal.signal(signum, lambda num, frame: held.append(num))
    try:
        yield held
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def reap_killed(proc: subprocess.Popen) -> None:
    """`proc.wait()` for a child already killed, when a signal may have brought us here.

    `Popen.wait(timeout=…)` takes `_waitpid_lock` before entering its `try`, so a signal
    whose handler raises can land between the two and leave the lock held for good. A plain
    `proc.wait()` then blocks on that lock forever, and a process told to stop never does:
    a runner's abort hung for the length of the command, 1 run in 1200. So the wait has a
    bound, and past it the child is collected directly. Never raises.
    """
    try:
        proc.wait(timeout=_REAP_WAIT_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        pid, status = os.waitpid(proc.pid, getattr(os, "WNOHANG", 0))
        if pid == proc.pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
    except (OSError, ValueError):
        pass
