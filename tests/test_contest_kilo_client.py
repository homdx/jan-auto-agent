"""tests/test_contest_kilo_client.py — KC-1: the client, against a fake server.

`tools/contest/kilo_client.py` is a transcription of the calls
`scripts/kilo_hello.py` made live on Kilo 7.6.2 (PROBE.md). These tests point
it at `tests/_kilo_fake.py`, which replays those same routes and those same
payload shapes, so nothing here starts a `kilo` binary or calls a live
provider. Without the module every test below fails at import.

The cases, from the ticket:

  1. two turns into one session — turn 1's idle is not mistaken for turn 2's;
  2. a permission in the middle, both recorded variants;
  3. reply_permission("always") raises before any HTTP;
  4. session.error -> IdleResult.status == "error" with the payload;
  5. timeout -> abort was requested;
  6. tool_parts in order, last_assistant_text == "done";
  7. KiloServer.spawn on a binary that exits immediately raises with the tail;
  8. find_kilo_binary orders 7.10.0 above 7.6.2.

KC-12 (round 51) adds four cases to the wait: `idle_event_timeout` aborts a
session that goes silent for longer than it, long before the overall
`timeout`; any event of the session — not only the ones the wait acts on —
resets that clock, across several windows; a neighbour session on the same
tap does not count at all; and the clock runs from the start of the wait, so
a turn that never emits anything is cut at the window too.

KC-25 (round 64) adds the sixth primitive: `providers()` is `GET /provider`,
the offer decoded as is, so intake can check a roster's `provider/model` pairs
before the round; a non-2xx is a `KiloHttpError` and a non-dict body is a
`ValueError`.

KC-47 (round 91) adds the silence clock's one exception: while this session
still has a `bash` `tool` part in `state.status` `running`, the bound is the
call's own `timeout` plus `idle_event_timeout` of grace, measured from the
part's `running` event. Every case in §1–§4 of the ticket is settled here over
a scripted tap on the fake clock, because they are all about the *gap* after a
`running` event and there is no transport and no wall time to starve.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _kilo_fake  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402

import tools.contest.kilo_client as kilo_client_module  # noqa: E402
from tools.contest.kilo_client import (  # noqa: E402
    EventTap,
    KiloClient,
    KiloHttpError,
    KiloServerError,
    KiloServer,
    SessionRef,
    find_kilo_binary,
)

# This module binds real http.server instances on OS-assigned ephemeral ports
# (the fake, plus the spawned stub binary), so it shares the xdist_group with
# the other port-binding suites: pytest.ini's --dist=loadgroup pins them to one
# worker, so none of them can race another for a port.
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


# ─────────────────────────────────────────────────────────────────────────────
# the session rules the probe sent, and the permission payloads it recorded
# ─────────────────────────────────────────────────────────────────────────────

RULES = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "external_directory", "pattern": "*", "action": "ask"},
    {"permission": "doom_loop", "pattern": "*", "action": "ask"},
]

#: The key sets PROBE.md recorded for a permission.asked payload.
PERMISSION_KEYS = {"id", "sessionID", "permission", "patterns", "metadata", "always", "tool"}
PERMISSION_TOOL_KEYS = {"messageID", "callID"}
REPLIED_KEYS = {"sessionID", "requestID", "reply"}

RECORDED_PERMISSIONS = {
    # spec sent to the fake -> (permission, patterns, always, metadata keys)
    "external_directory": (
        {"permission": "external_directory",
         "patterns": ["/tmp/*"],
         "metadata": {"command": "rm -v /tmp/testfile",
                      "description": "Remove test file with verbose output",
                      "directories": ["/tmp"], "patterns": ["/tmp/*"]}},
        "external_directory", ["/tmp/*"], ["/tmp/*"],
        {"command", "description", "directories", "patterns"},
    ),
    "bash": (
        {"permission": "bash", "patterns": ["rm -v /tmp/testfile"],
         "metadata": {"command": "rm -v /tmp/testfile",
                      "description": "Remove test file at /tmp/testfile"},
         "always": ["rm *"]},
        "bash", ["rm -v /tmp/testfile"], ["rm *"],
        {"command", "description"},
    ),
}


def _reject(event):
    """A stand-in for the KC-3 policy: reject, with the reason the model reads."""
    return "reject", "kilo_client test: not inside the session directory"


@contextmanager
def _probe(tmp_path, scenario, *, reply_timeout: float = _kilo_fake.REPLY_TIMEOUT_S,
           agent=None):
    """A fake server, an attached KiloServer, a client, a tap and a session."""
    directory = str(tmp_path)
    fake = FakeKiloServer(scenario, directory=directory,
                          reply_timeout=reply_timeout).start()
    server = KiloServer.attach(fake.url)
    client = KiloClient(server, directory)
    tap = EventTap(fake.url, directory, str(tmp_path / "events.jsonl")).start()
    # a tap only receives events after its /event connection is open, on the
    # fake as on a real server — wait for it so session.created is not lost.
    # FL-1 (round 84): this used to poll for a fixed 2 s. That is a deadline,
    # and on the operator's 32-worker box a thread opening an HTTP connection
    # can need longer; missing the handshake loses every event the turn
    # emits, which surfaces as a bewildering `status == "timeout"` somewhere
    # far from here. 30 s, and it still fails loudly if the tap never comes.
    deadline = time.monotonic() + 30
    while not fake.subscribers and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake.subscribers, "the tap never connected"
    session = client.create_session("kenary", "hy3:free", rules=RULES,
                                    title="kilo-client-test", agent=agent)
    try:
        yield _Probe(fake, server, client, tap, session, directory)
    finally:
        tap.stop()
        tap.join()
        server.close()
        fake.stop()


class _Probe:
    def __init__(self, fake, server, client, tap, session, directory):
        self.fake = fake
        self.server = server
        self.client = client
        self.tap = tap
        self.session = session
        self.directory = directory


# ─────────────────────────────────────────────────────────────────────────────
# 1 — two turns into one session
# ─────────────────────────────────────────────────────────────────────────────

def test_turn_one_idle_is_not_turn_two_idle(tmp_path):
    """The exact bug the probe hit: a wait that rescaned the event list from
    the start answered turn 2 with turn 1's session.idle ("idle after 0.0 s",
    file unchanged). Turn 2 holds its idle back by `delay`, so the cursor is
    observable: nothing idles between the two prompts, and wait_idle for turn
    2 cannot return before turn 2's idle was actually emitted."""
    scenario = {"turns": [
        {"events": ["busy", "file.edited", "idle_status", "idle", "session.turn.close"],
         "assistant": "created hello.txt",
         "tool_parts": [{"tool": "write", "status": "completed",
                         "input": {"path": "hello.txt"}, "output": "written"}]},
        {"events": ["busy", "file.edited", "idle"], "assistant": "done", "delay": 0.6},
    ]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "create hello.txt with Hello world")
        res1 = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                  on_question=lambda event: None)
        assert res1.status == "idle"
        assert res1.permissions == []
        # FL-1 (round 84): the deadline is far away and the bound is in the
        # middle of the gap, so a loaded box cannot blur "idled" into "hit
        # the deadline" the way a 5 s deadline under a 3 s bound could.
        assert 0.0 <= res1.elapsed < 30.0

        # turn 1's idle is consumed, not just matched: a fresh wait with the
        # same predicate finds nothing, and turn 2 has not gone idle yet.
        idle_pred = lambda e: e.get("type") == "session.idle"  # noqa: E731
        assert h.tap.wait(idle_pred, 0.3) is None
        assert len(h.fake.events_of("session.idle")) == 1

        h.client.prompt(h.session, "append the model id as a second line")
        started = time.monotonic()
        res2 = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                  on_question=lambda event: None)
        assert res2.status == "idle"
        # turn 2's idle was held back by 0.6 s, so returning faster proves the
        # wait answered with a stale event.
        assert res2.elapsed >= 0.55
        assert len(h.fake.events_of("session.idle")) == 2

        assert h.client.last_assistant_text(h.session) == "done"
        assert len(h.client.messages(h.session)) == 4  # user + assistant, twice



# KC-63 (round 106, mimo-v2-5): Kilo follows a session.error with two
# session.idle for the same session. The next turn's wait must not read them.
_ERROR_THEN_TURN = {"turns": [
    {"error": "UnknownError", "idles_after_error": 2},
    {"events": ["busy", "file.edited", "idle"], "assistant": "done", "delay": 0.6},
]}


def _error_then_leftovers(h):
    """Turn 1 errors; returns once both leftover idles sit in the tap."""
    h.client.prompt(h.session, "first")
    res1 = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                              on_question=lambda event: None)
    assert res1.status == "error"
    assert res1.stale_skipped == 0
    deadline = time.monotonic() + 30
    while len(h.fake.events_of("session.idle")) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(h.fake.events_of("session.idle")) == 2
    # the tap has to hold them too, not only the fake
    while (sum(1 for e in list(h.tap.events) if e.get("type") == "session.idle") < 2
           and time.monotonic() < deadline):
        time.sleep(0.02)


def test_a_turn_does_not_end_on_the_idles_left_after_an_error(tmp_path):
    """With a mark taken before prompt 2, its wait skips the two leftover
    idles and ends on turn 2's own idle, held back 0.6 s."""
    with _probe(tmp_path, _ERROR_THEN_TURN) as h:
        _error_then_leftovers(h)
        since = h.tap.mark()
        h.client.prompt(h.session, "retry")
        res2 = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                  on_question=lambda event: None, since=since)
        assert res2.status == "idle"
        assert res2.elapsed >= 0.55
        assert res2.stale_skipped == 2
        assert len(h.fake.events_of("session.idle")) == 3


def test_without_since_the_leftover_idle_still_ends_the_wait(tmp_path):
    """The default path is unchanged: no mark, the first leftover idle ends
    the wait at once. This is also what makes the test above a real catch."""
    with _probe(tmp_path, _ERROR_THEN_TURN) as h:
        _error_then_leftovers(h)
        h.client.prompt(h.session, "retry")
        res2 = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                  on_question=lambda event: None)
        assert res2.status == "idle"
        assert res2.elapsed < 0.55
        assert res2.stale_skipped == 0


def test_skip_to_never_moves_the_cursor_backward(tmp_path):
    """A mark below the cursor is a no-op, never a replay; a mark past the
    end stops at the end."""
    tap = EventTap("http://127.0.0.1:9", str(tmp_path), str(tmp_path / "events.jsonl"))
    tap.events.extend({"type": "session.idle", "properties": {}} for _ in range(3))
    assert tap.mark() == 3
    assert tap.wait(lambda e: True, 0) is not None
    assert tap.cursor == 1
    assert tap.skip_to(0) == 0
    assert tap.cursor == 1
    assert tap.skip_to(10**9) == 2
    assert tap.cursor == 3
    assert tap.skip_to(3) == 0


def test_an_idle_without_busy_is_logged_and_not_changed(tmp_path, caplog):
    """KC-63 diagnostic: a turn that idles without going busy logs one
    warning, and its result is the plain idle it always was."""
    scenario = {"turns": [{"events": ["idle"], "assistant": "done"}]}
    caplog.set_level(logging.WARNING, logger="tools.contest.kilo_client")
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "first")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
    assert res.status == "idle"
    assert res.stale_skipped == 0
    lines = [r.getMessage() for r in caplog.records if "idle without busy" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith(h.session.id)


def test_wait_idle_sees_only_its_own_session(tmp_path):
    """Events for a different session in the same directory are not ours."""
    scenario = {"turns": [
        {"events": ["busy", "idle"], "assistant": "one"},
        {"events": ["busy", "idle"], "assistant": "two"},
    ]}
    with _probe(tmp_path, scenario) as h:
        other = h.client.create_session("kenary", "hy3:free", rules=RULES,
                                        title="the other one")
        h.client.prompt(h.session, "first")
        h.client.prompt(other, "also first")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
        assert res.status == "idle"
        assert h.client.last_assistant_text(h.session) == "one"
        assert h.client.last_assistant_text(other) == "one"
        # both sessions went idle; the wait only waited on ours
        assert len(h.fake.events_of("session.idle")) == 2


def test_tap_closed_wakes_a_waiting_caller(tmp_path):
    """A dropped stream surfaces as status "closed", not a full timeout."""
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.fake.stop()          # wakes the stream with the sentinel
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
    assert res.status == "closed"
    assert isinstance(res.error, str) and res.error
    assert h.tap.events[-1]["type"] == "tap.closed"


# ─────────────────────────────────────────────────────────────────────────────
# 2 — a permission in the middle, both recorded variants
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", sorted(RECORDED_PERMISSIONS))
def test_permission_in_the_middle_is_answered_and_idle_is_reached(tmp_path, variant):
    """The policy is a callback: the client answers what it says and keeps
    waiting. The event it hands over carries PROBE.md's recorded keys, and the
    reply body sent over HTTP is {"reply", "message"}."""
    spec, permission, patterns, always, metadata_keys = RECORDED_PERMISSIONS[variant]
    scenario = {"turns": [{
        "events": ["busy", "file.edited", "idle_status", "idle", "session.turn.close"],
        "permission": spec,
        "assistant": "done",
    }]}
    seen = []

    def on_permission(event):
        seen.append(event)
        return _reject(event)

    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "delete /tmp/testfile")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=on_permission,
                                 on_question=lambda event: None)

    assert res.status == "idle"
    assert len(res.permissions) == 1
    assert len(seen) == 1

    event = seen[0]
    assert event["type"] == "permission.asked"
    props = event["properties"]
    assert set(props) == PERMISSION_KEYS
    assert props["sessionID"] == h.session.id
    assert props["permission"] == permission
    assert props["patterns"] == patterns
    assert props["always"] == always
    assert set(props["metadata"]) == metadata_keys
    assert props["metadata"]["command"] == "rm -v /tmp/testfile"
    assert props["id"].startswith("per_")
    assert set(props["tool"]) == PERMISSION_TOOL_KEYS

    replies = h.fake.calls(method="POST", prefix="/permission/")
    assert len(replies) == 1
    assert replies[0]["body"] == {
        "reply": "reject",
        "message": "kilo_client test: not inside the session directory",
    }
    assert replies[0]["query"]["directory"] == h.directory

    answered = h.fake.events_of("permission.replied")
    assert len(answered) == 1
    assert set(answered[0]["properties"]) == REPLIED_KEYS
    assert answered[0]["properties"]["requestID"] == props["id"]
    assert answered[0]["properties"]["reply"] == "reject"

    # asked, then replied, then idle — idle is last, and only after the reply
    order = [e["type"] for e in h.fake.events]
    assert order.index("permission.asked") < order.index("permission.replied")
    assert order.index("permission.replied") < order.index("session.idle")


def test_reply_permission_falls_back_to_the_legacy_route(tmp_path):
    """A 404 from /permission/{id}/reply is the probe's fallback trigger."""
    spec, *_ = RECORDED_PERMISSIONS["external_directory"]
    scenario = {"turns": [{"events": ["busy", "idle"], "permission": spec,
                           "assistant": "done"}],
                 "permission_endpoint_404": True}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "delete /tmp/testfile")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
    assert res.status == "idle"
    primary = h.fake.calls(method="POST", prefix="/permission/")
    legacy = h.fake.calls(method="POST",
                          prefix=f"/session/{h.session.id}/permissions/")
    assert len(primary) == 1 and len(legacy) == 1
    assert primary[0]["body"]["reply"] == "reject"
    assert legacy[0]["body"] == {"response": "reject"}


def test_reply_permission_always_raises_before_any_http(tmp_path):
    """`always` would whitelist the pattern for the rest of the session, so it
    is refused in code, not by the server. No request may have been made."""
    with _probe(tmp_path, {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]}) as h:
        made = h.fake.calls(method="POST")
        with pytest.raises(ValueError, match="never 'always'"):
            h.client.reply_permission(h.session, "per_fake000001", "always",
                                      "the model insisted")
        assert h.fake.calls(method="POST") == made

        with pytest.raises(ValueError, match="one of"):
            h.client.reply_permission(h.session, "per_fake000001", "maybe", "nope")
        with pytest.raises(ValueError, match="permission_id"):
            h.client.reply_permission(h.session, "", "once", "empty id")
        assert h.fake.calls(method="POST") == made


# ─────────────────────────────────────────────────────────────────────────────
# 3 — session.error, timeout
# ─────────────────────────────────────────────────────────────────────────────

def test_session_error_yields_error_status_with_the_payload(tmp_path):
    error = {"name": "ModelError", "data": {"message": "the model exploded"}}
    scenario = {"turns": [{"events": ["busy"], "error": error}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "summarise the module")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
        # a turn that errors never sends an assistant message
        assistant = h.client.last_assistant_text(h.session)
    assert res.status == "error"
    assert res.error == error
    assert res.elapsed >= 0.0
    emitted = h.fake.events_of("session.error")
    assert len(emitted) == 1
    assert set(emitted[0]["properties"]) == {"sessionID", "error"}
    assert emitted[0]["properties"]["error"] == error
    assert h.fake.events_of("session.idle") == []
    assert assistant == ""


def test_timeout_aborts_the_session(tmp_path):
    """Nothing idles: the wait gives up and sends abort before returning."""
    scenario = {"turns": [{"events": ["busy"], "idle": False, "assistant": "stalled"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 0.5, on_permission=_reject,
                                 on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.error is None
    # FL-1 (round 84): a bound that only says "it did not hang" — the 0.5 s
    # deadline is what is under test and `status` already reports it.
    assert 0.0 <= res.elapsed < 30.0
    assert elapsed < 30.0

    aborts = h.fake.calls(method="POST", path="/abort")
    assert len(aborts) == 1
    assert aborts[0]["path"] == f"/session/{h.session.id}/abort"
    assert aborts[0]["body"] is None
    assert aborts[0]["query"]["directory"] == h.directory
    assert h.fake.sessions()[0].aborted is True


# ─────────────────────────────────────────────────────────────────────────────
# 3b — KC-12: the silence clock, separate from the overall timeout
# ─────────────────────────────────────────────────────────────────────────────

def test_wait_idle_aborts_on_stall(tmp_path):
    """A session silent for longer than ``idle_event_timeout`` is aborted
    well before the overall ``timeout`` elapses. The fake emits one
    ``session.status busy`` and then goes silent for 60 s without ever
    idling, so the wait must be bounded by the 0.5 s silence window — not by
    the 60 s pause it would have waited out, and not by the 120 s deadline.

    FL-1 (round 84): the pause was 2 s under a 1.5 s bound, i.e. 1 s of
    slack between "took the window" and "took the pause", and the operator's
    32-worker stress run does not honour 1 s. Widening the *slow* path is
    free — a green run still returns at ~0.5 s — and it buys 19.5 s of
    slack. (`FakeKiloServer._sleep` wakes on `stop()`, so the 60 s pause
    costs nothing at teardown.)"""
    scenario = {"turns": [{"pause_before_idle_sec": 60, "assistant": "still running"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "run the slow command")
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 120.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.error is None
    assert res.permissions == [] and res.questions == []
    assert res.elapsed < 20.0, res.elapsed     # the 0.5 s window, not the 60 s pause
    assert elapsed < 20.0
    # the shape's heartbeat went out, no idle ever did, and one abort was sent
    assert h.fake.events_of("session.status")
    assert h.fake.events_of("session.idle") == []
    assert h.fake.recorded_abort_for(h.session.id)


class _ScriptedTap:
    """A tap that hands ``wait_idle`` a scripted stream on a fake clock.

    FL-1 (round 84). The integration version of this claim
    (``test_events_of_the_session_keep_a_turn_alive`` and the one that used
    to live here) has a shape that cannot be made load-proof:

      a regression cuts the turn at ``window``, so the only proof is
      *surviving* longer than ``window``, and the turn survives only if no
      gap between two **delivered** events exceeds it — so the window, the
      test's runtime and its tolerance for a starved box are one number.

    Widening it just makes the test slower, and it still failed the
    operator's 32-worker stress run at eight seconds, because what starves
    is the delivery path — a fake HTTP server, an SSE stream, a reader
    thread — not the thread doing the emitting.

    So the claim is settled here instead, with neither. There is no
    transport: this stands in for ``EventTap`` and releases the next event
    when ``wait_idle`` asks for one. And there is no wall clock: time only
    moves when this object moves it, by exactly the amount ``wait_idle``
    said it was willing to wait. A starved box cannot change the outcome
    because nothing here is measured against real time.

    Faithful to ``EventTap.wait`` in the two ways that matter: the cursor
    advances past every event looked at, matching or not, and an event the
    predicate rejects is *consumed without being returned* — the caller
    keeps waiting. That second one is what makes a regression visible: a
    ``wait_idle`` that did not count ``session.status`` as an event of the
    session would consume the beats, see nothing, and let its silence clock
    run out.
    """

    def __init__(self, events, *, every: float, clock):
        self._events = list(events)
        self._every = float(every)
        self._clock = clock
        self._next_at = clock.now + self._every

    def wait(self, pred, timeout):
        deadline = self._clock.now + max(0.0, float(timeout))
        while True:
            if not self._events or self._next_at > deadline:
                # nothing more is due inside what the caller will wait for:
                # the stream is silent for the whole of it
                self._clock.now = deadline
                return None
            self._clock.now = self._next_at
            self._next_at = self._clock.now + self._every
            event = self._events.pop(0)
            if pred(event):
                return event
            # consumed, not returned — exactly what EventTap does with an
            # event its caller's predicate rejects


class _FakeClock:
    """``time.monotonic`` that only moves when a test moves it."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now


def _silence_clock_probe(monkeypatch, events, *, window, every, aborts=None):
    """Run ``KiloClient.wait_idle`` over a scripted stream on a fake clock."""
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            (aborts if aborts is not None else []).append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m",
                         directory="/nowhere")
    tap = _ScriptedTap(events, every=every, clock=clock)
    return _Client().wait_idle(tap, session, 10_000.0, idle_event_timeout=window,
                               on_permission=_reject, on_question=lambda event: None)


def _ev(etype, session_id="ses_probe", **props):
    return {"type": etype, "properties": {"sessionID": session_id, **props}}


def test_events_of_the_session_keep_wait_idle_alive(monkeypatch):
    """The silence clock counts every event of the session, not only the ones
    ``wait_idle`` acts on: a ``session.status busy`` every 0.4 s carries the
    turn through eight 1 s windows, and it still reaches idle — many windows
    past the first one. No abort, no early cut-off."""
    window, every = 1.0, 0.4
    beats = [_ev("session.status", status="busy") for _ in range(20)]
    aborts: list = []

    res = _silence_clock_probe(monkeypatch, beats + [_ev("session.idle")],
                               window=window, every=every, aborts=aborts)

    assert res.status == "idle"
    # it really waited the beats out — 21 events at 0.4 s is 8.4 s, more than
    # eight times the window it would have been cut at without them
    assert res.elapsed > window * 8, res.elapsed
    assert aborts == []


def test_a_beat_the_wait_does_not_count_lets_the_silence_clock_run_out(monkeypatch):
    """The other side of the same claim, and the regression guard: an event
    the predicate rejects is consumed without waking the wait, so a
    ``wait_idle`` that stopped counting ``session.status`` would time out at
    the window — which is exactly what this test sees when the beats belong
    to *another* session."""
    window, every = 1.0, 0.4
    beats = [_ev("session.status", session_id="ses_someone_else", status="busy")
             for _ in range(20)]
    aborts: list = []

    res = _silence_clock_probe(monkeypatch, beats + [_ev("session.idle")],
                               window=window, every=every, aborts=aborts)

    assert res.status == "timeout"
    # cut at the window, not after the neighbour's 8 s of chatter
    assert res.elapsed == window, res.elapsed
    assert aborts == ["ses_probe"]


def test_the_silence_clock_is_off_when_no_idle_event_timeout_is_given(monkeypatch):
    """``idle_event_timeout=None`` is KC-1's wait, event for event: a silent
    stream runs to the overall deadline instead of being cut at a window."""
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)
    aborts: list = []

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            aborts.append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m",
                         directory="/nowhere")
    tap = _ScriptedTap([], every=1.0, clock=clock)
    res = _Client().wait_idle(tap, session, 42.0, idle_event_timeout=None,
                               on_permission=_reject, on_question=lambda event: None)

    assert res.status == "timeout"
    assert res.elapsed == 42.0
    assert aborts == ["ses_probe"]


# ─────────────────────────────────────────────────────────────────────────────
# 3c — KC-36: the turn deadline asks before it kills
# ─────────────────────────────────────────────────────────────────────────────

def _deadline_clock_probe(monkeypatch, events, *, timeout, every, on_deadline,
                          idle_event_timeout=None, aborts=None):
    """``wait_idle`` over a scripted stream on a fake clock, with the KC-36
    deadline callback wired in. The probe of the silence clock above, with the
    overall ``timeout`` and the callback both caller-chosen."""
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            (aborts if aborts is not None else []).append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m",
                         directory="/nowhere")
    tap = _ScriptedTap(events, every=every, clock=clock)
    return _Client().wait_idle(tap, session, timeout,
                               idle_event_timeout=idle_event_timeout,
                               on_permission=_reject, on_question=lambda event: None,
                               on_deadline=on_deadline)


def test_on_deadline_moves_the_deadline_while_it_grants(monkeypatch):
    """30 granted twice, then nothing: the wait outlives its own deadline and
    ends at the last extended one, the callback is called at the deadline and
    again 30 s past each grant, and one ``abort`` goes out at the end."""
    grants = iter([30.0, 30.0, None])
    calls: list = []
    aborts: list = []

    def on_deadline(elapsed):
        calls.append(round(elapsed, 6))
        return next(grants)

    res = _deadline_clock_probe(monkeypatch, [], timeout=60.0, every=1.0,
                                on_deadline=on_deadline, aborts=aborts)

    assert res.status == "timeout"
    # the elapsed seconds at each ask: the deadline, then 30 past each grant
    assert calls == [60.0, 90.0, 120.0], calls
    assert res.elapsed >= 60.0 + 30.0 + 30.0, res.elapsed
    assert aborts == ["ses_probe"]


@pytest.mark.parametrize("answer", [0, 0.0, -5, "30", [], {}, None])
def test_a_deadline_answer_that_is_not_positive_aborts_at_once(monkeypatch, answer):
    """``0``, a negative number, a non-number and ``None`` all abort exactly as
    today: at the original deadline, with the callback asked exactly once."""
    calls: list = []
    aborts: list = []

    def on_deadline(elapsed):
        calls.append(elapsed)
        return answer

    res = _deadline_clock_probe(monkeypatch, [], timeout=60.0, every=1.0,
                                on_deadline=on_deadline, aborts=aborts)

    assert res.status == "timeout"
    assert calls == [60.0], calls
    assert res.elapsed == 60.0, res.elapsed
    assert aborts == ["ses_probe"]


def test_a_deadline_callback_that_raises_aborts_and_logs(monkeypatch, caplog):
    """A callback that raises — a broken churn reader, a missing worktree — is
    logged here and never raised into a round, and the wait aborts at the
    original deadline as if the callback had never been given."""
    caplog.set_level(logging.WARNING, logger="tools.contest.kilo_client")
    aborts: list = []

    def on_deadline(elapsed):
        raise RuntimeError("worktree is gone")

    res = _deadline_clock_probe(monkeypatch, [], timeout=60.0, every=1.0,
                                on_deadline=on_deadline, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == 60.0, res.elapsed
    assert aborts == ["ses_probe"]
    assert any("worktree is gone" in record.getMessage() for record in caplog.records), \
        caplog.text


def test_an_extension_never_resurrects_a_session_that_went_quiet(monkeypatch):
    """The silence clock keeps racing the extended deadline. Beats every 2 s
    with a 10 s window carry the turn through the 60 s deadline, where 30 is
    granted and the deadline moves to 90; the beats then stop at 70, and the
    window — not the extension — ends the wait at 80."""
    window, every = 10.0, 2.0
    beats = [_ev("session.status", status="busy") for _ in range(35)]   # 2..70 s
    calls: list = []
    aborts: list = []

    def on_deadline(elapsed):
        calls.append(elapsed)
        return 30.0

    res = _deadline_clock_probe(monkeypatch, beats, timeout=60.0, every=every,
                                on_deadline=on_deadline,
                                idle_event_timeout=window, aborts=aborts)

    assert res.status == "timeout"
    # asked once, at the deadline — never again, at the extended one
    assert calls == [60.0], calls
    # the silence clock won: 70 s of beats plus the 10 s window, 10 s inside
    # the deadline the grant had just moved to
    assert res.elapsed == 80.0, res.elapsed
    assert res.elapsed < 60.0 + 30.0, res.elapsed
    assert aborts == ["ses_probe"]


def test_a_neighbours_events_do_not_keep_a_silent_session_alive(tmp_path):
    """The clock is the session's own: the tap reads every session of the
    directory, so a chatty neighbour must not push a silent one past its
    silence window. The silent session is aborted at 0.5 s while the other
    keeps emitting.

    FL-1 (round 84): same widening as the sibling stall test above — the
    pause and the deadline move out, the bound sits in the middle, and the
    neighbour chatters until teardown rather than for a counted 1 s, so a
    loaded box cannot run it out of beats before the silent session is
    cut."""
    scenario = {"turns": [{"pause_before_idle_sec": 60}]}
    with _probe(tmp_path, scenario) as h:
        neighbour = h.client.create_session("kenary", "hy3:free", rules=RULES,
                                            title="the chatty one")
        h.client.prompt(h.session, "go quiet")

        def chat():
            while not h.fake._stop.is_set():
                time.sleep(0.1)
                h.fake._emit({"type": "session.status",
                              "properties": {"sessionID": neighbour.id,
                                             "status": "busy"}})

        threading.Thread(target=chat, daemon=True).start()
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 120.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.elapsed < 20.0, res.elapsed
    assert elapsed < 20.0
    assert h.fake.recorded_abort_for(h.session.id)
    assert not h.fake.recorded_abort_for(neighbour.id)


def test_a_turn_with_no_event_at_all_is_cut_at_the_window(tmp_path):
    """The clock starts when the wait does, not at the first event: a second
    turn whose prompt is accepted and then answered with nothing — no busy,
    no idle — is a stall at 0.5 s, not a turn that waits out the 10 s
    deadline. (A first turn always has ``session.created`` on the tap ahead
    of it, which is why this needs a second one.)

    FL-1 (round 84): the deadline moves out to 120 s and the bound to 20 s —
    the two paths this test tells apart are 0.5 s and 120 s, so nothing is
    lost and a loaded box has 19.5 s of room. The first turn's own window
    moves out too: it is scaffolding here, not the claim, and it must not be
    able to stall the turn it is only setting up."""
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "one"},
                          {"events": [], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "first")
        res = h.client.wait_idle(h.tap, h.session, 120.0, idle_event_timeout=30.0,
                                 on_permission=_reject, on_question=lambda event: None)
        assert res.status == "idle"
        h.client.prompt(h.session, "second")
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 120.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.elapsed < 20.0, res.elapsed
    assert elapsed < 20.0
    assert h.fake.recorded_abort_for(h.session.id)


# ─────────────────────────────────────────────────────────────────────────────
# 3c — KC-47: a `bash` call that is still running is not silence
# ─────────────────────────────────────────────────────────────────────────────

class _TimedScriptedTap:
    """A scripted stream at absolute times, on the fake clock.

    `_ScriptedTap` spaces its events an equal `every` apart, which cannot say
    "one event, then nothing at all, then one event" — and KC-47's claim *is*
    the gap after a `bash` part goes `running`. So the events are seconds
    after the wait starts, and nothing here is measured against real time: a
    starved box cannot change an outcome, because there is no transport and no
    wall clock to drift.

    Faithful to `EventTap.wait` in the two ways that matter: the cursor
    advances past every event looked at, matching or not, and an event the
    predicate rejects is consumed without being returned. That second one is
    what makes §3's neighbour case visible.
    """

    def __init__(self, events, *, clock):
        self._events = sorted((clock.now + float(at), event)
                              for at, event in events)
        self._clock = clock

    def wait(self, pred, timeout):
        deadline = self._clock.now + max(0.0, float(timeout))
        while self._events and self._events[0][0] <= deadline:
            at, event = self._events.pop(0)
            self._clock.now = at
            if pred(event):
                return event
        self._clock.now = deadline
        return None


def _kc47_probe(monkeypatch, events, *, window, timeout=10_000.0, aborts=None):
    """`KiloClient.wait_idle` over a KC-47 stream on the fake clock.

    `window` is the `idle_event_timeout`; `timeout` is the overall wait, the
    runner's `turn_timeout_sec`. `aborts` records every session
    `_abort_quietly` was sent for, so an assertion reads it rather than
    re-deriving it.
    """
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            (aborts if aborts is not None else []).append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m",
                         directory="/nowhere")
    return _Client().wait_idle(_TimedScriptedTap(events, clock=clock), session,
                               timeout, idle_event_timeout=window,
                               on_permission=_reject,
                               on_question=lambda event: None)


def _part_updated(part_id, *, tool="bash", status="running", timeout_ms=600000,
                  command="python3 -m pytest tests -n 4", session_id="ses_probe",
                  **state):
    """One `message.part.updated` carrying a `tool` part, in the server's shape.

    PROBE.md's part: an `id`, `type` `tool` and the tool's name, with a
    `state` holding `status` and `input`. `timeout_ms=None` omits the key,
    which is how a call that asked for no timeout arrives.
    """
    input_ = {"command": command}
    if timeout_ms is not None:
        input_["timeout"] = timeout_ms
    part = {"id": part_id, "type": "tool", "tool": tool,
            "messageID": f"msg_{part_id}", "callID": f"call_{part_id}",
            "state": {"status": status, "input": input_, **state}}
    return _ev("message.part.updated", session_id=session_id, part=part)


def test_a_running_bash_part_holds_the_silence_clock_open(monkeypatch):
    """The round 86 fix: a `bash` call with a 600 s `timeout` is allowed its
    600 s plus the window of grace, so silence of `idle_event_timeout + 1`
    inside it is the call running, not a stall. `session.idle` arrives and no
    abort is sent — the behaviour before KC-47 cut this turn at the window."""
    window = 1.0
    aborts: list = []
    events = [
        (1.0, _part_updated("p1", timeout_ms=600000)),
        (3.0, _part_updated("p1", status="completed", timeout_ms=600000)),
        (3.1, _ev("session.idle")),
    ]

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "idle"
    assert res.open_tool is None
    assert aborts == []
    assert res.elapsed == pytest.approx(3.1)


def test_a_silence_past_a_bash_parts_own_timeout_stalls_with_the_call_named(monkeypatch):
    """The same call, let run past its own `timeout` plus the grace: a stall,
    one abort, and `open_tool` carrying the command so the next reader does
    not have to dig in events.jsonl."""
    window = 1.0
    command = "python3 -m pytest tests -n 4"
    aborts: list = []
    events = [(1.0, _part_updated("p1", timeout_ms=600000, command=command))]

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert aborts == ["ses_probe"]
    # past 600 + the window of grace, measured from the `running` event
    assert res.elapsed == pytest.approx(1.0 + 600.0 + window)
    assert res.open_tool["tool"] == "bash"
    assert res.open_tool["command"] == command
    assert res.open_tool["running_for"] == pytest.approx(601.0)


def test_a_bash_part_with_no_timeout_uses_the_module_default_not_forever(monkeypatch):
    """A call that names no `timeout` still ends, at Kilo's own `bash`
    default. The constant is patched down so the claim is readable in numbers
    and, more importantly, so "forever" cannot pass: an unbounded part would
    run to the 10 000 s wait timeout instead of 2.5 s."""
    monkeypatch.setattr(kilo_client_module, "_KILO_BASH_DEFAULT_TIMEOUT_MS", 500)
    window = 1.0
    aborts: list = []
    events = [(1.0, _part_updated("p1", timeout_ms=None))]

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert aborts == ["ses_probe"]
    assert res.elapsed == pytest.approx(1.0 + 0.5 + window)
    assert res.open_tool is not None and res.open_tool["tool"] == "bash"


@pytest.mark.parametrize("tool", ["task", "read", "edit", "write", "bash"])
def test_a_non_bash_tool_part_is_not_a_suspension(monkeypatch, tool):
    """§3: only `bash` holds the silence clock open. Round 64's `nex-n2-5-pro`
    sat in a failed `task` — that is KC-42's — and an `edit` or a `read`
    keeps KC-12's window, cut at it event for event.

    `bash` is in the list because a part whose `state.status` is not
    `running` or `pending` is not open either, and it must not widen anything.
    """
    if tool == "bash":
        events = [(1.0, _part_updated("p1", status="completed", timeout_ms=600000))]
    else:
        events = [(1.0, _part_updated("p1", tool=tool, timeout_ms=600000))]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=1.0, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(2.0)   # 1.0 to the part, 1.0 of silence
    assert res.open_tool is None
    assert aborts == ["ses_probe"]


def test_two_bash_parts_only_the_still_running_one_sets_the_bound(monkeypatch):
    """Two calls open: the widest one binds, and closing it drops the bound
    to the survivor's own. Each call's bound runs from its own `running`
    event, so after the closure it is p2's 1.1 + 1 + the window = 3.1 — below
    KC-12's 2.5 + the window from the closing event, which is what binds: an
    open call never leaves a turn less room than KC-12 would. With p1 never
    closed the stall would land at 1.0 + 3 + the window = 5.0."""
    window = 1.0
    events = [
        (1.0, _part_updated("p1", timeout_ms=3000, command="python3 -m pytest tests")),
        (1.1, _part_updated("p2", timeout_ms=1000, command="python3 -m pytest .smoke_tests/")),
        (2.5, _part_updated("p1", status="completed", timeout_ms=3000)),
    ]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(2.5 + window)
    assert res.open_tool["tool"] == "bash"
    assert res.open_tool["command"] == "python3 -m pytest .smoke_tests/"
    assert res.open_tool["running_for"] == pytest.approx(2.4)


def test_the_calls_bound_runs_from_its_running_event_not_from_later_updates(monkeypatch):
    """A running call's later updates (its output streaming in) reset KC-12's
    clock like any event, but not the call's own bound: that runs from the
    first `running` — 1.0 + 3 + the window = 5.0, not 2.0 + 3 + the window."""
    window = 1.0
    events = [
        (1.0, _part_updated("p1", timeout_ms=3000)),
        (2.0, _part_updated("p1", timeout_ms=3000, metadata={"output": "...."})),
    ]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(1.0 + 3.0 + window)
    assert res.open_tool["running_for"] == pytest.approx(4.0)
    assert aborts == ["ses_probe"]


def test_with_every_bash_part_closed_the_bound_is_kc12s_again(monkeypatch):
    """Both calls finish: the wait is KC-12's again, event for event, and the
    stall names nothing — an empty `open_tool`, exactly as a KC-12 silence
    did before KC-47."""
    window = 1.0
    events = [
        (1.0, _part_updated("p1", timeout_ms=600000)),
        (1.2, _part_updated("p2", timeout_ms=600000)),
        (1.4, _part_updated("p1", status="completed", timeout_ms=600000)),
        (1.6, _part_updated("p2", status="completed", timeout_ms=600000)),
    ]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(1.6 + window)
    assert res.open_tool is None
    assert aborts == ["ses_probe"]


def test_a_neighbour_sessions_bash_part_does_not_move_this_sessions_clock(monkeypatch):
    """§3: the tap reads every session of the directory, and this session's
    clock is its own. A neighbour's 600 s `bash` is consumed without waking
    the wait, and the silence still runs out at the window."""
    window = 1.0
    events = [(1.0, _part_updated("p1", session_id="ses_someone_else", timeout_ms=600000))]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=window, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(window)
    assert res.open_tool is None
    assert aborts == ["ses_probe"]


def test_the_overall_timeout_still_bounds_a_wider_bash_call(monkeypatch):
    """An agent that asks for a 40-minute `bash` on a 30-minute turn gets the
    turn's end. The bound is widened only against the silence clock, and
    `open_tool` stays empty: the turn deadline won, so the answer is "the turn
    never idled", not "the call was slow"."""
    window = 1.0
    events = [(1.0, _part_updated("p1", timeout_ms=2_400_000))]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=window, timeout=10.0, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(10.0)
    assert res.open_tool is None
    assert aborts == ["ses_probe"]


def test_a_malformed_part_updates_the_map_without_raising(monkeypatch):
    """Fail-open: a `message.part.updated` whose part is missing, not a dict,
    without an id, or whose `state` is not a dict must not raise into a round
    and must not widen anything — the silence clock stays KC-12's, which is
    what any shape the server sends should degrade to."""
    malformed = [
        _ev("message.part.updated"),                                            # no part
        _ev("message.part.updated", part=None),
        _ev("message.part.updated", part=["not", "a", "dict"]),
        _ev("message.part.updated", part={"type": "tool", "tool": "bash"}),     # no id
        _ev("message.part.updated", part={"id": "p1", "type": "tool",
                                          "tool": "bash", "state": "running"}),  # state not a dict
        _ev("message.part.updated", part={"id": "p1", "type": "tool",
                                          "tool": "bash",
                                          "state": {"status": "idle"}}),        # not open
    ]
    events = [(1.0 + 0.1 * i, event) for i, event in enumerate(malformed)]
    aborts: list = []

    res = _kc47_probe(monkeypatch, events, window=1.0, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(1.5 + 1.0)   # the last event, plus the window
    assert res.open_tool is None
    assert aborts == ["ses_probe"]


def test_a_non_numeric_timeout_falls_back_to_the_module_default(monkeypatch):
    """A `timeout` the model spelled as a word is not a number, so the call
    takes Kilo's own default rather than raising — and rather than being
    unbounded. The constant is patched down, as above, so the claim is a
    number instead of "forever"."""
    monkeypatch.setattr(kilo_client_module, "_KILO_BASH_DEFAULT_TIMEOUT_MS", 500)
    part = {"id": "p1", "type": "tool", "tool": "bash",
            "state": {"status": "running",
                      "input": {"command": "python3 -m pytest tests",
                                "timeout": "soon"}}}
    aborts: list = []

    res = _kc47_probe(monkeypatch, [(1.0, _ev("message.part.updated", part=part))],
                      window=1.0, aborts=aborts)

    assert res.status == "timeout"
    assert res.elapsed == pytest.approx(1.0 + 0.5 + 1.0)
    assert res.open_tool["tool"] == "bash"
    assert res.open_tool["command"] == "python3 -m pytest tests"
    assert res.open_tool["running_for"] == pytest.approx(1.5)


# ─────────────────────────────────────────────────────────────────────────────
# 4 — reading the turn back
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_parts_and_last_assistant_text(tmp_path):
    """The tool call and its result come from GET /message, not from /event:
    session.next.tool.* never appeared on the stream in this build."""
    rejected = "The user rejected permission to use this specific tool call with " \
               "the following feedback: kilo_client test: not inside the session directory"
    scenario = {"turns": [{
        "events": ["busy", "file.edited", "idle"],
        "tool_parts": [
            {"type": "tool", "tool": "bash",
             "state": {"status": "error",
                       "input": {"command": "rm -v /tmp/testfile"},
                       "output": rejected}},
            {"tool": "write", "status": "completed",
             "input": {"path": "hello.txt", "content": "Hello world"},
             "output": "wrote 11 bytes"},
        ],
        "assistant": "done",
        "diff": [{"file": "hello.txt", "patch": "+Hello world"}],
        "info": {"cost": 0.021, "tokens": {"total": 4100, "input": 4000, "output": 100}},
    }]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "write hello.txt")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
        assert res.status == "idle"

        parts = h.client.tool_parts(h.session)
        assert [p["tool"] for p in parts] == ["bash", "write"]
        assert all(p["type"] == "tool" for p in parts)
        assert parts[0]["state"]["status"] == "error"
        assert parts[0]["state"]["input"] == {"command": "rm -v /tmp/testfile"}
        assert parts[0]["state"]["output"] == rejected
        assert "error" not in parts[0]["state"]
        assert parts[1]["state"] == {"status": "completed",
                                     "input": {"path": "hello.txt", "content": "Hello world"},
                                     "output": "wrote 11 bytes"}
        assert h.client.last_assistant_text(h.session) == "done"

        messages = h.client.messages(h.session)
        assert [m["info"]["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["parts"] == [{"type": "text", "text": "write hello.txt"}]
        assert messages[1]["parts"][-1] == {"type": "text", "text": "done"}

        assert h.client.diff(h.session) == [{"file": "hello.txt", "patch": "+Hello world"}]
        info = h.client.session_info(h.session)
        assert info["id"] == h.session.id
        assert info["cost"] == 0.021
        assert info["tokens"] == {"total": 4100, "input": 4000, "output": 100}
        assert info["agent"] is None

    assert h.fake.events_of("session.next.tool.called") == []
    assert h.fake.events_of("session.next.tool.completed") == []


def test_on_prompt_mutates_the_session_directory(tmp_path):
    """The turn hook sees the session directory and the prompt it was given."""
    seen = []

    def on_prompt(directory, text):
        seen.append((directory, text))
        Path(directory, "hello.txt").write_text("Hello world\n", encoding="utf-8")

    scenario = {"turns": [{"events": ["busy", "idle"], "on_prompt": on_prompt,
                           "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "create hello.txt")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
    assert res.status == "idle"
    assert seen == [(h.directory, "create hello.txt")]
    assert (tmp_path / "hello.txt").read_text() == "Hello world\n"


# ─────────────────────────────────────────────────────────────────────────────
# 5 — KiloServer: spawn, attach, close
# ─────────────────────────────────────────────────────────────────────────────

STUB_KILO = '''#!/usr/bin/env python3
import http.server
import sys

args = sys.argv[1:]
host = args[args.index("--hostname") + 1]
port = int(args[args.index("--port") + 1])
print("stub kilo serve on %s:%s" % (host, port), flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        # /global/health is what spawn polls; /session/... answers with a LIST,
        # not a dict, so the client's malformed-data fallback is exercised.
        if self.path.startswith("/global/health"):
            body, status = b"ok", 200
        elif self.path.startswith("/session/"):
            body, status = b"[]", 200
        else:
            body, status = b"nope", 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


http.server.HTTPServer((host, port), Handler).serve_forever()
'''


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_spawn_waits_for_health_and_close_kills_the_child(tmp_path):
    binary = _write_executable(tmp_path / "kilo", STUB_KILO)
    log = tmp_path / "serve.log"
    started = time.monotonic()
    # FL-1 (round 84): the gate is generous and the bound is in the middle —
    # spawn returning at all is the claim, and a health poll that needs 12 s
    # on a 32-worker box is still a pass, not a failure.
    server = KiloServer.spawn(str(binary), log_path=str(log), health_timeout=60.0)
    assert time.monotonic() - started < 60.0
    try:
        assert server.base_url.startswith("http://127.0.0.1:")
        assert server.pid is not None
        pid = server.pid
        # it really is serving on the port it picked, and malformed data from
        # it degrades to "no data" instead of raising
        client = KiloClient(server, str(tmp_path))
        ref = SessionRef(id="anything", provider_id="p", model_id="m",
                         directory=str(tmp_path))
        assert client.session_info(ref) == {}
        assert client.diff(ref) == []
        assert client.last_assistant_text(ref) == ""
    finally:
        server.close()
    with pytest.raises(OSError):
        os.kill(pid, 0)
    assert log.read_text().splitlines()[1].startswith("stub kilo serve on 127.0.0.1:")


def test_spawn_of_a_binary_that_exits_raises_with_the_log_tail(tmp_path):
    """The binary prints one line and exits 3: spawn must not wait out the
    whole timeout, and the tail of the log is what tells the operator why."""
    binary = _write_executable(tmp_path / "kilo", "#!/bin/sh\necho 'kilo: fatal: provider "
                                                   "config not found: kenary'\nexit 3\n")
    log = tmp_path / "serve.log"
    started = time.monotonic()
    with pytest.raises(KiloServerError) as exc:
        KiloServer.spawn(str(binary), log_path=str(log), health_timeout=60.0)
    # FL-1 (round 84): "must not wait out the whole timeout" is the claim, so
    # the timeout moves out to 60 s and the bound sits at 30 s — the child
    # exits at once and spawn notices at once, on any box.
    assert time.monotonic() - started < 30.0
    message = str(exc.value)
    assert "exited with code 3" in message
    assert "provider config not found: kenary" in exc.value.log_tail
    assert "provider config not found: kenary" in message
    assert "provider config not found: kenary" in log.read_text()


def test_spawn_of_a_missing_binary_is_a_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        KiloServer.spawn(str(tmp_path / "no-such-kilo"),
                         log_path=str(tmp_path / "serve.log"), health_timeout=1.0)


# KC-35: the stub records the environment it was started with, so a spawn
# without `env` and one with it can be compared.
STUB_KILO_ENV = '''#!/usr/bin/env python3
import http.server
import os
import sys

args = sys.argv[1:]
host = args[args.index("--hostname") + 1]
port = int(args[args.index("--port") + 1])

dump = os.environ.get("KC35_ENV_DUMP")
if dump:
    with open(dump, "w", encoding="utf-8") as handle:
        for key in sorted(os.environ):
            handle.write(key + "=" + os.environ[key] + "\\n")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"ok" if self.path.startswith("/global/health") else b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


http.server.HTTPServer((host, port), Handler).serve_forever()
'''


def test_spawn_env_is_added_to_the_inherited_environment(tmp_path, monkeypatch):
    """`env` is an overlay on the operator's environment, not a replacement —
    the child keeps its `PATH` and gains `KILO_CONFIG_CONTENT`."""
    binary = _write_executable(tmp_path / "kilo", STUB_KILO_ENV)
    dump = tmp_path / "child-env"
    overlay = {"KILO_CONFIG_CONTENT": '{"provider": {"kenary": {}}}',
               "KC35_ENV_DUMP": str(dump)}
    monkeypatch.setenv("KC35_ALREADY_HERE", "inherited")
    server = KiloServer.spawn(str(binary), log_path=str(tmp_path / "serve.log"),
                              health_timeout=60.0, env=overlay)
    try:
        pairs = dict(line.split("=", 1) for line in dump.read_text(encoding="utf-8").splitlines())
    finally:
        server.close()

    assert pairs["KILO_CONFIG_CONTENT"] == overlay["KILO_CONFIG_CONTENT"]
    assert pairs["KC35_ENV_DUMP"] == str(dump)
    # the inherited environment came along for the ride
    assert pairs["PATH"] == os.environ["PATH"]
    assert pairs["KC35_ALREADY_HERE"] == "inherited"


def test_spawn_without_env_passes_nothing_extra(tmp_path, monkeypatch):
    """No `env` → the child sees its parent's environment and nothing else."""
    binary = _write_executable(tmp_path / "kilo", STUB_KILO_ENV)
    dump = tmp_path / "child-env"
    monkeypatch.setenv("KC35_ENV_DUMP", str(dump))
    server = KiloServer.spawn(str(binary), log_path=str(tmp_path / "serve.log"),
                              health_timeout=60.0)
    try:
        pairs = dict(line.split("=", 1) for line in dump.read_text(encoding="utf-8").splitlines())
    finally:
        server.close()

    assert pairs["PATH"] == os.environ["PATH"]
    assert pairs["KC35_ENV_DUMP"] == str(dump)
    # the operator set no overlay: nothing was added on the way
    assert pairs.get("KILO_CONFIG_CONTENT") == os.environ.get("KILO_CONFIG_CONTENT")


def test_attach_checks_health_once_and_close_leaves_the_process_alone(tmp_path):
    """close() on a server we only attached to must not kill anything: the fake
    is still answering after the close."""
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        assert h.server.attached is True
        assert h.server.pid is None
        h.server.close()
        h.server.close()                     # idempotent
        assert h.client.session_info(h.session)["id"] == h.session.id

    with pytest.raises(KiloServerError, match="not healthy"):
        KiloServer.attach("http://127.0.0.1:1")


def test_client_raises_kilo_http_error_on_non_2xx(tmp_path):
    with _probe(tmp_path, {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]}) as h:
        with pytest.raises(KiloHttpError) as exc:
            h.client.messages(SessionRef(id="ses_not_there", provider_id="p",
                                         model_id="m", directory=h.directory))
        assert exc.value.status == 404
        assert exc.value.method == "GET"
        assert exc.value.path == "/session/ses_not_there/message"
        assert isinstance(exc.value.body, dict)


def test_prompt_sends_the_model_in_prompt_async_shape(tmp_path):
    """create_session sends {"providerID", "id"}; prompt_async sends
    {"providerID", "modelID"}. Both shapes are the live server's."""
    with _probe(tmp_path, {"turns": [{"events": ["busy", "idle"], "assistant": "done"},
                                     {"events": ["busy", "idle"], "assistant": "again"}]}) as h:
        h.client.prompt(h.session, "first")
        h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                           on_question=lambda event: None)
        h.client.prompt(h.session, "second")
        h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                           on_question=lambda event: None)
        h.client.abort(h.session)

    created = h.fake.calls(method="POST", path="/session")
    assert len(created) == 1
    assert created[0]["body"]["model"] == {"providerID": "kenary", "id": "hy3:free"}
    assert created[0]["body"]["title"] == "kilo-client-test"
    assert created[0]["body"]["permission"] == RULES
    assert "agent" not in created[0]["body"]
    assert created[0]["query"]["directory"] == h.directory

    prompts = h.fake.calls(method="POST", path="/prompt_async")
    assert len(prompts) == 2
    assert [p["body"]["parts"] for p in prompts] == [
        [{"type": "text", "text": "first"}], [{"type": "text", "text": "second"}],
    ]
    assert all(p["body"]["model"] == {"providerID": "kenary", "modelID": "hy3:free"}
               for p in prompts)


def test_create_session_with_agent_sends_the_agent(tmp_path):
    with _probe(tmp_path, {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]},
                agent="build") as h:
        assert h.session.agent == "build"
        created = h.fake.calls(method="POST", path="/session")
        assert created[0]["body"]["agent"] == "build"
        info = h.client.session_info(h.session)
        assert info["agent"] == "build"


# ─────────────────────────────────────────────────────────────────────────────
# 6 — the event tap on disk, in the probe's format
# ─────────────────────────────────────────────────────────────────────────────

def test_tap_writes_the_probe_event_log_format(tmp_path):
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]}
    log_path = tmp_path / "events.jsonl"
    fake = FakeKiloServer(scenario, directory=str(tmp_path)).start()
    try:
        tap = EventTap(fake.url, str(tmp_path), str(log_path)).start()
        deadline = time.monotonic() + 30   # see _probe: a tap only sees events
        while not fake.subscribers and time.monotonic() < deadline:
            time.sleep(0.02)               # after its /event connection is open
        assert fake.subscribers, "the tap never connected"
        client = KiloClient(KiloServer.attach(fake.url), str(tmp_path))
        session = client.create_session("kenary", "hy3:free", rules=RULES, title="log")
        client.prompt(session, "go")
        res = client.wait_idle(tap, session, 60.0, on_permission=_reject,
                               on_question=lambda event: None)
        assert res.status == "idle"
        tap.stop()
        # a hang guard on the reader unwinding, not a claim about how fast
        # it does (FL-1, round 84, round 7)
        assert tap.join(60) is True
    finally:
        fake.stop()

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert lines and all(set(entry) == {"t", "event"} for entry in lines)
    assert all(isinstance(entry["event"], dict) for entry in lines)
    assert [entry["event"]["type"] for entry in lines] == [
        e["type"] for e in tap.events
    ]
    assert any(entry["event"]["type"] == "session.created" for entry in lines)


def test_tap_keeps_every_event_for_its_whole_life(tmp_path):
    """`events` is not pruned as the cursor advances — KC-9 and KC-10 read it
    after the turn is over."""
    scenario = {"turns": [
        {"events": ["busy", "idle"], "assistant": "one"},
        {"events": ["busy", "idle"], "assistant": "two"},
    ]}
    with _probe(tmp_path, scenario) as h:
        for text in ("first", "second"):
            h.client.prompt(h.session, text)
            assert h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                      on_question=lambda event: None).status == "idle"
        assert len(h.tap.events) == len(h.fake.events)
        assert h.tap.cursor == len(h.fake.events)
        assert h.tap.events == h.fake.events


# ─────────────────────────────────────────────────────────────────────────────
# 7 — find_kilo_binary
# ─────────────────────────────────────────────────────────────────────────────

def _extension(home: Path, version: str) -> Path:
    folder = home / ".vscode" / "extensions" / f"kilocode.kilo-code-{version}-linux-x64"
    binary = folder / "bin" / "kilo"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f"kilo {version}\n", encoding="utf-8")
    return binary


@pytest.mark.parametrize("versions,expected", [
    (["7.6.2", "7.10.0"], "7.10.0"),       # "7.10.0" < "7.6.2" lexically
    (["7.10.0", "7.6.2", "7.2.1"], "7.10.0"),
    (["7.6.2", "7.6.10"], "7.6.10"),
    (["7.2.1", "7.10.0", "10.0.0"], "10.0.0"),
])
def test_find_kilo_binary_orders_by_version_not_lexically(tmp_path, monkeypatch,
                                                          versions, expected):
    monkeypatch.setenv("HOME", str(tmp_path))
    for version in versions:
        _extension(tmp_path, version)
    found = find_kilo_binary()
    assert found.endswith(os.path.join(f"kilocode.kilo-code-{expected}-linux-x64",
                                       "bin", "kilo"))
    assert found == str(_extension(tmp_path, expected))


def test_find_kilo_binary_explicit_wins_and_missing_explicit_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    newest = _extension(tmp_path, "7.10.0")
    _extension(tmp_path, "7.6.2")
    explicit = tmp_path / "elsewhere" / "kilo"
    explicit.parent.mkdir()
    explicit.write_text("explicit\n", encoding="utf-8")
    assert find_kilo_binary(str(explicit)) == str(explicit)
    # a typo does not get passed to Popen: it falls through to the extension
    assert find_kilo_binary(str(tmp_path / "elsewhere" / "nope")) == str(newest)


def test_find_kilo_binary_falls_back_to_path_then_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))      # no extensions here
    monkeypatch.setattr("tools.contest.kilo_client.shutil.which", lambda name: "/usr/local/bin/kilo")
    assert find_kilo_binary() == "/usr/local/bin/kilo"

    monkeypatch.setattr("tools.contest.kilo_client.shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError) as exc:
        find_kilo_binary(str(tmp_path / "nope"))
    message = str(exc.value)
    assert str(tmp_path / "nope") in message
    assert "kilocode.kilo-code-*" in message
    assert "which" in message


# ─────────────────────────────────────────────────────────────────────────────
# 8 — a scenario bug shows up, it does not hang the test
# ─────────────────────────────────────────────────────────────────────────────

def test_an_unscripted_prompt_goes_idle_and_leaves_a_trace(tmp_path):
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "one"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "one")
        assert h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                  on_question=lambda event: None).status == "idle"
        h.client.prompt(h.session, "two — nothing scripted for this one")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
    assert res.status == "idle"
    assert any("has no scripted turn" in note for note in h.fake.turn_errors)


def test_a_turn_hook_that_raises_becomes_a_recorded_error(tmp_path):
    def boom(directory, text):
        raise RuntimeError("the hook is broken")

    scenario = {"turns": [{"events": ["busy", "idle"], "on_prompt": boom,
                           "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res = h.client.wait_idle(h.tap, h.session, 60.0, on_permission=_reject,
                                 on_question=lambda event: None)
        assistant = h.client.last_assistant_text(h.session)
    assert res.status == "idle"
    assert h.fake.turn_errors == ["on_prompt: RuntimeError: the hook is broken"]
    assert assistant == "done"


# ─────────────────────────────────────────────────────────────────────────────
# 9 — GET /provider: the offer, as the server sees it
# ─────────────────────────────────────────────────────────────────────────────

def _offered(provider_id: str, name: str, model_ids) -> dict:
    """One entry of a `GET /provider` body, in the shape the live server sends."""
    return {
        "id": provider_id,
        "name": name,
        "source": "static",
        "models": {
            model_id: {"id": model_id, "providerID": provider_id, "name": model_id,
                       "status": "active",
                       "capabilities": {"reasoning": True, "toolcall": True},
                       "variants": {}}
            for model_id in model_ids
        },
    }


def _offer(providers, connected=None):
    """A whole `GET /provider` body around the providers named."""
    return {
        "all": list(providers),
        "default": {"kenary": "hy3:free"},
        "connected": list(connected if connected is not None else ["kenary"]),
        "failed": [],
    }


def test_providers_returns_the_offer_decoded_as_is(tmp_path):
    """`providers()` is the body `GET /provider` sent, reshaped by nothing: intake
    compares the roster's `provider/model` pairs against it, and a provider's
    `name` there is the display string, not the id `POST /session` wants."""
    offer = _offer((_offered("sensenova123", "sensenova",
                             ("sensenova-6.8-flash-lite", "hy3:free")),
                    _offered("kenary", "kenari", ("agnes-2-5-flash:free",))),
                   connected=["sensenova123", "kenary"])
    scenario = {"providers": offer,
                "turns": [{"events": ["busy", "idle"], "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        providers = h.client.providers()

    assert providers == offer
    (sensenova, kenary) = providers["all"]
    assert sensenova["id"] == "sensenova123" and sensenova["name"] == "sensenova"
    assert kenary["id"] == "kenary" and kenary["name"] == "kenari"
    assert set(providers) == {"all", "default", "connected", "failed"}
    request = h.fake.calls(method="GET", path="/provider")[0]
    assert request["query"]["directory"] == h.directory


def test_providers_defaults_to_the_fake_offer(tmp_path):
    """A scenario that names no `providers` gets the default offer, whose one
    provider is `kenary`, named `kenari`."""
    with _probe(tmp_path, {"turns": [{"events": ["busy", "idle"], "assistant": "done"}]}) as h:
        providers = h.client.providers()
        (provider,) = h.fake.offer["all"]
        assert provider["id"] != provider["name"]
    assert providers == h.fake.offer
    (provider,) = providers["all"]
    assert provider["id"] == "kenary" and provider["name"] == "kenari"
    assert providers["connected"] == ["kenary"]
    assert "hy3:free" in provider["models"]


def test_providers_of_a_500_is_a_kilo_http_error(tmp_path):
    with _probe(tmp_path, {"providers_status": 500,
                           "turns": [{"events": ["busy", "idle"], "assistant": "done"}]}) as h:
        with pytest.raises(KiloHttpError) as exc:
            h.client.providers()
    assert exc.value.status == 500
    assert exc.value.method == "GET"
    assert exc.value.path == "/provider"
    assert isinstance(exc.value.body, dict)


def test_providers_of_a_non_dict_body_is_a_value_error(tmp_path):
    """Like `create_session`'s, a body that is not a dict is a `ValueError`."""
    with _probe(tmp_path, {"providers": ["not", "a", "dict"],
                           "turns": [{"events": ["busy", "idle"], "assistant": "done"}]}) as h:
        with pytest.raises(ValueError, match="GET /provider"):
            h.client.providers()
