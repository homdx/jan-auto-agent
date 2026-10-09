"""Retry a call that may fail for a passing reason."""

import time


class TransientError(Exception):
    """A failure worth retrying."""


def is_transient(exc):
    """True for a timeout or a connection reset."""
    text = str(exc).lower()
    return "timeout" in text or "reset" in text


def backoff(attempt, base=0.5):
    """Seconds to wait before attempt number *attempt*."""
    return base * 2 ** attempt


def retry_call(fn, delay=0.5, sleep=time.sleep, max_attempts=5):
    """Call *fn* until it succeeds, at most *max_attempts* times."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except TransientError:
            if attempt == max_attempts:
                raise
            sleep(delay)


def with_deadline(fn, deadline, clock=time.monotonic):
    """Call *fn*; raise TimeoutError when *deadline* has passed."""
    result = fn()
    if clock() > deadline:
        raise TimeoutError("deadline passed")
    return result
