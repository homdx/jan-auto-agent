"""`_wait_for_stream` reports whether the tap actually connected.

FL-1 (round 84) family C4, in production this time. An event published before
a tap's stream is open is *lost* — the fake drops it exactly as a real server
does — so a round that prompts into a tap nobody is reading hands its silence
clock a stream that has seen nothing, which is indistinguishable from a
session that genuinely went quiet.

`KiloBackend.wait_ready` and `variant.hello_probe` both wait for that
handshake. The wait used to be 5 s and to return `None` whether or not it
succeeded, so a tap that never connected looked exactly like one that
connected instantly. `hello_probe` is the sharpest case: its events being
dropped reads as "this variant did not answer", and the agent silently runs
at a lower reasoning variant than it could.

Nothing here raises. A slow tap is not a reason to fail a round — it is a
reason to say so.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.contest.backend import (  # noqa: E402
    STREAM_CONNECT_TIMEOUT_S,
    _wait_for_stream,
)


class _Tap:
    """The three attributes `_wait_for_stream` looks at."""

    def __init__(self, *, connects_after: float | None = 0.0, ended: bool = False):
        self.directory = "/nowhere"
        self._socket = object() if connects_after == 0.0 else None
        self._ended = ended
        if connects_after:
            threading.Timer(connects_after,
                            lambda: setattr(self, "_socket", object())).start()

    def join(self, _timeout):
        return self._ended


def test_a_connected_stream_reports_true():
    assert _wait_for_stream(_Tap()) is True


def test_a_stream_that_never_opens_reports_false_and_says_so(caplog):
    """The whole point: the caller can tell, and the log names the cost."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")

    connected = _wait_for_stream(_Tap(connects_after=None), timeout=0.05)

    assert connected is False
    assert any("did not open" in r.getMessage() for r in caplog.records), caplog.text
    assert any("reads as silent" in r.getMessage() for r in caplog.records)


def test_a_reader_that_already_ended_reports_false():
    """`tap.join()` true means the reader is gone — the wait will see
    `tap.closed`, and there is no stream to publish into either way."""
    assert _wait_for_stream(_Tap(connects_after=None, ended=True), timeout=0.05) is False


def test_a_slow_stream_is_waited_for_rather_than_abandoned():
    tap = _Tap(connects_after=0.15)

    assert _wait_for_stream(tap, timeout=10.0) is True


def test_the_default_is_not_a_five_second_bet():
    """5 s was a wall-clock bet on how fast a thread opens an HTTP connection,
    and the operator's 32-worker stress run walks through bets that size. The
    equivalent handshake in the contest tests is 30 s for the same reason."""
    assert STREAM_CONNECT_TIMEOUT_S >= 30.0
