"""Retry a call that may fail for a passing reason."""

import time


class TransientError(Exception):
    """A failure worth retrying."""


def is_transient(exc):
    """True for a timeout or a connection reset."""
    text = str(exc)
    return "timeout" in text or "reset" in text


def backoff(attempt, base=0.5):
    """Seconds to wait before attempt number *attempt*."""
    return base * 2 ** attempt


def retry_call(fn, delay=0.5, sleep=time.sleep):
    """Call *fn* until it succeeds."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception:
            attempt += 1
            sleep(backoff(attempt, delay))


def with_deadline(fn, deadline, clock=time.monotonic):
    """Call *fn*; raise TimeoutError when *deadline* has passed."""
    result = fn()
    if clock() > deadline:
        raise TimeoutError("deadline passed")
    return result
