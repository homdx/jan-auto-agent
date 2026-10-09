"""Round 146: the minutes a provider spends failing are given back to the turn's deadline.

glm-4.7-flash twice ended `STALLED: no idle after 70m (… unchanged for 10m)` with a
real tree (6–7 files, ~950 lines). Its `events.jsonl` for the turn, in five-minute
buckets, was this:

    21:15–21:37  status:retry 1–2 per bucket, nothing else          (zai erroring)
    21:38–21:50  message.part.delta in thousands, busy x20          (the work)
    21:50–22:00  status:retry 2–3 per bucket, offline / restored    (zai erroring)
    22:00:05     the 70-minute deadline: the churn had not grown in the last ten
                 minutes, so KC-36 refused the extension and aborted

More than thirty of the seventy minutes were Kilo waiting to retry a provider that
was failing — time the agent could not have worked in, counted by the deadline as
"no work". KC-66 gives the gate's waits back and KC-58 the suite queue's; this is the
same kind of time: a streak of `retry` statuses, from the first one to the next
output of the model, moves the turn's deadline by exactly its length. The streak is
bounded elsewhere (KC-64 `provider_retry_max_attempts`, KC-61's bound on a delay), and
the giveback by the turn's own timeout.

Everything runs over a scripted tap on a fake clock: no transport, no wall time.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.contest.kilo_client as kc  # noqa: E402
from tools.contest.kilo_client import KiloClient, SessionRef  # noqa: E402

SESSION = SessionRef(id="ses_clock", provider_id="p", model_id="m", directory="/tmp/ws")
SID = SESSION.id


class _Clock:
    def __init__(self, monotonic=1000.0):
        self.now = float(monotonic)
        self.wall = float(time.time())

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.now += float(seconds)
        self.wall += float(seconds)


class _Tap:
    """``(seconds_after_the_previous, event)`` pairs on the fake clock.

    Like `EventTap.wait`: the cursor passes every event looked at, a rejected one is
    consumed, and when nothing arrives before the timeout the clock moves to it.
    """

    def __init__(self, script, clock):
        self._script = list(script)
        self._clock = clock
        self._due = clock.now + (self._script[0][0] if self._script else 0.0)

    def wait(self, pred, timeout):
        limit = self._clock.now + max(0.0, float(timeout))
        while True:
            if not self._script or self._due > limit:
                self._clock.advance(limit - self._clock.now)
                return None
            self._clock.advance(self._due - self._clock.now)
            _, event = self._script.pop(0)
            if self._script:
                self._due = self._clock.now + self._script[0][0]
            if pred(event):
                return event


class _Client(KiloClient):
    def __init__(self):
        self.aborts = []

    def _abort_quietly(self, session):
        self.aborts.append(session.id)


def _status(kind, **extra):
    return {"type": "session.status",
            "properties": {"sessionID": SID, "status": {"type": kind, **extra}}}


def _retry(attempt=1):
    return _status("retry", attempt=attempt, message="upstream timed out")


def _output():
    """A part the model produced: the end of a retry streak."""
    return {"type": "message.part.updated",
            "properties": {"sessionID": SID, "part": {"type": "step-start", "id": "p1"}}}


def _idle():
    return {"type": "session.idle", "properties": {"sessionID": SID}}


def _reject(event):
    return "reject", "test"


def _wait(monkeypatch, script, *, timeout=600.0, grants=(), armed=True,
          silence=None, retries_limit=None):
    """`wait_idle` over *script*; ``on_deadline`` answers from *grants* and logs asks."""
    clock = _Clock()
    monkeypatch.setattr(kc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kc.time, "time", clock.time)
    client = _Client()
    asked, answers = [], list(grants)

    def on_deadline(elapsed):
        asked.append(round(elapsed, 1))
        return answers.pop(0) if answers else None

    result = client.wait_idle(
        _Tap(script, clock), SESSION, timeout, on_permission=_reject,
        on_question=lambda event: None, idle_event_timeout=silence,
        on_deadline=on_deadline if armed else None,
        max_retry_attempts=retries_limit)
    return client, result, asked


# ── the reproduction ──────────────────────────────────────────────────────────

def test_a_turn_that_spent_its_minutes_retrying_a_failing_provider_is_not_aborted(monkeypatch):
    """The glm turn, scaled to a 600 s deadline: 400 s of retries, then the work.

    Before: the deadline fired at 600 s with the model still working — the abort
    and `no idle after …`. Now the 400 s are given back, the turn idles at 700 s,
    and the deadline never asked."""
    script = [(5.0, _status("busy")), (5.0, _retry(1)),       # at 10 s: the streak starts
              (395.0, _output()),                             # at 405 s: the model answers
              (295.0, _idle())]                               # at 700 s: done
    client, result, asked = _wait(monkeypatch, script, silence=900.0)
    assert result.status == "idle", result
    assert asked == [] and client.aborts == []
    assert 394.0 <= result.retry_waited <= 396.0              # exactly the streak


def test_the_same_turn_without_the_retries_is_aborted_at_the_deadline(monkeypatch):
    """The control: 400 s of silence that is no retry gets the old verdict."""
    script = [(5.0, _status("busy")), (695.0, _idle())]
    client, result, asked = _wait(monkeypatch, script)
    assert result.status == "timeout"
    assert asked == [600.0] and client.aborts == [SID]
    assert result.retry_waited == 0.0


def test_a_streak_still_open_at_the_deadline_holds_it(monkeypatch):
    """Retries from 300 s on, the answer at 800 s: the deadline (600 s) falls
    inside the streak, does not ask, and the turn is not aborted."""
    script = [(5.0, _status("busy")), (295.0, _retry(1)), (500.0, _output()),
              (50.0, _idle())]
    client, result, asked = _wait(monkeypatch, script, silence=1000.0)
    assert result.status == "idle"
    assert asked == [] and client.aborts == []
    assert 499.0 <= result.retry_waited <= 501.0


def test_the_giveback_is_the_retry_s_and_not_the_work_after_it(monkeypatch):
    """Retries cost 100 s; the model then works on past the original deadline,
    so the deadline asks, at 600 s + 100 s, once — and a refusal aborts."""
    script = [(5.0, _retry(1)), (100.0, _output()), (1200.0, _idle())]
    client, result, asked = _wait(monkeypatch, script, silence=2000.0)
    assert result.status == "timeout"
    assert asked == [700.0] and client.aborts == [SID]       # 600 s + the 100 s owed
    assert 99.0 <= result.retry_waited <= 101.0


def test_two_streaks_are_both_given_back(monkeypatch):
    script = [(5.0, _retry(1)), (100.0, _output()),           # 105 s
              (100.0, _retry(1)), (100.0, _output()),         # 305 s
              (100.0, _idle())]
    _, result, asked = _wait(monkeypatch, script, timeout=250.0, silence=500.0)
    assert result.status == "idle" and asked == []
    assert 199.0 <= result.retry_waited <= 201.0


def test_a_text_part_is_not_the_model_answering(monkeypatch):
    """KC-64's rule: our own prompt comes back as a `text` part (round 106's
    laguna had nine of them with no token out). It does not end a streak."""
    text = {"type": "message.part.updated",
            "properties": {"sessionID": SID, "part": {"type": "text", "id": "t"}}}
    script = [(5.0, _retry(1)), (50.0, text), (50.0, _output()), (50.0, _idle())]
    _, result, _ = _wait(monkeypatch, script, silence=500.0)
    assert 99.0 <= result.retry_waited <= 101.0               # 5 → 105, not 5 → 55


# ── what bounds it ────────────────────────────────────────────────────────────

def test_the_giveback_is_at_most_the_turn_s_own_timeout(monkeypatch):
    """A provider that retries for hours cannot hold the deadline for hours: the
    turn is owed at most its own `timeout` for it, and then the deadline asks."""
    script = [(5.0, _retry(n)) for n in range(1, 400)] + [(5.0, _idle())]
    client, result, asked = _wait(monkeypatch, script, timeout=300.0, silence=400.0)
    assert result.status == "timeout"
    assert asked and asked[0] >= 600.0                        # 300 s + 300 s owed
    assert result.retry_waited <= 300.0 + 1.0


def test_kc64_still_ends_a_streak_of_retries_that_never_answers(monkeypatch):
    """The attempts bound is untouched: the provider that keeps failing ends as
    `ProviderUnavailable`, whatever the deadline is doing."""
    script = [(5.0, _retry(n)) for n in range(1, 30)]
    client, result, asked = _wait(monkeypatch, script, timeout=300.0, silence=400.0,
                                  retries_limit=10)
    assert result.status == "error"
    assert result.error["name"] == "ProviderUnavailable"
    assert client.aborts == [SID] and asked == []


# ── callers that did not ask for a turn clock ─────────────────────────────────

def test_without_a_turn_clock_a_retry_does_not_move_the_deadline(monkeypatch):
    """The probe and every caller of `wait_idle` without `on_deadline`: the
    deadline is the caller's own bound and the loop is what it was."""
    script = [(5.0, _retry(1)), (100.0, _output()), (1200.0, _idle())]
    client, result, asked = _wait(monkeypatch, script, armed=False, silence=2000.0)
    assert result.status == "timeout" and asked == []
    assert 599.0 <= result.elapsed <= 601.0
    assert result.retry_waited == 0.0
