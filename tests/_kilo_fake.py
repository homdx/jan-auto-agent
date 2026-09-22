"""tests/_kilo_fake.py — a scripted stand-in for ``kilo serve``, for KC-1+.

``tools/contest/kilo_client.py`` was written against the live calls recorded in
``docs/kilo-contest/PROBE.md`` by ``scripts/kilo_hello.py`` (commit 67e834d).
This module is the test double for it: an ``http.server`` on an ephemeral
127.0.0.1 port that replays those same calls and those same payload shapes, so
a caller of the client can be tested without a ``kilo`` binary and without a
live provider. The real server is never started and no provider is called.

Everything here is scripted by one scenario dict, per session:

    {
        "session": {...},            # merged into GET /session/{id} (cost, tokens)
        "diff": [...],               # default GET /session/{id}/diff
        "providers": {...},          # the GET /provider body; DEFAULT_OFFER when absent
        "providers_status": 500,     # GET /provider answers this instead of 200
        "turns": [                   # one entry per prompt the client sends
            {
                "events": ["busy", "file.edited", "idle"],
                "permission": {"permission": "external_directory",
                               "patterns": ["/tmp/*"],
                               "metadata": {"command": "rm -v /tmp/testfile",
                                            "directories": ["/tmp"]}},
                "question": {"question": "which file?"},
                "on_prompt": callable(directory, text) -> None,
                "tool_parts": [{"tool": "bash", "status": "error",
                                "input": {...}, "output": "..."}],
                "assistant": "done",
                "diff": [...],       # replaces the session diff after this turn
                "info": {...},       # merged into GET /session/{id} after this turn
                "delay": 2.5,        # seconds to hold before going idle
                "pause_before_idle_sec": 2,  # session.status busy, then that many
                                     # seconds of silence: no idle, no error,
                                     # nothing — a stalled turn (KC-12)
                "idle": false,       # never go idle (a stalled turn)
                "error": {...},      # session.error instead of idle
            },
        ],
        "permission_endpoint_404": true,   # /permission/{id}/reply answers 404,
                                           # so the client's legacy fallback shows
    }

Emission order, per turn: ``session.created`` on create; the turn's events in
order, except ``session.idle`` and ``session.turn.close``, which are held back;
then ``permission.asked`` — and the turn *blocks* there until the reply
arrives, because that is how the live server behaves — then
``permission.replied``; then ``session.idle`` (or ``session.error``); then
``session.turn.close``. That ordering is what makes the client's cursor
observable: a wait that rescaned from the start of the stream would see turn 1
idle while turn 2 was still asking a permission.

The payload keys are the ones recorded in PROBE.md: ``permission.asked``
carries ``id``, ``sessionID``, ``permission``, ``patterns``, ``metadata``,
``always``, ``tool{messageID, callID}``; ``permission.replied`` carries
``sessionID``, ``requestID``, ``reply``; ``permission.replied``/
``session.idle``/``session.status``/``session.created``/``session.turn.close``
all sit under a ``properties`` dict next to a ``type``. ``session.created`` and
``session.error`` payloads were never printed by the probe, so they use the
envelope every other recorded event uses and nothing more.

The two offer keys are per round, not per session: ``providers`` replaces
the whole ``GET /provider`` body (the ticket's ``{"all", "default",
"connected", "failed"}`` shape, each provider carrying its ``models`` map), and
``providers_status`` answers it with something other than 200, so the client's
``KiloHttpError`` path is reachable. Without either, the default body is one
provider ``kenary`` named ``kenari`` offering the model ids the contest's own
test rosters use, so a roster built on them is on offer with no override.

Every request is recorded as ``{method, path, query, body}`` in ``requests``
(``path`` without its query string) and every emitted event in ``events``, so
an assertion can read what the client actually sent and what the server
actually said. Unknown routes answer 404, like the real server does.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__all__ = ["FakeKiloServer"]


# ── routes, in the order the probe hit them ──────────────────────────────────

_RE_HEALTH = re.compile(r"^/global/health$")
_RE_EVENT = re.compile(r"^/event$")
_RE_PROVIDER = re.compile(r"^/provider$")
_RE_SESSION = re.compile(r"^/session$")
_RE_SESSION_GET = re.compile(r"^/session/([^/]+)/((?:message|diff)(?:/.*)?)$")
_RE_SESSION_BY_ID = re.compile(r"^/session/([^/]+)$")
_RE_PROMPT = re.compile(r"^/session/([^/]+)/prompt_async$")
_RE_ABORT = re.compile(r"^/session/([^/]+)/abort$")
_RE_PERMISSION_REPLY = re.compile(r"^/permission/([^/]+)/reply$")
_RE_PERMISSION_LEGACY = re.compile(r"^/session/([^/]+)/permissions/([^/]+)$")
_RE_QUESTION_REJECT = re.compile(r"^/question/([^/]+)/reject$")

_REPLIES = ("once", "reject")

# turn event names -> (event type, extra properties)
_EVENTS = {
    "busy": ("session.status", {"status": "busy"}),
    "idle_status": ("session.status", {"status": "idle"}),
    "file.edited": ("file.edited", {}),
    "session.diff": ("session.diff", {}),
    "idle": ("session.idle", {}),
    "session.turn.close": ("session.turn.close", {}),
}
#: emitted only after session.idle — PROBE.md's sequence ends with it
_POST_IDLE = "session.turn.close"


def _offered_model(model_id: str, provider_id: str = "kenary") -> dict:
    """One entry of a provider's ``models`` dict, as ``GET /provider`` sends it.

    ``status`` is ``active`` and the capabilities are the ones the live server
    sent in 7.6.2; nothing here reads them, but a scenario that copies the
    shape does.
    """
    return {
        "id": model_id,
        "providerID": provider_id,
        "name": model_id,
        "status": "active",
        "capabilities": {"reasoning": False, "toolcall": True},
        "variants": {},
    }


def _offered(provider_id: str, name: str, model_ids) -> dict:
    """One entry of ``GET /provider``'s ``all`` list, keyed by model id."""
    return {
        "id": provider_id,
        "name": name,
        "source": "static",
        "models": {model_id: _offered_model(model_id, provider_id) for model_id in model_ids},
    }


#: The offer the fake answers ``GET /provider`` with when a scenario names
#: none: one provider whose ``id`` is ``kenary`` and whose ``name`` is the
#: display string ``kenari`` — so a roster that spells the display name is
#: refused the way the live server's would be — carrying the model ids the
#: contest suites roster by default (`tests/test_contest_cli.py`'s
#: ``kenary/agent-a:free``, ``kenary/agent-b:free``, and ``--models
#: x:free,y:free``).
DEFAULT_OFFER = {
    "all": [_offered("kenary", "kenari", ("hy3:free", "agent-a:free", "agent-b:free",
                                          "x:free", "y:free"))],
    "default": {"kenary": "hy3:free"},
    "connected": ["kenary"],
    "failed": [],
}


def _tool_part(part: dict) -> dict:
    """Normalise a scripted tool part into the shape GET /message returns.

    Both forms are accepted: the full ``{"type": "tool", "tool": ..., "state":
    {...}}``, and the flat ``{"tool": ..., "status": ..., "input": ...,
    "output": ...}`` that is shorter to write in a scenario.
    """
    if not isinstance(part, dict):
        return part
    if isinstance(part.get("state"), dict):
        return {"type": "tool", **part}
    state = {k: part[k] for k in ("status", "input", "output", "error") if k in part}
    return {"type": "tool", "tool": part.get("tool"), "state": state}


class _Bus:
    """Fan-out to every open SSE connection, plus the recorded event log."""

    def __init__(self) -> None:
        self._subs: set = set()
        self._lock = threading.Lock()
        self.log: list = []

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            self.log.append(event)
            subs = list(self._subs)
        for q in subs:
            q.put(event)

    def close(self) -> None:
        with self._lock:
            subs, self._subs = list(self._subs), set()
        for q in subs:
            q.put(None)


class _Session:
    def __init__(self, sid: str, directory: str, model: dict, agent, rules: list,
                 info: dict, diff: list) -> None:
        self.id = sid
        self.directory = directory
        self.model = dict(model or {})
        self.agent = agent
        self.rules = list(rules or [])
        self.info = dict(info)
        self.diff = list(diff or [])
        self.messages: list = []
        self.turn_index = 0
        self.aborted = False


class FakeKiloServer:
    """A scripted ``kilo serve`` on an ephemeral port of 127.0.0.1.

    ``start`` binds and serves; ``stop`` wakes every open stream and shuts the
    listener down. Usable as a context manager. ``url`` is the base URL to
    hand to ``KiloServer.attach`` or ``EventTap``.
    """

    def __init__(self, scenario: dict | None = None, directory: str | None = None,
                 *, host: str = "127.0.0.1", port: int = 0,
                 reply_timeout: float = 20.0) -> None:
        self.scenario = dict(scenario or {})
        self.directory = directory or os.getcwd()
        self.host = host
        self._port = port
        self.reply_timeout = float(reply_timeout)
        self._bus = _Bus()
        self.requests: list = []
        self._sessions: dict = {}
        self._pending: dict = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0
        self._httpd = None
        self._thread = None
        self._base = None
        #: permission ids that were asked but never answered (the reply timeout hit)
        self.unanswered: list = []
        #: turn hooks that raised, so a scenario bug is visible instead of silent
        self.turn_errors: list = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        """The base URL, e.g. ``http://127.0.0.1:53211``. ``None`` before start."""
        return self._base

    def start(self) -> "FakeKiloServer":
        if self._httpd is not None:
            return self
        httpd = ThreadingHTTPServer((self.host, self._port), _Handler)
        httpd._fake = self
        httpd.daemon_threads = True
        self._httpd = httpd
        self._base = f"http://{self.host}:{httpd.server_address[1]}"
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._bus.close()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(5)

    def __enter__(self) -> "FakeKiloServer":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

    # ── read-outs for assertions ───────────────────────────────────────────

    @property
    def events(self) -> list:
        """Every event ever emitted, in order. The single source for a sequence."""
        with self._bus._lock:
            return list(self._bus.log)

    def events_of(self, etype: str) -> list:
        return [e for e in self.events if e.get("type") == etype]

    def calls(self, method: str | None = None, path: str | None = None,
              prefix: str | None = None) -> list:
        """Recorded requests, optionally filtered.

        ``path`` matches exactly or as a suffix, so ``calls(path="/abort")``
        finds ``/session/ses_x/abort``. ``prefix`` matches the start, for
        routes whose middle is an id, so ``calls(prefix="/permission/")``
        finds ``/permission/per_x/reply``.
        """
        out = []
        for record in self.requests:
            if method is not None and record["method"] != method:
                continue
            if path is not None:
                if record["path"] != path and not record["path"].endswith(path):
                    continue
            if prefix is not None and not record["path"].startswith(prefix):
                continue
            out.append(record)
        return out

    def recorded_abort_for(self, session_id: str) -> bool:
        """True when ``POST /session/{session_id}/abort`` reached the fake.

        The stall and timeout paths of ``wait_idle`` both send exactly one
        abort, so an assertion reads it back instead of re-deriving it from
        the request list.
        """
        path = f"/session/{session_id}/abort"
        return any(r["path"] == path for r in self.requests)

    def sessions(self) -> list:
        return list(self._sessions.values())

    @property
    def offer(self) -> dict:
        """The ``GET /provider`` body this fake answers with — the scenario's
        ``providers`` when one is given, else :data:`DEFAULT_OFFER`."""
        return self.scenario.get("providers", DEFAULT_OFFER)

    @property
    def subscribers(self) -> int:
        """Open event streams. Zero means a tap has not connected yet, and an
        event emitted before it connects is lost — exactly as on a real server.
        """
        with self._bus._lock:
            return len(self._bus._subs)

    # ── internals the handler reaches in ───────────────────────────────────

    def _record_request(self, method: str, path: str, query: dict, body) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path,
                                  "query": dict(query or {}), "body": body})

    def _next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq += 1
            return f"{prefix}_fake{self._seq:06d}"

    def _emit(self, event: dict) -> None:
        self._bus.publish(event)

    def _sleep(self, seconds: float) -> bool:
        """Sleep, but wake at once when stop() is called. False if stopped."""
        deadline = time.monotonic() + float(seconds)
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return True

    def _create_session(self, body: dict, directory: str) -> dict:
        sid = self._next_id("ses")
        model = dict((body or {}).get("model") or {})
        agent = (body or {}).get("agent")
        rules = list((body or {}).get("permission") or [])
        info = {"id": sid, "title": (body or {}).get("title"),
                "agent": agent, "cost": 0.0, "tokens": {"total": 0, "input": 0, "output": 0}}
        info.update(self.scenario.get("session") or {})
        info["id"] = sid
        session = _Session(sid, directory or self.directory, model, agent, rules,
                           info, list(self.scenario.get("diff") or []))
        with self._lock:
            self._sessions[sid] = session
        self._emit({"type": "session.created",
                    "properties": {"sessionID": sid, "title": info.get("title"),
                                   "model": model, "agent": agent}})
        return {"id": sid, "title": info.get("title"), "model": model, "agent": agent}

    def _permission_event(self, session: _Session, spec: dict) -> tuple:
        pid = self._next_id("per")
        spec = dict(spec or {})
        always = spec.pop("always", None)
        properties = {
            "id": pid,
            "sessionID": session.id,
            "permission": spec.get("permission", "external_directory"),
            "patterns": list(spec.get("patterns") or []),
            "metadata": dict(spec.get("metadata") or {}),
            "always": list(always) if always is not None else list(spec.get("patterns") or []),
            "tool": {"messageID": self._next_id("msg"), "callID": self._next_id("call")},
        }
        box = {"event": threading.Event(), "reply": None, "id": pid, "session": session}
        with self._lock:
            self._pending[pid] = box
        return pid, {"type": "permission.asked", "properties": properties}

    def _question_event(self, session: _Session, spec: dict) -> tuple:
        qid = self._next_id("ques")
        spec = dict(spec or {})
        box = {"event": threading.Event(), "rejected": False, "id": qid, "session": session}
        with self._lock:
            self._pending[qid] = box
        properties = {"id": qid, "sessionID": session.id}
        properties.update(spec)
        return qid, {"type": "question.asked", "properties": properties}

    def _answer_permission(self, pid: str, reply: str) -> bool:
        with self._lock:
            box = self._pending.get(pid)
        if box is None:
            return False
        if reply not in _REPLIES:
            return False
        box["reply"] = reply
        box["event"].set()
        return True

    def _reject_question(self, qid: str) -> bool:
        with self._lock:
            box = self._pending.get(qid)
        if box is None:
            return False
        box["rejected"] = True
        box["event"].set()
        return True

    def _session(self, sid: str) -> _Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def _run_turn(self, session: _Session, turn: dict, text: str) -> None:
        """Replay one prompt. Runs on its own thread: idle must wait for the
        permission reply, and the reply arrives on a different connection."""
        if not isinstance(turn, dict):
            self.turn_errors.append(f"turn is {type(turn).__name__}, not a dict")
            turn = {}

        on_prompt = turn.get("on_prompt")
        if callable(on_prompt):
            try:
                on_prompt(session.directory, text)
            except Exception as e:
                self.turn_errors.append(f"on_prompt: {type(e).__name__}: {e}")

        session.messages.append({
            "info": {"role": "user", "sessionID": session.id, "time": time.time()},
            "parts": [{"type": "text", "text": text}],
        })

        pause = turn.get("pause_before_idle_sec")
        if pause is not None:
            # KC-12's stall shape: one heartbeat, then that many seconds of
            # nothing — no idle, no error, no turn.close. The wait is what has
            # to notice; the sleep only keeps the turn "running" that long.
            self._emit(self._event_for(session, "busy"))
            self._sleep(float(pause))
            return

        names = list(turn.get("events") or [])
        for name in names:
            if name in (_POST_IDLE, "idle"):
                continue
            event = self._event_for(session, name)
            if event is not None:
                self._emit(event)

        permission = turn.get("permission")
        if permission is not None:
            pid, event = self._permission_event(session, permission)
            self._emit(event)
            box = self._pending.get(pid)
            if box is None or not box["event"].wait(self.reply_timeout):
                self.unanswered.append(pid)
            else:
                reply = box["reply"]
                if reply is not None:
                    self._emit({"type": "permission.replied",
                                "properties": {"sessionID": session.id,
                                               "requestID": pid, "reply": reply}})

        question = turn.get("question")
        if question is not None:
            qid, event = self._question_event(session, question)
            self._emit(event)
            box = self._pending.get(qid)
            if box is None or not box["event"].wait(self.reply_timeout):
                self.unanswered.append(qid)
            else:
                self._emit({"type": "question.replied",
                            "properties": {"sessionID": session.id, "requestID": qid,
                                           "reply": "reject"}})

        if turn.get("info") is not None:
            session.info.update(turn["info"])
        if turn.get("diff") is not None:
            session.diff = list(turn["diff"])

        error = turn.get("error")
        if error is not None:
            # session.error is *instead of* the assistant message and the idle:
            # the model never answered, so it has nothing to say.
            if isinstance(error, str):
                error = {"name": error, "message": error}
            session.info["error"] = error
            self._emit({"type": "session.error",
                        "properties": {"sessionID": session.id, "error": error}})
            return

        parts = [_tool_part(p) for p in (turn.get("tool_parts") or [])]
        if turn.get("assistant"):
            parts.append({"type": "text", "text": turn["assistant"]})
        session.messages.append({
            "info": {"role": "assistant", "sessionID": session.id, "time": time.time()},
            "parts": parts,
        })

        if turn.get("idle", True):
            delay = turn.get("delay")
            if delay and not self._sleep(delay):
                return
            self._emit({"type": "session.idle",
                        "properties": {"sessionID": session.id}})
        if _POST_IDLE in names:
            self._emit({"type": _EVENTS[_POST_IDLE][0],
                        "properties": {"sessionID": session.id}})

    @staticmethod
    def _event_for(session: _Session, name: str) -> dict | None:
        if name not in _EVENTS:
            return None
        etype, extra = _EVENTS[name]
        properties = {"sessionID": session.id}
        properties.update(extra)
        return {"type": etype, "properties": properties}


class _Handler(BaseHTTPRequestHandler):
    """One request at a time per connection, threaded across connections."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence; requests are recorded instead
        pass

    @property
    def fake(self) -> FakeKiloServer:
        return self.server._fake  # type: ignore[attr-defined]

    # ── plumbing ───────────────────────────────────────────────────────────

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _record(self, body) -> tuple:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.fake._record_request(self.command, parsed.path, query, body)
        return parsed.path, query

    def _json(self, status: int, payload=None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _not_found(self, path: str) -> None:
        self._json(404, {"error": f"no such route: {path}"})

    # ── GET ────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path, query = self._record(None)
        directory = query.get("directory") or self.fake.directory

        if _RE_HEALTH.fullmatch(path):
            return self._json(200, {"status": "ok"})

        if _RE_EVENT.fullmatch(path):
            return self._stream_events()

        if _RE_PROVIDER.fullmatch(path):
            # the offer: `providers` replaces it wholesale, `providers_status`
            # makes the route answer an error instead (KC-25's intake line)
            return self._json(int(self.fake.scenario.get("providers_status", 200)),
                              self.fake.scenario.get("providers", DEFAULT_OFFER))

        if _RE_SESSION_BY_ID.fullmatch(path):
            m = _RE_SESSION_BY_ID.fullmatch(path)
            session = self.fake._session(m.group(1))
            if session is None:
                return self._not_found(path)
            return self._json(200, session.info)

        m = _RE_SESSION_GET.fullmatch(path)
        if m:
            session = self.fake._session(m.group(1))
            if session is None:
                return self._not_found(path)
            if m.group(2).split("/", 1)[0] == "message":
                return self._json(200, list(session.messages))
            return self._json(200, list(session.diff))

        return self._not_found(path)

    # ── POST ───────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        body = self._body()
        path, query = self._record(body)
        directory = query.get("directory") or self.fake.directory

        if _RE_SESSION.fullmatch(path):
            return self._json(200, self.fake._create_session(body, directory))

        m = _RE_PROMPT.fullmatch(path)
        if m:
            session = self.fake._session(m.group(1))
            if session is None:
                return self._not_found(path)
            turns = self.fake.scenario.get("turns") or []
            if session.turn_index >= len(turns):
                # no script for this prompt: go idle, and leave a trace
                self.fake.turn_errors.append(
                    f"{session.id}: prompt {session.turn_index} has no scripted turn")
                self.fake._emit({"type": "session.idle",
                                 "properties": {"sessionID": session.id}})
                session.turn_index += 1
                return self._json(204)
            turn = turns[session.turn_index]
            session.turn_index += 1
            text = ""
            if isinstance(body, dict):
                text = "".join(p.get("text", "") for p in (body.get("parts") or [])
                               if isinstance(p, dict) and p.get("type") == "text")
            threading.Thread(target=self.fake._run_turn, args=(session, turn, text),
                             daemon=True).start()
            return self._json(204)

        m = _RE_ABORT.fullmatch(path)
        if m:
            session = self.fake._session(m.group(1))
            if session is None:
                return self._not_found(path)
            session.aborted = True
            return self._json(204)

        m = _RE_PERMISSION_REPLY.fullmatch(path)
        if m:
            return self._permission_reply(m.group(1), body, legacy=False)

        m = _RE_PERMISSION_LEGACY.fullmatch(path)
        if m:
            sid, pid = m.group(1), m.group(2)
            if self.fake._session(sid) is None:
                return self._not_found(path)
            return self._permission_reply(pid, body, legacy=True)

        m = _RE_QUESTION_REJECT.fullmatch(path)
        if m:
            qid = m.group(1)
            if not self.fake._reject_question(qid):
                return self._json(404, {"error": f"no such question: {qid}"})
            return self._json(200, True)

        return self._not_found(path)

    def _permission_reply(self, pid: str, body, *, legacy: bool) -> None:
        """The live endpoint, and the probe's fallback for when it 404s."""
        if self.fake.scenario.get("permission_endpoint_404") and not legacy:
            return self._json(404, {"error": "not found"})
        reply = None
        if isinstance(body, dict):
            reply = body.get("reply") if not legacy else body.get("response")
        if reply is None or reply not in _REPLIES:
            return self._json(400, {"error": f"reply must be one of {_REPLIES}"})
        if not self.fake._answer_permission(pid, reply):
            return self._json(404, {"error": f"no such permission request: {pid}"})
        return self._json(200, True)

    # ── the event stream ───────────────────────────────────────────────────

    def _stream_events(self) -> None:
        q = self.fake._bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.flush()
            while not self.fake._stop.is_set():
                try:
                    event = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event is None:
                    break
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # the tap closed the stream; that is how a session ends
            pass
        finally:
            self.fake._bus.unsubscribe(q)
            self.close_connection = True


# ─────────────────────────────────────────────────────────────────────────────
# a minimal OpenAI-compatible endpoint, for KC-34's OpenRouterBackend
# ─────────────────────────────────────────────────────────────────────────────

def _openai_reply(content=None, calls=(), usage=(10, 20), *, role="assistant"):
    """One ``/chat/completions`` reply in the shape the endpoint returns.

    ``content`` is the assistant text (``None`` for a tool-only turn) and
    ``calls`` is a list of ``{"id", "function": {"name", "arguments"}}`` where
    ``arguments`` is the JSON *string* the endpoint sends.
    """
    message = {"role": role, "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return {"choices": [{"finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def _openai_tool(name, arguments, call_id="call_1"):
    """One tool call, with ``arguments`` as the JSON string the endpoint sends."""
    payload = json.dumps(arguments, ensure_ascii=False) if not isinstance(arguments, str) else arguments
    return {"id": call_id, "function": {"name": name, "arguments": payload}}


class _OpenRouterHandler(BaseHTTPRequestHandler):
    """One POST per reply, threaded across connections, like the Kilo fake."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence; requests are recorded instead
        pass

    @property
    def fake(self):
        return self.server._fake

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        with self.fake._lock:
            self.fake.requests.append({"path": path, "body": body,
                                       "authorization": self.headers.get("Authorization")})
        if not path.endswith("/chat/completions"):
            return self._json(404, {"error": f"no such route: {path}"})
        with self.fake._lock:
            index = self.fake._index
            self.fake._index += 1
            spec = self.fake.responses[index] if index < len(self.fake.responses) else {}
        if isinstance(spec, dict) and "status" in spec:
            status, payload = int(spec["status"]), spec.get("body")
        else:
            status, payload = 200, spec
        if status >= 400:
            # the client turns this into an error turn, not a crash
            return self._json(status, payload if isinstance(payload, dict) else {"error": str(payload)})
        return self._json(200, payload)


class FakeOpenRouter:
    """A scripted OpenAI-compatible ``/chat/completions`` on 127.0.0.1.

    The other half of KC-34's test double: ``OpenRouterBackend`` speaks this
    endpoint straight, with no ``kilo`` in the path, so the fake is one route
    and nothing else. Each entry of *responses* is one reply, in call order —
    a dict body returned as 200, or ``{"status": N, "body": …}`` for an error
    answer. A reply beyond the end answers an empty body, which the agent loop
    reads as ``error`` rather than a crash. ``requests`` records every call as
    ``{path, body, authorization}``, so an assertion can read what the backend
    actually sent. The real provider is never called.
    """

    def __init__(self, responses=None, *, host: str = "127.0.0.1", port: int = 0,
                 base_path: str = "/v1") -> None:
        self.responses = list(responses or [])
        self.requests: list = []
        self.host = host
        self._port = port
        self._base_path = base_path.rstrip("/")
        self._lock = threading.Lock()
        self._index = 0
        self._httpd = None
        self._thread = None
        self._base = None

    @property
    def url(self) -> str:
        """The base URL, e.g. ``http://127.0.0.1:53211/v1``. ``None`` before start."""
        return self._base

    def start(self) -> "FakeOpenRouter":
        if self._httpd is not None:
            return self
        httpd = ThreadingHTTPServer((self.host, self._port), _OpenRouterHandler)
        httpd._fake = self
        httpd.daemon_threads = True
        self._httpd = httpd
        self._base = f"http://{self.host}:{httpd.server_address[1]}" + self._base_path
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(5)

    def __enter__(self) -> "FakeOpenRouter":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False
