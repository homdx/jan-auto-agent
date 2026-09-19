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
"""

from __future__ import annotations

import json
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

from _kilo_fake import FakeKiloServer  # noqa: E402

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
def _probe(tmp_path, scenario, *, reply_timeout: float = 20.0, agent=None):
    """A fake server, an attached KiloServer, a client, a tap and a session."""
    directory = str(tmp_path)
    fake = FakeKiloServer(scenario, directory=directory,
                          reply_timeout=reply_timeout).start()
    server = KiloServer.attach(fake.url)
    client = KiloClient(server, directory)
    tap = EventTap(fake.url, directory, str(tmp_path / "events.jsonl")).start()
    # a tap only receives events after its /event connection is open, on the
    # fake as on a real server — wait for it so session.created is not lost
    for _ in range(100):
        if fake.subscribers:
            break
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
        res1 = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
                                  on_question=lambda event: None)
        assert res1.status == "idle"
        assert res1.permissions == []
        assert 0.0 <= res1.elapsed < 3.0

        # turn 1's idle is consumed, not just matched: a fresh wait with the
        # same predicate finds nothing, and turn 2 has not gone idle yet.
        idle_pred = lambda e: e.get("type") == "session.idle"  # noqa: E731
        assert h.tap.wait(idle_pred, 0.3) is None
        assert len(h.fake.events_of("session.idle")) == 1

        h.client.prompt(h.session, "append the model id as a second line")
        started = time.monotonic()
        res2 = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
                                  on_question=lambda event: None)
        assert res2.status == "idle"
        # turn 2's idle was held back by 0.6 s, so returning faster proves the
        # wait answered with a stale event.
        assert res2.elapsed >= 0.55
        assert len(h.fake.events_of("session.idle")) == 2

        assert h.client.last_assistant_text(h.session) == "done"
        assert len(h.client.messages(h.session)) == 4  # user + assistant, twice


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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=on_permission,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
    assert 0.0 <= res.elapsed < 3.0
    assert elapsed < 3.0

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
    ``session.status busy`` and then goes silent for 2 s without ever
    idling, so the wait must be bounded by the 0.5 s silence window — not by
    the 2 s pause it would have waited out, and not by the 10 s deadline."""
    scenario = {"turns": [{"pause_before_idle_sec": 2, "assistant": "still running"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "run the slow command")
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 10.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.error is None
    assert res.permissions == [] and res.questions == []
    assert res.elapsed < 1.5, res.elapsed      # the 0.5 s window, not the 2 s pause
    assert elapsed < 1.5
    # the shape's heartbeat went out, no idle ever did, and one abort was sent
    assert h.fake.events_of("session.status")
    assert h.fake.events_of("session.idle") == []
    assert h.fake.recorded_abort_for(h.session.id)


def test_events_of_the_session_keep_wait_idle_alive(tmp_path):
    """The silence clock counts every event of the session, not only the ones
    ``wait_idle`` acts on: a ``session.status busy`` every 0.15 s is eight
    0.3 s windows, and the turn still reaches idle — several windows past
    the first one. No abort, no early cut-off."""
    scenario = {"turns": [{"events": ["busy"], "delay": 1.0, "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        def heartbeat():
            for _ in range(8):
                time.sleep(0.15)
                h.fake._emit({"type": "session.status",
                              "properties": {"sessionID": h.session.id,
                                             "status": "busy"}})

        threading.Thread(target=heartbeat, daemon=True).start()
        started = time.monotonic()
        h.client.prompt(h.session, "slow turn")
        res = h.client.wait_idle(h.tap, h.session, 5.0, idle_event_timeout=0.3,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "idle"
    assert res.elapsed > 0.9, res.elapsed      # it really waited out the heartbeats
    assert elapsed > 0.9
    assert not h.fake.recorded_abort_for(h.session.id)
    # busy plus at least two beats: with idle_event_timeout=0.3 and no
    # heartbeat counted, this wait would have stalled out at ~0.3 s
    assert len(h.fake.events_of("session.status")) >= 3


def test_a_neighbours_events_do_not_keep_a_silent_session_alive(tmp_path):
    """The clock is the session's own: the tap reads every session of the
    directory, so a chatty neighbour must not push a silent one past its
    silence window. The silent session is aborted at 0.5 s while the other
    keeps emitting."""
    scenario = {"turns": [{"pause_before_idle_sec": 2}]}
    with _probe(tmp_path, scenario) as h:
        neighbour = h.client.create_session("kenary", "hy3:free", rules=RULES,
                                            title="the chatty one")
        h.client.prompt(h.session, "go quiet")

        def chat():
            for _ in range(10):
                time.sleep(0.1)
                h.fake._emit({"type": "session.status",
                              "properties": {"sessionID": neighbour.id,
                                             "status": "busy"}})

        threading.Thread(target=chat, daemon=True).start()
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 10.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.elapsed < 1.5, res.elapsed
    assert elapsed < 1.5
    assert h.fake.recorded_abort_for(h.session.id)
    assert not h.fake.recorded_abort_for(neighbour.id)


def test_a_turn_with_no_event_at_all_is_cut_at_the_window(tmp_path):
    """The clock starts when the wait does, not at the first event: a second
    turn whose prompt is accepted and then answered with nothing — no busy,
    no idle — is a stall at 0.5 s, not a turn that waits out the 10 s
    deadline. (A first turn always has ``session.created`` on the tap ahead
    of it, which is why this needs a second one.)"""
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "one"},
                          {"events": [], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "first")
        res = h.client.wait_idle(h.tap, h.session, 10.0, idle_event_timeout=1.0,
                                 on_permission=_reject, on_question=lambda event: None)
        assert res.status == "idle"
        h.client.prompt(h.session, "second")
        started = time.monotonic()
        res = h.client.wait_idle(h.tap, h.session, 10.0, idle_event_timeout=0.5,
                                 on_permission=_reject, on_question=lambda event: None)
        elapsed = time.monotonic() - started
    assert res.status == "timeout"
    assert res.elapsed < 1.5, res.elapsed
    assert elapsed < 1.5
    assert h.fake.recorded_abort_for(h.session.id)


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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
    server = KiloServer.spawn(str(binary), log_path=str(log), health_timeout=10.0)
    assert time.monotonic() - started < 10.0
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
        KiloServer.spawn(str(binary), log_path=str(log), health_timeout=5.0)
    assert time.monotonic() - started < 5.0
    message = str(exc.value)
    assert "exited with code 3" in message
    assert "provider config not found: kenary" in exc.value.log_tail
    assert "provider config not found: kenary" in message
    assert "provider config not found: kenary" in log.read_text()


def test_spawn_of_a_missing_binary_is_a_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        KiloServer.spawn(str(tmp_path / "no-such-kilo"),
                         log_path=str(tmp_path / "serve.log"), health_timeout=1.0)


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
        h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
                           on_question=lambda event: None)
        h.client.prompt(h.session, "second")
        h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        for _ in range(100):          # see _probe: a tap only sees events after
            if fake.subscribers:      # its /event connection is open
                break
            time.sleep(0.02)
        client = KiloClient(KiloServer.attach(fake.url), str(tmp_path))
        session = client.create_session("kenary", "hy3:free", rules=RULES, title="log")
        client.prompt(session, "go")
        res = client.wait_idle(tap, session, 5.0, on_permission=_reject,
                               on_question=lambda event: None)
        assert res.status == "idle"
        tap.stop()
        assert tap.join(5) is True
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
            assert h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        assert h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
                                  on_question=lambda event: None).status == "idle"
        h.client.prompt(h.session, "two — nothing scripted for this one")
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
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
        res = h.client.wait_idle(h.tap, h.session, 5.0, on_permission=_reject,
                                 on_question=lambda event: None)
        assistant = h.client.last_assistant_text(h.session)
    assert res.status == "idle"
    assert h.fake.turn_errors == ["on_prompt: RuntimeError: the hook is broken"]
    assert assistant == "done"
