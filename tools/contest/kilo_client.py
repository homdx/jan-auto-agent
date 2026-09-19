"""tools/contest/kilo_client.py — KC-1: the probe's primitives as a module.

``scripts/kilo_hello.py`` (commit 67e834d) proved, live on Kilo 7.6.2, the
five primitives the contest needs: spawn ``kilo serve`` and wait for
``GET /global/health``; create a session with ``{"providerID", "id"}`` and a
permission rule list; send ``prompt_async`` with ``{"providerID", "modelID"}``;
tap the SSE stream; answer a permission. ``docs/kilo-contest/PROBE.md`` holds
the recorded payload shapes. Every call below is the probe's call — same
path, same body, same query — only the printing, the exiting and the
decisions are gone.

Two things the probe learned live are behaviour here, not comments:

  * every SSE event is consumed exactly once, from a persistent cursor
    (``EventTap.wait``). A wait that rescaned the list from the start
    mistook turn 1's ``session.idle`` for turn 2's — observed as "idle
    after 0.0 s" with an unchanged file (PROBE.md, §Facts 5).
  * the tool call and its result are read from the ``tool`` parts of
    ``GET /session/{id}/message`` (``KiloClient.tool_parts``), never from
    the event stream — ``session.next.tool.*`` never appeared on
    ``/event`` in this build (PROBE.md, §Facts 7).
  * ``wait_idle`` acts on ``_SESSION_EVENTS`` only; with a silence clock set
    (KC-12), every other event carrying this session's ``sessionID`` — a
    ``session.status busy``, a ``file.edited`` — resets that clock and
    nothing else.

Nothing here decides a permission: ``KiloClient.wait_idle`` hands the event
to a callback and sends back what the callback says. Reporting is by value
and by exception, never by printing — a non-2xx raises :class:`KiloHttpError`,
a server that never becomes healthy raises :class:`KiloServerError` carrying
the tail of its log, and a lost event stream surfaces as the synthetic
``tap.closed`` event so a caller waiting on it wakes up instead of running
its own timeout. Malformed server data degrades rather than raising:
``tool_parts`` skips a part that is not a dict, ``last_assistant_text``
returns ``""``, ``diff`` returns ``[]``, ``session_info`` returns ``{}``.

Standard library only.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Literal

__all__ = [
    "EventTap",
    "IdleResult",
    "KiloClient",
    "KiloHttpError",
    "KiloServerError",
    "KiloServer",
    "SessionRef",
    "find_kilo_binary",
]

_log = logging.getLogger(__name__)

#: The only replies this module will send. `always` is refused: under
#: `external_directory` it would whitelist the pattern for the rest of the
#: session (PROBE.md, §Facts 4).
_REPLIES = ("once", "reject")

#: Session events a wait has to look at, all of which carry a
#: `properties.sessionID`. `session.next.tool.*` is deliberately absent —
#: see the module docstring.
_SESSION_EVENTS = (
    "session.idle",
    "session.error",
    "permission.asked",
    "permission.v2.asked",
    "question.asked",
    "question.v2.asked",
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

#: How long one read of the event stream may block. The stream never ends on
#: its own, and a session can sit idle for minutes, so this is generous: the
#: stream is closed by `stop()` or by the server, not by a timeout.
_STREAM_READ_TIMEOUT = 3600.0


# ─────────────────────────────────────────────────────────────────────────────
# errors
# ─────────────────────────────────────────────────────────────────────────────

class KiloHttpError(Exception):
    """A Kilo HTTP call answered with a non-2xx status.

    ``body`` is the decoded JSON object when the server sent JSON, the raw
    text otherwise, ``None`` for an empty body. ``status`` is 0 when there
    was no answer at all — the connection failed, timed out or was refused —
    so a caller can tell "the server is gone" from "the server refused".
    Nothing here retries: the server is local, so a failure is reported, not
    retried.
    """

    def __init__(self, status: int, body, method: str, path: str) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> {status}: {body}")


class KiloServerError(Exception):
    """The ``kilo serve`` process could not be reached.

    Raised when a spawned server never answers 200 on ``GET /global/health``
    inside the timeout, or when ``attach`` finds an unhealthy server. The
    child is already killed when this is raised; ``log_tail`` carries the
    last lines of its log so the operator can see why.
    """

    def __init__(self, message: str, log_tail: str = "") -> None:
        self.log_tail = log_tail
        if log_tail:
            message = f"{message}\n--- log tail ---\n{log_tail.rstrip()}"
        super().__init__(message)


# ─────────────────────────────────────────────────────────────────────────────
# results
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionRef:
    """A session the client created, and where it lives.

    ``provider_id`` / ``model_id`` are the values passed to
    ``create_session`` rather than parsed out of the reply, so a server that
    echoes the model back in a different shape does not change what a later
    ``prompt`` sends.
    """

    id: str
    provider_id: str
    model_id: str
    directory: str
    agent: str | None = None


@dataclass(frozen=True)
class IdleResult:
    """The outcome of one ``KiloClient.wait_idle``.

    ``status`` is ``"idle"`` (the session finished its turn), ``"error"``
    (``session.error`` arrived, ``error`` holds its payload), ``"timeout"``
    (nothing arrived in time — ``abort`` was sent first) or ``"closed"``
    (the event stream ended; ``error`` holds the tap's reason).
    ``permissions`` and ``questions`` hold the full events that were
    answered, in order, so a caller can audit or persist them.

    A silence stall and the overall ``timeout`` share ``"timeout"`` —
    ``wait_idle`` does not invent a fourth status for it. A caller that needs
    to tell them apart reads ``elapsed``: a stall lands well under the
    overall deadline.
    """

    status: Literal["idle", "error", "timeout", "closed"]
    error: object = None
    permissions: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    elapsed: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# transport helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode(raw: bytes | None):
    """A response body as a JSON object when it is one, else raw text, else None."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _http_status(url: str, timeout: float = 5.0) -> int:
    """GET and return the status code. 0 when there is no answer at all.

    Used only for the health poll, where "not up yet" is the expected answer
    and must not raise.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def _log_tail(path: str, max_bytes: int = 65536, max_chars: int = 2000) -> str:
    """The end of a log file, or "" when there is nothing to read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")[-max_chars:]


def _terminate(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """terminate, wait ``grace`` seconds, then kill. Never raises."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except (OSError, ValueError):
        return
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            proc.kill()
        except (OSError, ValueError):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# binary lookup
# ─────────────────────────────────────────────────────────────────────────────

def _version_key(path: str) -> tuple:
    """Sort key for an extension install path: the version in its folder name.

    ``~/.vscode/extensions/kilocode.kilo-code-7.6.2-linux-x64/bin/kilo`` sorts
    below ``...-7.10.0-...``. A purely lexical sort gets this backwards:
    "7.10.0" < "7.6.2" because "1" < "6", which is the bug in the probe's
    own ``find_kilo``.
    """
    folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
    m = _VERSION_RE.search(folder)
    if not m:
        return (0, 0, 0, folder)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), folder)


def find_kilo_binary(explicit: str | None = None) -> str:
    """Return the path of the ``kilo`` binary to run.

    In order: ``explicit``, the newest VS Code extension copy, ``kilo`` on
    the PATH. ``explicit`` is trusted only if it names a file — a typo falls
    through to the next source instead of being passed to ``Popen``. Raises
    ``FileNotFoundError`` naming every place that was looked at.
    """
    looked: list[str] = []
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        looked.append(explicit)

    ext_glob = "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo"
    # newest first: the probe took the last of a lexical sort, which is how
    # "7.6.2" beats "7.10.0" in its hands
    for path in sorted(glob.glob(os.path.expanduser(ext_glob)),
                       key=_version_key, reverse=True):
        if os.path.isfile(path):
            return path
    looked.append(os.path.expanduser(ext_glob))

    on_path = shutil.which("kilo")
    if on_path:
        return on_path
    looked.append('shutil.which("kilo")')

    raise FileNotFoundError(
        "no kilo binary found; looked at: " + "; ".join(looked)
    )


# ─────────────────────────────────────────────────────────────────────────────
# the server
# ─────────────────────────────────────────────────────────────────────────────

class KiloServer:
    """A ``kilo serve`` process (spawned or attached to) plus its base URL.

    ``spawn`` owns the child: ``close`` terminates it (5 s of grace, then
    kills it) and closes its log. ``attach`` only records the URL —
    ``close`` on an attached server does nothing to a process it does not
    own.
    """

    def __init__(self, base_url: str, *, _process=None, _log_file=None,
                 _log_path: str | None = None, _attached: bool = False) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._process = _process
        self._log_file = _log_file
        self._log_path = _log_path
        self._attached = _attached

    # ── facts about this server ────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def pid(self) -> int | None:
        """The child's pid, or None for a server we only attached to."""
        return self._process.pid if self._process is not None else None

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def log_path(self) -> str | None:
        return self._log_path

    # ── lifecycle ──────────────────────────────────────────────────────────

    @classmethod
    def spawn(cls, binary: str, *, log_path: str, hostname: str = "127.0.0.1",
              health_timeout: float = 30.0) -> "KiloServer":
        """Start ``kilo serve`` on a free port and wait until it is healthy.

        Raises :class:`KiloServerError` — with the tail of ``log_path`` — if
        the child exits before the health check passes or the timeout runs
        out; the child is killed before the exception is raised. A binary
        that cannot be started at all raises ``FileNotFoundError`` from
        ``Popen``, which says more than a wrapped message would.
        """
        port = _free_port()
        url = f"http://{hostname}:{port}"
        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} kilo serve --port {port}\n")
        log_file.flush()
        try:
            proc = subprocess.Popen(
                [str(binary), "serve", "--port", str(port),
                 "--hostname", hostname, "--print-logs"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except (OSError, ValueError):
            log_file.close()
            raise

        deadline = time.monotonic() + float(health_timeout)
        while time.monotonic() < deadline:
            status = _http_status(url + "/global/health", timeout=3.0)
            if status == 200:
                return cls(url, _process=proc, _log_file=log_file,
                           _log_path=log_path)
            if proc.poll() is not None and status != 200:
                tail = _log_tail(log_path)
                log_file.close()
                _terminate(proc)
                raise KiloServerError(
                    f"kilo serve exited with code {proc.returncode} before "
                    f"/global/health answered 200 at {url}",
                    log_tail=tail,
                )
            time.sleep(0.2)

        tail = _log_tail(log_path)
        log_file.close()
        _terminate(proc)
        raise KiloServerError(
            f"kilo serve did not report healthy at {url} within "
            f"{health_timeout:.1f}s; log: {log_path}",
            log_tail=tail,
        )

    @classmethod
    def attach(cls, url: str) -> "KiloServer":
        """Wrap an already-running server. Its health is checked exactly once.

        Raises :class:`KiloServerError` when the check does not answer 200.
        """
        base = str(url).rstrip("/")
        status = _http_status(base + "/global/health", timeout=5.0)
        if status != 200:
            raise KiloServerError(
                f"kilo serve at {base} is not healthy (GET /global/health -> {status})"
            )
        return cls(base, _attached=True)

    def close(self) -> None:
        """Terminate the child we spawned and close its log. Idempotent.

        A no-op for a server that was only attached to: we do not kill a
        process we do not own.
        """
        proc, self._process = self._process, None
        if proc is not None:
            _terminate(proc)
        log, self._log_file = self._log_file, None
        if log is not None:
            try:
                log.flush()
                log.close()
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "KiloServer":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# the event tap
# ─────────────────────────────────────────────────────────────────────────────

def _response_socket(resp):
    """The socket under an SSE response, if it can be reached.

    ``http.client`` does not expose it, so this walks the response's file
    object. If that shape ever changes, ``stop()`` falls back to setting the
    flag only, and the reader notices on the next event.
    """
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    return sock if hasattr(sock, "shutdown") else None


class EventTap:
    """The probe's SSE reader: a daemon thread over ``GET /event?directory=``.

    Every ``data:`` line is parsed, appended to ``log_path`` as
    ``{"t": <time.time()>, "event": {...}}`` (the probe's on-disk format) and
    kept in memory in :attr:`events`, in arrival order.

    :meth:`wait` consumes from a persistent cursor, so each event is
    examined exactly once across calls. That is load-bearing: a wait that
    rescanned from the start of the list mistook turn 1's ``session.idle``
    for turn 2's (PROBE.md, §Facts 5).

    When the stream ends or is stopped, one synthetic event
    ``{"type": "tap.closed", "properties": {"error": "..."}}`` is appended —
    the same record shape as everything else, in memory and on disk — so a
    caller waiting on it wakes up instead of running its own timeout.
    """

    def __init__(self, base_url: str, directory: str, log_path: str) -> None:
        self._base_url = str(base_url).rstrip("/")
        self.directory = str(directory)
        self.log_path = str(log_path)
        self.url = self._base_url + "/event?directory=" + urllib.parse.quote(
            self.directory, safe="")
        self.events: list[dict] = []
        self.cursor: int = 0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._log = None

    def start(self) -> "EventTap":
        """Start the reader thread (already running: no change)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        parent = os.path.dirname(os.path.abspath(self.log_path))
        os.makedirs(parent, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8")
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="kilo-event-tap", daemon=True)
        self._thread = thread
        thread.start()
        return self

    def _run(self) -> None:
        req = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=_STREAM_READ_TIMEOUT) as resp:
                # this thread owns the connection, so it is the only one that
                # may close it; stop() wakes the recv below by another route
                self._socket = _response_socket(resp)
                buffer = ""
                while True:
                    if self._stop.is_set():
                        self._close_with("stopped")
                        return
                    try:
                        chunk = resp.readline()
                    except Exception as e:
                        self._close_with(f"stream ended: {type(e).__name__}: {e}")
                        return
                    if not chunk:
                        # EOF: the server closed the stream, or stop() shut the
                        # read side down to wake us
                        self._close_with("stopped" if self._stop.is_set() else "stream ended")
                        return
                    # readline() is not one line here: http.client feeds it a
                    # chunk, so one call can hand back several `data:` lines.
                    # Splitting is what keeps an event from being dropped with
                    # its neighbour.
                    buffer += chunk.decode("utf-8", "replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        self._handle_line(line.rstrip("\r"))
        except Exception as e:
            self._close_with(f"{type(e).__name__}: {e}")
        finally:
            self._socket = None
            self._close_log()

    def _handle_line(self, line: str) -> None:
        """One physical line of the stream: keep the `data:` payloads."""
        line = line.strip()
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(event, dict):
            self._record(event)

    def _record(self, event: dict) -> None:
        """Keep the event in memory and append it to the log, as the probe did.

        A log that cannot be written (a full disk, a closed handle) does not
        stop the reader: the in-memory list is the copy a caller waits on.
        """
        with self.lock:
            self.events.append(event)
        try:
            self._write_log({"t": time.time(), "event": event})
        except (OSError, ValueError, TypeError):
            pass

    def _write_log(self, entry: dict) -> None:
        log = self._log
        if log is None:
            return
        log.write(json.dumps(entry) + "\n")
        log.flush()

    def _close_with(self, reason: str) -> None:
        """Append the synthetic ``tap.closed`` event that wakes waiters."""
        self._record({"type": "tap.closed", "properties": {"error": reason}})

    def _close_log(self) -> None:
        log, self._log = self._log, None
        if log is not None:
            try:
                log.flush()
                log.close()
            except (OSError, ValueError):
                pass

    def wait(self, pred: Callable[[dict], bool], timeout: float) -> dict | None:
        """First unconsumed event for which ``pred(event)`` is true, else None.

        ``pred`` receives the whole event (``type`` and ``properties``), so a
        caller can filter on either. The cursor advances past every event
        looked at, matching or not, and it persists across calls.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            while True:
                with self.lock:
                    if self.cursor >= len(self.events):
                        break
                    event = self.events[self.cursor]
                    self.cursor += 1
                if pred(event):
                    return event
            time.sleep(0.2)
        return None

    def stop(self) -> "EventTap":
        """Ask the reader to finish. Any number of times, even before start.

        The reader thread owns the connection, and closing a response from
        another thread while that thread is blocked in recv() on the same
        socket hangs the caller instead of ending the reader. So the flag is
        set and the *read side* of the reader's socket is shut down: recv()
        returns EOF, the reader records ``tap.closed`` and closes the socket
        itself. With the socket unreachable the flag alone suffices and the
        reader notices on the next event.
        """
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RD)
            except OSError:
                # already closed by the reader; the flag is enough
                pass
        return self

    def join(self, timeout: float = 5.0) -> bool:
        """Wait up to ``timeout`` seconds for the reader thread to end."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def __enter__(self) -> "EventTap":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        self.join()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# the client
# ─────────────────────────────────────────────────────────────────────────────

class KiloClient:
    """One session directory on one server.

    Every method appends ``?directory=<quoted>`` to its path, exactly as the
    probe built the query string, and raises :class:`KiloHttpError` on a
    non-2xx answer.
    """

    def __init__(self, server: KiloServer, directory: str) -> None:
        self.server = server
        self.directory = os.path.abspath(str(directory))
        self._query = "?directory=" + urllib.parse.quote(self.directory, safe="")

    def _url(self, path: str) -> str:
        return f"{self.server.base_url}{path}{self._query}"

    def _request(self, method: str, path: str, body=None, timeout: float = 30.0):
        """One round trip: ``(status, decoded body)``.

        Non-2xx answers come back as a status and body, not as an exception —
        :meth:`_check` is what turns them into :class:`KiloHttpError`. A
        connection-level failure is reported as status 0.
        """
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self._url(path), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _decode(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _decode(e.read())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise KiloHttpError(0, f"{type(e).__name__}: {e}", method, path) from e

    @staticmethod
    def _check(status: int, body, method: str, path: str) -> None:
        if not 200 <= status < 300:
            raise KiloHttpError(status, body, method, path)

    # ── the session ────────────────────────────────────────────────────────

    def create_session(self, provider_id: str, model_id: str, *, rules: list,
                       title: str, agent: str | None = None) -> SessionRef:
        """``POST /session`` — the probe's body, ``model`` as ``{"providerID", "id"}``."""
        body = {
            "title": title,
            "model": {"providerID": provider_id, "id": model_id},
            "permission": list(rules),
        }
        if agent:
            body["agent"] = agent
        status, resp = self._request("POST", "/session", body)
        self._check(status, resp, "POST", "/session")
        if not isinstance(resp, dict) or not isinstance(resp.get("id"), str):
            raise ValueError(f"POST /session returned no session id: {resp!r}")
        return SessionRef(id=resp["id"], provider_id=provider_id, model_id=model_id,
                          agent=agent, directory=self.directory)

    def prompt(self, session: SessionRef, text: str) -> None:
        """``prompt_async`` into an existing session; 200 and 204 both succeed.

        The model is ``{"providerID", "modelID"}`` here — a different shape
        than ``create_session``, which is how the live server wants it.
        """
        path = f"/session/{session.id}/prompt_async"
        body = {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": session.provider_id, "modelID": session.model_id},
        }
        status, resp = self._request("POST", path, body)
        self._check(status, resp, "POST", path)
        return None

    def abort(self, session: SessionRef) -> None:
        """``POST /session/{id}/abort`` — no body, as the probe sent it."""
        path = f"/session/{session.id}/abort"
        status, resp = self._request("POST", path)
        self._check(status, resp, "POST", path)
        return None

    def messages(self, session: SessionRef) -> list:
        """``GET /session/{id}/message`` — every message, in order."""
        path = f"/session/{session.id}/message"
        status, resp = self._request("GET", path)
        self._check(status, resp, "GET", path)
        if not isinstance(resp, list):
            raise ValueError(
                f"GET {path} returned {type(resp).__name__}, expected a list")
        return resp

    def tool_parts(self, session: SessionRef) -> list:
        """Every part with ``type == "tool"``, in message order.

        These parts are the source of truth for what the model did and what
        it got back: ``session.next.tool.*`` never appeared on the event
        stream in this build (PROBE.md, §Facts 7). Each part carries ``tool``
        and a ``state`` dict with ``status``, ``input`` and ``output`` or
        ``error``; a caller should use ``.get`` because a part that is still
        running has no output yet.
        """
        parts: list = []
        for message in self.messages(session):
            for part in (message.get("parts") or []):
                if isinstance(part, dict) and part.get("type") == "tool":
                    parts.append(part)
        return parts

    def last_assistant_text(self, session: SessionRef) -> str:
        """The text of the most recent assistant message, joined and stripped.

        ``""`` when the session has no assistant message yet, rather than a
        placeholder — a caller can tell "the model said nothing" from "it
        has not said anything yet" by looking at :meth:`messages`.
        """
        for message in reversed(self.messages(session)):
            if (message.get("info") or {}).get("role") != "assistant":
                continue
            texts = [part.get("text", "") for part in (message.get("parts") or [])
                     if isinstance(part, dict) and part.get("type") == "text"]
            return " ".join(texts).strip()
        return ""

    def diff(self, session: SessionRef) -> list:
        """``GET /session/{id}/diff`` — ``[]`` if the server sent no list."""
        path = f"/session/{session.id}/diff"
        status, resp = self._request("GET", path)
        self._check(status, resp, "GET", path)
        return resp if isinstance(resp, list) else []

    def session_info(self, session: SessionRef) -> dict:
        """``GET /session/{id}`` — ``cost``, ``tokens`` and the rest. ``{}`` on malformed data."""
        path = f"/session/{session.id}"
        status, resp = self._request("GET", path)
        self._check(status, resp, "GET", path)
        return resp if isinstance(resp, dict) else {}

    # ── permissions and questions ──────────────────────────────────────────

    def reply_permission(self, session: SessionRef, permission_id: str,
                         reply: Literal["once", "reject"], message: str) -> None:
        """Answer a permission. Never ``always`` — the enum is enforced here.

        ``message`` is what the model reads as a tool error, so it is the
        reason a policy gives, not a log line. On a 404 from
        ``/permission/{id}/reply`` the probe's fallback is tried:
        ``POST /session/{id}/permissions/{pid}`` with ``{"response": reply}``
        — which is why this takes the session, not just the permission id.
        """
        if reply not in _REPLIES:
            raise ValueError(
                f"reply must be one of {_REPLIES}, never 'always' — under "
                f"external_directory it would whitelist the pattern for the rest "
                f"of the session: got {reply!r}")
        if not isinstance(permission_id, str) or not permission_id:
            raise ValueError(f"permission_id must be a non-empty string: {permission_id!r}")

        path = f"/permission/{permission_id}/reply"
        status, resp = self._request("POST", path, {"reply": reply, "message": message})
        if status == 404:
            path = f"/session/{session.id}/permissions/{permission_id}"
            status, resp = self._request("POST", path, {"response": reply})
        self._check(status, resp, "POST", path)
        return None

    def reject_question(self, question_id: str) -> None:
        """``POST /question/{id}/reject`` — a question is never answered."""
        path = f"/question/{question_id}/reject"
        status, resp = self._request("POST", path)
        self._check(status, resp, "POST", path)
        return None

    # ── the wait ───────────────────────────────────────────────────────────

    def _abort_quietly(self, session: SessionRef) -> None:
        """Send the timeout's abort without letting it mask the timeout."""
        try:
            self.abort(session)
        except KiloHttpError as e:
            _log.warning("abort(%s) after a timeout failed: %s", session.id, e)

    def wait_idle(self, tap: EventTap, session: SessionRef, timeout: float, *,
                  idle_event_timeout: float | None = None,
                  on_permission: Callable[[dict], tuple],
                  on_question: Callable[[dict], None]) -> IdleResult:
        """Block until this session goes idle, answering on the way.

        The probe's ``wait_idle`` with the decisions delegated:
        ``on_permission(event) -> (reply, message)`` decides a
        ``permission.*.asked`` event (the reply and the reason the model will
        read) and ``on_question(event) -> None`` observes a
        ``question.*.asked`` event, which is rejected regardless. Only events
        whose ``properties.sessionID`` is this session's are examined, and
        each is examined exactly once — the cursor is the tap's.

        ``timeout`` bounds the whole wait; ``idle_event_timeout`` (seconds,
        ``None`` = off, which is exactly the behaviour without it) bounds the
        silence. Neither is a constant here — the caller decides both, so the
        same primitive can be timed by a round's config, a script, or a test.
        A session that stays silent for longer than ``idle_event_timeout`` is
        aborted and returned as ``status="timeout"``, the same status the
        overall ``timeout`` produces: the clock is ``time.monotonic()`` from
        the session's *last* event of any type, and it races ``timeout``
        independently, so a session idle within the overall deadline that goes
        quiet partway through is still caught. The silence counts only this
        session's events — the tap reads every session of the directory, and
        a neighbour's traffic on the same stream does not keep this turn
        alive.

        Events that a permission or a question was *answered* for are
        collected in ``permissions`` / ``questions``, so a caller can audit
        or persist the turn. A timeout sends ``abort`` first; a failure of
        that abort (the session is already gone) is logged, not raised. A
        failure of the reply itself is logged and skipped — the session then
        reaches the timeout like any other, which is the only honest outcome.
        """
        started = time.monotonic()
        deadline = started + float(timeout)
        session_id = session.id
        # KC-12: the silence clock. None (or a non-positive number) is off,
        # and then the loop below is KC-1's, event for event.
        silence = float(idle_event_timeout) if idle_event_timeout is not None else None
        if silence is not None and silence <= 0:
            silence = None
        last_seen = started
        permissions: list = []
        questions: list = []

        def wanted(event: dict) -> bool:
            etype = event.get("type")
            if etype == "tap.closed":
                # a tap-level event: it has no sessionID, and it applies to
                # whatever stream this tap is reading
                return True
            if (event.get("properties") or {}).get("sessionID") != session_id:
                return False
            # with the silence clock on, every event of this session wakes the
            # wait: the ones acted on below are handled, the rest only reset
            # the clock — a `session.status busy` or a `file.edited` is the
            # session working, not stalled
            return silence is not None or etype in _SESSION_EVENTS

        while True:
            now = time.monotonic()
            left = deadline - now
            if silence is not None:
                # the two bounds race: whichever runs out first ends the wait
                left = min(left, silence - (now - last_seen))
            if left <= 0:
                self._abort_quietly(session)
                return IdleResult(status="timeout", elapsed=time.monotonic() - started,
                                  permissions=permissions, questions=questions)
            event = tap.wait(wanted, left)
            if event is None:
                continue
            last_seen = time.monotonic()

            etype = event.get("type")
            props = event.get("properties") or {}

            if etype in ("permission.asked", "permission.v2.asked"):
                try:
                    reply, message = on_permission(event)
                    self.reply_permission(session, props.get("id"), reply, message)
                except (KiloHttpError, ValueError, TypeError) as e:
                    _log.warning("permission %s not answered: %s", props.get("id"), e)
                permissions.append(event)
                continue

            if etype in ("question.asked", "question.v2.asked"):
                questions.append(event)
                try:
                    on_question(event)
                    question_id = props.get("id")
                    if question_id:
                        self.reject_question(question_id)
                except (KiloHttpError, ValueError) as e:
                    _log.warning("question %s not rejected: %s", props.get("id"), e)
                continue

            elapsed = time.monotonic() - started
            if etype == "session.error":
                return IdleResult(status="error", error=props.get("error", props),
                                  elapsed=elapsed, permissions=permissions,
                                  questions=questions)
            if etype == "tap.closed":
                return IdleResult(status="closed", error=props.get("error"),
                                  elapsed=elapsed, permissions=permissions,
                                  questions=questions)
            if etype != "session.idle":
                # only with the silence clock on: an event of this session
                # that reset it and asks for nothing
                continue
            return IdleResult(status="idle", elapsed=elapsed,
                              permissions=permissions, questions=questions)
