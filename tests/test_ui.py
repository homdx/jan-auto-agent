"""tests/test_ui.py — AGNES-11: a closed output stream no longer raises from
`Spinner.spin`, `Spinner.stop` or `stream_tracker.on_token`.

A closed stream (an `io.StringIO` after `.close()`) raises `ValueError` on
write — distinct from the `BrokenPipeError`/`OSError` family a dead pipe
raises. The three writers in tools/ui.py already swallow the pipe errors;
AGNES-11 adds the closed-stream case: `spin` stops the spinner thread exactly
like the broken-pipe branch, `stop` finishes despite the swallowed clear
line, and `on_token` keeps counting tokens because the received answer is
not discarded just because its cosmetic echo failed.

  AC-1  `Spinner.spin` on a closed stream raises nothing and stops the thread.
  AC-2  `Spinner.stop` on a closed stream raises nothing (with or without a live thread).
  AC-3  `stream_tracker().on_token` on a closed stream raises nothing and still counts.
  AC-4  The pre-existing broken-pipe behaviour is unchanged (guard, passes on base).
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from unittest import mock

from tools import ui


def _closed_stdout() -> io.StringIO:
    """A real closed stream: every write raises `ValueError: I/O operation on closed file`."""
    buf = io.StringIO()
    buf.close()
    return buf


def test_spin_closed_stdout_stops_thread_without_raising():
    """AC-1: `spin` on a closed stream behaves like the broken-pipe branch — no raise, thread stopped."""
    spinner = ui.Spinner("waiting")
    spinner.running = True  # as if the background thread were alive
    with mock.patch.object(sys, "stdout", _closed_stdout()):
        spinner.spin()
    assert spinner.running is False
    assert spinner.idx == 0  # no frame was drawn, exactly like the broken-pipe branch


class _BrokenPipeStdout:
    """A stand-in for a pipe that closed early: writes raise `BrokenPipeError` (an `OSError`)."""

    encoding = "utf-8"

    def write(self, _t: str) -> None:
        raise BrokenPipeError("stdout pipe closed early")

    def flush(self) -> None:
        pass


def test_spin_broken_pipe_stops_thread_without_raising():
    """AC-4 guard: the pre-existing broken-pipe branch still stops the thread without raising."""
    spinner = ui.Spinner("waiting")
    spinner.running = True
    with mock.patch.object(sys, "stdout", _BrokenPipeStdout()):
        spinner.spin()
    assert spinner.running is False
    assert spinner.idx == 0


def test_stop_closed_stdout_still_finishes():
    """AC-2: `stop` with no thread yet must not raise when the clear line hits a closed stream."""
    spinner = ui.Spinner("waiting")
    with mock.patch.object(sys, "stdout", _closed_stdout()):
        spinner.stop()
    assert spinner.running is False
    assert spinner._thread is None


def test_stop_closed_stdout_full_lifecycle():
    """AC-2: a live spinner thread meeting a closed stream mid-run still lets `stop` finish."""
    spinner = ui.Spinner("waiting")
    spinner.start()
    time.sleep(0.1)  # let the thread draw frames on the real stdout first
    with mock.patch.object(sys, "stdout", _closed_stdout()):
        time.sleep(0.25)  # several 0.08 s spin cycles: the thread now meets the closed stream
        spinner.stop()
    assert spinner.running is False
    assert spinner._thread is None


def test_on_token_closed_stdout_keeps_counting():
    """AC-3: `on_token` on a closed stream raises nothing and the received tokens are still counted."""
    on_tok, stats = ui.stream_tracker()
    with mock.patch.object(sys, "stdout", _closed_stdout()):
        on_tok("a")
        on_tok("b")
    assert "2 tok" in stats()


def test_on_token_broken_pipe_keeps_counting():
    """AC-4 guard: the pre-existing broken-pipe swallow keeps counting too."""
    on_tok, stats = ui.stream_tracker()
    with mock.patch.object(sys, "stdout", _BrokenPipeStdout()):
        on_tok("a")
        on_tok("b")
    assert "2 tok" in stats()


# ── beyond the ticket's three cases: other kinds of closed stream, mid-stream, and what must still raise ──
import pytest  # noqa: E402


def _closed_file():
    f = tempfile.TemporaryFile("w+")
    f.close()
    return f


def _closed_wrapper():
    r, w = os.pipe()
    t = io.TextIOWrapper(os.fdopen(w, "wb"), encoding="utf-8")
    t.close()
    os.close(r)
    return t


@pytest.mark.parametrize("make", [_closed_stdout, _closed_file, _closed_wrapper])
def test_all_three_writers_survive_every_kind_of_closed_stream(make):
    """A closed `StringIO`, a closed file and a closed text wrapper all raise `ValueError` on write."""
    with mock.patch.object(sys, "stdout", make()):
        ui.Spinner("t").spin()
        ui.Spinner("t").stop()
        on_token, stats = ui.stream_tracker()
        on_token("a")
        time.sleep(0.06)
        on_token("b")
        on_token("c")
        assert "3 tok" in stats()


def test_a_stream_closed_midway_keeps_the_answer_and_the_count():
    buf = io.StringIO()
    on_token, stats = ui.stream_tracker()
    with mock.patch.object(sys, "stdout", buf):
        on_token("hello ")
        time.sleep(0.06)
        buf.close()
        on_token("x")
        on_token("y")
    assert "3 tok" in stats()


def test_the_spinner_thread_ends_quietly_on_a_closed_stream(monkeypatch):
    """No traceback from the daemon thread, and `running` is cleared."""
    import threading
    seen = []
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append(a.exc_value))
    spinner = ui.Spinner("t")
    with mock.patch.object(sys, "stdout", _closed_stdout()):
        spinner.start()
        time.sleep(0.3)
        spinner.stop()
    assert spinner.running is False and seen == []


def test_a_valueerror_that_is_not_the_output_still_propagates():
    """Only the write is guarded: a message whose `__str__` raises `ValueError` is a bug to see."""
    class Msg:
        def __str__(self):
            raise ValueError("not a closed stream")

    with mock.patch.object(sys, "stdout", io.StringIO()):
        with pytest.raises(ValueError, match="not a closed stream"):
            ui.Spinner(Msg()).spin()


@pytest.mark.parametrize("call", [lambda: ui.Spinner("t").spin(), lambda: ui.stream_tracker()[0]("x")])
def test_other_write_errors_still_propagate(call):
    class Boom(io.StringIO):
        def write(self, s):
            raise RuntimeError("disk on fire")

    with mock.patch.object(sys, "stdout", Boom()):
        with pytest.raises(RuntimeError, match="disk on fire"):
            call()
