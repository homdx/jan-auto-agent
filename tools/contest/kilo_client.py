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
  * with a ``bash`` ``tool`` part of this session still in
    ``state.status`` `running` (KC-47), that silence bound is the call's own
    ``state.input.timeout`` plus ``idle_event_timeout`` of grace, measured
    from the part's ``running`` event — a full ``pytest`` run is not silence,
    and a stall that fires anyway names the call in ``IdleResult.open_tool``.
  * ``providers`` is ``GET /provider`` decoded as is (KC-25) — nothing is
    reshaped, because the caller compares the roster's ``provider/model``
    pairs against it, and a provider's ``name`` there is the display string
    from ``kilo.jsonc``, not the ``id`` ``POST /session`` wants.

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
from dataclasses import dataclass, field, replace
from typing import Callable, Literal

__all__ = [
    "AGENT_TEST_TIMEOUT_MS",
    "EventTap",
    "IdleResult",
    "KiloClient",
    "KiloHttpError",
    "KiloServerError",
    "KiloServer",
    "SessionRef",
    "find_kilo_binary",
    "kilo_neighbours",
]

_log = logging.getLogger(__name__)

#: Replies this module sends; "always" is refused, since it would whitelist the pattern (PROBE.md).
_REPLIES = ("once", "reject")

#: Session events a wait looks at; session.next.tool.* is left out on purpose.
_SESSION_EVENTS = (
    "session.idle",
    "session.error",
    "permission.asked",
    "permission.v2.asked",
    "question.asked",
    "question.v2.asked",
)


#: Asks from a subagent that only the runner can answer; its idle and error events are its own (KC-71).
_CHILD_ASKS = (
    "permission.asked",
    "permission.v2.asked",
    "question.asked",
    "question.v2.asked",
)


def _child_session(event: dict, parents) -> str | None:
    """KC-71: the id of a session *event* announces as a child of *parents*.

    Kilo's ``task`` tool runs a subagent in a session of its own, and
    ``session.created`` / ``session.updated`` carry it as ``info.id`` with
    ``info.parentID`` naming the session that started it. ``None`` for every
    other event, and for a session whose parent is not one of *parents*.
    """
    if event.get("type") not in ("session.created", "session.updated"):
        return None
    info = (event.get("properties") or {}).get("info")
    if not isinstance(info, dict):
        return None
    child, parent = info.get("id"), info.get("parentID")
    if isinstance(child, str) and child and parent in parents:
        return child
    return None


#: Multiple of max_retry_wait past which a retry delay is a time the provider stated (round 146).
_BACKOFF_CROSSING_FACTOR = 4.0


def _retry_past_the_bound(wait: float | None, attempt, named: bool,
                          bound: float) -> str | None:
    """Round 146: what a Kilo `retry` status is, against `max_retry_wait`.

    ``None`` — a blip, the wait goes on. ``"quota"`` — a reset the provider
    named: the *first* retry of a streak (Kilo resets ``attempt`` after every
    success, and a daily quota ends at ``attempt: 1``, KC-64), a retry whose text
    is a quota phrase, or a delay no doubling could have reached from the bound.
    ``"backoff"`` — the same error repeated until Kilo's own backoff grew past the
    bound: the provider has named nothing, so it is not a quota.

    *wait* is seconds to the retry, ``None`` when Kilo gave no (numeric) time —
    then only a quota phrase decides, as before. An ``attempt`` that is no
    number counts as the first retry: today's verdict, fail-open.

    Bug 210/49: a ``NaN`` *wait* is no time at all — ``nan <= bound`` is
    False, so it fell through and read as a long delay (``"quota"`` on a first
    retry, ``"backoff"`` after). It is treated as ``None``: "Kilo gave no
    numeric time". ``inf`` is a real, very long delay and stays one.
    """
    if wait is not None and wait != wait:      # NaN is the one value unequal to itself
        wait = None
    if wait is None:
        return "quota" if named else None
    if wait <= bound:
        return None
    first = (not isinstance(attempt, int) or isinstance(attempt, bool)
             or attempt <= 1)
    if named or first or wait > _BACKOFF_CROSSING_FACTOR * bound:
        return "quota"
    return "backoff"


def _is_busy(event: dict) -> bool:
    """KC-63: a sign the session started working on a prompt.

    ``session.status`` busy in either shape (the fake sends
    ``"busy"``, live Kilo ``{"type": "busy"}``) or a ``session.turn.open``.
    """
    etype = event.get("type")
    if etype == "session.turn.open":
        return True
    if etype != "session.status":
        return False
    status = (event.get("properties") or {}).get("status")
    if isinstance(status, dict):
        status = status.get("type")
    return status == "busy"


def _skip_stale(tap: "EventTap", session_id: str, since: int) -> int:
    """KC-63: move *tap* past *since*; how many skipped events could have
    ended or steered this session's wait.

    Only this session's :data:`_SESSION_EVENTS` count: the ``session.created``
    or a neighbour's traffic that sits before every first prompt is skipped
    too, but it is not an earlier turn's leftover. One wait reads a tap at a
    time, and ``events`` only grows, so the slice is the skipped events.
    """
    start = tap.cursor
    skipped = tap.skip_to(since)
    return sum(1 for event in tap.events[start:start + skipped]
               if event.get("type") in _SESSION_EVENTS
               and (event.get("properties") or {}).get("sessionID") == session_id)


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

#: Max time one read of the event stream may block; the stream closes only on stop or server end.
_STREAM_READ_TIMEOUT = 3600.0

# Log flushes happen at most every 0.5 s, not per event, to keep the reader thread fast (FL-1).
_LOG_FLUSH_INTERVAL_S = 0.5

#: Kilo bash default timeout, read off the tool schema (KC-47), so an omitted timeout is bounded.
_KILO_BASH_DEFAULT_TIMEOUT_MS = 120000

#: Timeout the round's prompt tells the agent to use for its tests; must stay below turn_timeout_sec (KC-47).
AGENT_TEST_TIMEOUT_MS = 1_200_000

# KC-47 §4: how much of a running command's text a silence stall reports.
_OPEN_TOOL_COMMAND_CHARS = 120


# Errors

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


# Results

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
    variant: str | None = None


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

    ``"quiet"`` (KC-73) comes only from a wait armed with ``quiet_after``:
    the session sent nothing at all in that window, so nothing is running in
    it — and nothing is aborted. ``compacted`` is whether a
    ``session.compacted`` of this session was read on the way: Kilo compacted
    it by itself.

    ``open_tool`` (KC-47) is set only when the silence bound fired while this
    session still had a ``bash`` call in flight:
    ``{"tool": "bash", "command": <120 chars>, "running_for": s}``, so a stall
    report says what the agent was doing instead of "no event for 300s".
    ``None`` for every other outcome, and for a stall with no ``bash`` part
    open — KC-12's silence, byte for byte.
    """

    status: Literal["idle", "error", "timeout", "closed", "quiet"]
    error: object = None
    permissions: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    elapsed: float = 0.0
    open_tool: dict | None = None
    #: Events this session's since mark skipped (0 without a mark) (KC-63).
    stale_skipped: int = 0
    #: KC-73: a ``session.compacted`` of this session was read in this wait
    compacted: bool = False
    #: Count of provider retry statuses (429, 502, 503) seen in this wait (KC-75).
    provider_errors: int = 0
    #: Seconds of a retry streak that the wait added to its own deadline (round 146).
    retry_waited: float = 0.0


# Transport helpers

def _decode(raw: bytes | None):
    """A response body as a JSON object when it is one, else raw text, else None."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


_ADDR_IN_USE_RE = re.compile(r"EADDRINUSE|address already in use|port \d+ is (?:already )?in use",
                             re.IGNORECASE)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


# Binary lookup

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
    # Newest first: a lexical sort would put 7.10.0 below 7.6.2.
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


# The server

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

    #: MIMO-1: how many fresh ports `spawn` tries when the child lost its port.
    PORT_ATTEMPTS = 3

    @classmethod
    def spawn(cls, binary: str, *, log_path: str, hostname: str = "127.0.0.1",
              health_timeout: float = 30.0, env: dict | None = None) -> "KiloServer":
        """Start ``kilo serve`` on a free port; see :meth:`_spawn_once`.

        MIMO-1: the free port is found by binding port 0 and closing the socket,
        and ``kilo serve`` binds it later — another process can take it in
        between. A child that exits early with an address-in-use error in its
        log is retried on a fresh port, up to :attr:`PORT_ATTEMPTS` times; any
        other failure is raised at once, exactly as before.
        """
        for attempt in range(cls.PORT_ATTEMPTS):
            try:
                return cls._spawn_once(binary, log_path=log_path, hostname=hostname,
                                       health_timeout=health_timeout, env=env)
            except KiloServerError as err:
                if (attempt + 1 >= cls.PORT_ATTEMPTS
                        or not _ADDR_IN_USE_RE.search(getattr(err, "log_tail", "") or "")):
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    @classmethod
    def _spawn_once(cls, binary: str, *, log_path: str, hostname: str = "127.0.0.1",
                    health_timeout: float = 30.0, env: dict | None = None) -> "KiloServer":
        """Start ``kilo serve`` on a free port and wait until it is healthy.

        ``env`` (KC-35) is added on top of this process's own environment for the
        child, so ``KILO_CONFIG_CONTENT`` reaches the server it is meant to change;
        ``None`` inherits the environment untouched, exactly as before the parameter.

        Raises :class:`KiloServerError` — with the tail of ``log_path`` — if
        the child exits before the health check passes or the timeout runs
        out; the child is killed before the exception is raised. A binary
        that cannot be started at all raises ``FileNotFoundError`` from
        ``Popen``, which says more than a wrapped message would.
        """
        port = _free_port()
        url = f"http://{hostname}:{port}"
        log_file = open(log_path, "a", encoding="utf-8")
        child_env = None
        if env:
            child_env = {**os.environ, **env}
        try:
            log_file.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} kilo serve --port {port}\n")
            log_file.flush()
            proc = subprocess.Popen(
                [str(binary), "serve", "--port", str(port),
                 "--hostname", hostname, "--print-logs"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=child_env,
            )
        except (OSError, ValueError):
            log_file.close()
            raise

        try:
            deadline = time.monotonic() + float(health_timeout)
            while time.monotonic() < deadline:
                status = _http_status(url + "/global/health", timeout=3.0)
                # a 200 from a server that is not ours (another one took the port) is no health of this child
                if status == 200 and proc.poll() is None:
                    return cls(url, _process=proc, _log_file=log_file,
                               _log_path=log_path)
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
        except BaseException:
            log_file.close()
            _terminate(proc)
            raise
        tail = _log_tail(log_path)
        log_file.close()
        exited = proc.poll() is not None
        _terminate(proc)
        if exited:
            raise KiloServerError(
                f"kilo serve exited with code {proc.returncode} before "
                f"/global/health answered 200 at {url}",
                log_tail=tail,
            )
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


def kilo_data_dir(environ: dict) -> str:
    """The Kilo store one environment writes to, as a path.

    ``$XDG_DATA_HOME/kilo`` when the var is set, else ``$HOME/.local/share/kilo``
    — Kilo's own default. ``""`` when neither can be read, which is the
    "no data dir" answer the callers compare against rather than a raise.
    """
    root = str(environ.get("XDG_DATA_HOME") or "").strip()
    if root:
        return root.rstrip("/") + "/kilo"
    home = str(environ.get("HOME") or "").strip()
    if not home:
        return ""
    return home.rstrip("/") + "/.local/share/kilo"


def _proc_env(root: str, pid: int) -> dict:
    """The `KEY=VALUE` pairs of `/proc/<pid>/environ`, `{}` when unreadable.

    Every read that cannot happen is a missing pair, never an exception: the
    process may have exited between the scan and the read.
    """
    try:
        with open(f"{root}/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    pairs: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, _sep, value = item.partition(b"=")
        pairs[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return pairs


def _proc_argv0(root: str, pid: int) -> str:
    """The command of `/proc/<pid>/cmdline`, `""` when it has none.

    A kernel thread has no cmdline at all, and so does a process that exited
    between the scan and the read — `""` is skipped by the caller, not raised.
    """
    try:
        with open(f"{root}/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _proc_ppid(root: str, pid: int):
    """The parent pid off `/proc/<pid>/stat`, `None` when it cannot be read.

    The comm (field two) may itself hold `)` and spaces, so the walk starts
    after the last `)`: the state is first, the ppid second.
    """
    try:
        with open(f"{root}/{pid}/stat", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def kilo_neighbours(server_pid: int, *, proc_root: str = "/proc") -> tuple[int, str]:
    """KC-62: ``(count, data_dir)`` of the other Kilo processes that write the
    same store as *server_pid*.

    ``data_dir`` comes from that server's own ``/proc/<pid>/environ`` —
    ``XDG_DATA_HOME``, else ``HOME + "/.local/share"``, plus ``/kilo`` — so the
    box decides where the store is and no path is hard-coded. A process counts
    when its ``argv[0]`` basename is ``kilo``, it is not *server_pid* nor part of
    its tree, and its own environ resolves to that same ``data_dir``. The tree
    matters because a ``kilo run`` check boots a server of its own, and this
    round's server and its children are this round, not a neighbour.

    Everything is read through ``/proc`` — no ``ps``, no hard-coded paths.
    Fail-open: ``(0, "")`` when the pid is not usable, the server's environ
    resolves to no store, or ``/proc`` is not readable at all (not Linux, no
    perms). A scan that sees a store but cannot count counts zero, which is the
    "no neighbours" line, never a raise into a round.
    """
    if not isinstance(server_pid, int) or isinstance(server_pid, bool) or server_pid <= 0:
        return 0, ""
    root = str(proc_root)
    data_dir = kilo_data_dir(_proc_env(root, server_pid))
    if not data_dir:
        return 0, ""
    try:
        entries = os.listdir(root)
    except OSError:
        return 0, ""
    pids = sorted(int(entry) for entry in entries if entry.isdigit())
    children: dict[int, list[int]] = {}
    for pid in pids:
        ppid = _proc_ppid(root, pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    own = {server_pid}
    queue = list(children.get(server_pid, []))
    while queue:
        child = queue.pop()
        if child in own:
            continue
        own.add(child)
        queue.extend(children.get(child, []))
    count = 0
    for pid in pids:
        if pid in own:
            continue
        if os.path.basename(_proc_argv0(root, pid)) != "kilo":
            continue
        if kilo_data_dir(_proc_env(root, pid)) != data_dir:
            continue
        count += 1
    return count, data_dir


# The event tap

def _response_socket(resp):
    """The socket under an SSE response, if it can be reached.

    ``http.client`` does not expose it, so this walks the response's file
    object. If that shape ever changes, ``stop()`` falls back to setting the
    flag only, and the reader notices on the next event.
    """
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    return sock if hasattr(sock, "shutdown") else None


#: Timeout for compact (POST summarize), which writes the whole summary before it answers (KC-69).
COMPACT_TIMEOUT_SEC = 900.0


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
        # Waiters sleep on a condition, not a poll, so an event reaches wait() as soon as it is recorded (FL-1).
        self._cond = threading.Condition(self.lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._log = None
        self._last_flush: float | None = None

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
        try:
            thread.start()
        except BaseException:
            self._log.close()
            self._log, self._thread = None, None
            raise
        return self

    def _run(self) -> None:
        req = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=_STREAM_READ_TIMEOUT) as resp:
                # This thread owns the connection, so only it closes it; stop() wakes the read another way.
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
                        # EOF: the server closed the stream, or stop() shut it to wake the reader.
                        self._close_with("stopped" if self._stop.is_set() else "stream ended")
                        return
                    # Split the chunk into lines, so no event is lost to its neighbour.
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

        The in-memory copy is published *first*, and waiters are woken before
        the log is touched at all (FL-1, round 84) — what a caller's silence
        clock measures must not include this tap's own bookkeeping.
        """
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()
        try:
            self._write_log({"t": time.time(), "event": event})
        except (OSError, ValueError, TypeError):
            pass

    def _write_log(self, entry: dict) -> None:
        """Append one entry, flushing on a bounded cadence rather than per event.

        FL-1 (round 84): this used to ``flush()`` on every single event, on
        the reader thread, so the next line of the stream was not read until
        that write returned. A ``write(2)`` is not free when the page cache
        is full — under dirty-page writeback throttling it blocks, and on a
        box running four ``pytest -n 8`` invocations it blocked for
        *seconds*. The caller's silence clock counts that as the session
        going quiet, so a turn that was emitting the whole time got declared
        stalled and aborted: the agent's own event log starved the agent.

        The same hazard family as the metrics lock spanning ``fsync`` — a
        blocking disk operation on a path that a timing guard depends on.
        Buffered writes still land in order and ``_close_log`` flushes, so
        the on-disk format and completeness at ``stop()`` are unchanged.
        """
        log = self._log
        if log is None:
            return
        log.write(json.dumps(entry) + "\n")
        now = time.monotonic()
        if self._last_flush is None or (now - self._last_flush) >= _LOG_FLUSH_INTERVAL_S:
            log.flush()
            self._last_flush = now

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
        while True:
            event = None
            with self._cond:
                if self.cursor < len(self.events):
                    event = self.events[self.cursor]
                    self.cursor += 1
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    # Wake on each recorded event, not on a poll, so the silence clock is not inflated (FL-1).
                    self._cond.wait(left)
                    continue
            # The predicate is the caller's code, so it runs outside the lock.
            if pred(event):
                return event

    def mark(self) -> int:
        """KC-63: how many events this tap holds right now.

        Taken right *before* a prompt is sent: every event at an index below
        it was recorded before the prompt existed, so it belongs to an
        earlier turn.
        """
        with self._cond:
            return len(self.events)

    def skip_to(self, mark: int) -> int:
        """KC-63: move the cursor forward to *mark*, never backward.

        Returns how many unread events were skipped, so the caller can
        record them. The skipped events stay in ``self.events`` and in
        ``events.jsonl``; they are only never handed to ``wait`` again.
        """
        with self._cond:
            target = max(self.cursor, min(int(mark), len(self.events)))
            skipped = target - self.cursor
            self.cursor = target
            return skipped

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


# KC-47: the open bash parts the silence clock waits for

def _number(value) -> float | None:
    """``value`` as a float when it is a number, else None. Never raises.

    Kilo sends ``timeout`` as a number, and a malformed payload must not turn
    a stall bookkeeping line into an exception into a round.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def _part_input(part: dict) -> dict:
    """The arguments a ``tool`` part was called with; ``{}`` when it has none.

    The live server nests them under ``state.input``; anything else is
    treated as "no arguments", which is what a stalled stall report says
    rather than guessing at a shape the build never sent.
    """
    state = part.get("state")
    if not isinstance(state, dict):
        return {}
    input_ = state.get("input")
    return input_ if isinstance(input_, dict) else {}


def _part_command(part: dict) -> str:
    """The command a ``tool`` part is running, trimmed and bounded."""
    command = _part_input(part).get("command")
    if not isinstance(command, str):
        return ""
    return command.strip()[:_OPEN_TOOL_COMMAND_CHARS]


def _part_timeout_ms(part: dict) -> float:
    """The milliseconds the part asked for, else Kilo's own `bash` default."""
    timeout = _number(_part_input(part).get("timeout"))
    if timeout is not None and timeout > 0:
        return timeout
    return float(_KILO_BASH_DEFAULT_TIMEOUT_MS)


def _track_open_part(open_parts: dict, part: dict | None, at: float) -> None:
    """One ``message.part.updated`` of this session, into the open-parts map.

    A ``tool`` part with ``tool == "bash"`` and a ``state.status`` of
    ``running`` or ``pending`` is *open*, keyed by its part id; that same id
    in any other status closes it. Nothing else is tracked: a ``task``, an
    ``edit``, a ``read`` keeps KC-12's clock exactly as today, and a part
    without an id is skipped outright — one that could never be closed again
    would hold the silence clock open for good.
    """
    if not isinstance(part, dict):
        return
    part_id = part.get("id")
    if not isinstance(part_id, str) or not part_id:
        return
    state = part.get("state")
    status = state.get("status") if isinstance(state, dict) else None
    if part.get("type") == "tool" and part.get("tool") == "bash" \
            and status in ("running", "pending"):
        known = open_parts.get(part_id)
        # The bound runs from the call's first running event; later updates do not restart it.
        if known is not None and known["status"] == "running":
            at = known["at"]
        open_parts[part_id] = {"tool": "bash", "command": _part_command(part),
                               "timeout_ms": _part_timeout_ms(part), "at": at,
                               "status": status}
    else:
        open_parts.pop(part_id, None)


def _silence_left(silence: float, last_seen: float, open_parts: dict,
                  now: float) -> float:
    """Seconds the silence clock has left: KC-12's, widened by an open `bash` call.

    With no part open this is KC-12's window from the last event, untouched.
    With one it is the latest of that and each call's own timeout plus the
    window as grace, measured from the call's ``running`` event: the call's
    kill comes first, and its ``completed`` event is what resets the clock.
    Never less than KC-12's, so opening a part can only ever give a turn more
    room, and the session's own chatter never shortens the call's bound.
    """
    left = silence - (now - last_seen)
    for info in open_parts.values():
        left = max(left, info["at"] + float(info["timeout_ms"]) / 1000.0 + silence - now)
    return left


def _open_tool_report(open_parts: dict, now: float) -> dict | None:
    """What was still running when the silence bound fired, or None.

    The call whose own bound was the widest is the one the turn was waiting
    on — the stall happened inside it.
    """
    if not open_parts:
        return None
    info = max(open_parts.values(),
               key=lambda p: p["at"] + float(p["timeout_ms"]) / 1000.0)
    return {"tool": info["tool"], "command": info["command"],
            "running_for": now - info["at"]}


# The client

def _deadline_grant(on_deadline: Callable[[float], float | None],
                    elapsed: float) -> float | None:
    """The seconds a caller grants at its own deadline, or ``None``.

    KC-36: ``wait_idle`` asks instead of killing, and the answer is whatever
    this returns. ``None``, ``0``, a negative number and anything that is not
    a number all mean "abort as today", and a callback that raises — a broken
    churn reader, a missing worktree — is logged here and never raised into a
    round. Only a positive number moves the deadline.
    """
    try:
        value = on_deadline(elapsed)
    except Exception as exc:  # noqa: BLE001 — a hook is a round, not the round
        _log.warning("turn deadline asked, %s: %s", type(exc).__name__, exc)
        return None
    if not isinstance(value, (int, float)):
        return None
    return float(value) if float(value) > 0 else None


def _gate_back_sec(value) -> float:
    """The seconds a handler grants the turn back, or ``0.0``.

    KC-66: a bool is not a number of seconds, a string is not either, and a
    negative number is not a grant. Anything that is not a positive int or
    float is ``0.0`` — never an exception out of a permission reply.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if float(value) > 0 else 0.0


def _permission_answer(answer) -> "tuple[str, str, float]":
    """KC-66: ``(reply, message, gate_sec)`` out of an ``on_permission`` answer.

    Two elements is today's contract and it is unchanged: a handler that spent
    no gate time answers ``("once", "")`` exactly as before and ``gate_sec`` is
    ``0.0``. A third element is the seconds the handler asks back for the gate's
    own waits — the gate runs inside this loop, so every second of its 429
    would otherwise come out of the agent's turn. Anything that is not a
    sequence of at least two is a ``TypeError``, the failure ``on_permission``
    has always had: the permission is not answered. A third element that is not
    a positive number of seconds is ``0.0``.
    """
    if isinstance(answer, (tuple, list)) and len(answer) >= 2:
        granted = _gate_back_sec(answer[2]) if len(answer) > 2 else 0.0
        return answer[0], answer[1], granted
    raise TypeError(f"on_permission returned {answer!r}, not (reply, message)")


def _context_limit_of(value) -> int | None:
    """KC-10: a model's ``limit.context`` as a positive int, else ``None``.

    The offer sends it as a number, but a custom provider may send it as a
    string, and a limit of ``0`` means "none" the same as an absent one — both
    are the runner's cue to size the model itself, never a raise and never a
    zero a division would blow up on.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    if isinstance(value, str):
        try:
            number = int(value.strip().replace(",", ""))
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
    return None


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
        # Model context limits are read once per round, not once per prompt (KC-10).
        self._model_limits: dict = {}

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

    # ── the offer ───────────────────────────────────────────────────────────

    def providers(self) -> dict:
        """``GET /provider`` — the server's view of what is on offer, decoded as is.

        ``{"all": [provider…], "default": {providerID: modelID}, "connected":
        [providerID…], "failed": […]}``, each provider carrying an ``id``, a
        ``name``, ``source`` and a ``models`` dict keyed by model id — the
        model ids are what ``POST /session`` wants, and a provider's ``name``
        is the display string from ``kilo.jsonc``, which is not its ``id``.
        Nothing is reshaped here: the caller compares the roster's
        ``provider/model`` pairs against the body. A non-dict body is a
        :class:`ValueError`, like :meth:`create_session`'s.
        """
        status, resp = self._request("GET", "/provider")
        self._check(status, resp, "GET", "/provider")
        if not isinstance(resp, dict):
            raise ValueError(
                f"GET /provider returned {type(resp).__name__}, expected a dict")
        return resp

    def model_limit(self, provider_id: str, model_id: str) -> int | None:
        """KC-10: the model's ``limit.context``, else ``None``. Cached per client.

        ``GET /provider`` -> ``all[]`` -> the provider whose ``id`` is *provider_id*
        -> its ``models`` -> *model_id* -> ``limit.context``. ``None`` for a
        provider that is absent, for a model that is absent, for a model with no
        ``limit`` or no ``context`` in it, and for a ``context`` that is not a
        positive number — the free-tier custom providers declare only a name and
        their reasoning, and that is the case this number exists for. Every
        failure is ``None`` too: a body that is not a dict, a non-2xx answer, a
        closed connection. A ``None`` is the runner's cue to size the model
        itself, never a raise into a round.

        An answer that came out of the offer is cached by
        ``(provider_id, model_id)`` — a provider or a model the offer does not
        have, and a limit it does not declare. An offer that could not be read
        is not cached: the read was refused or lost, and the next prompt asks
        again.
        """
        key = (str(provider_id), str(model_id))
        if key in self._model_limits:
            return self._model_limits[key]
        try:
            offer = self.providers()
        except (KiloHttpError, ValueError, TypeError, OSError):
            return None
        if not isinstance(offer, dict):
            return None
        provider = None
        for entry in offer.get("all") or []:
            if isinstance(entry, dict) and entry.get("id") == str(provider_id):
                provider = entry
                break
        if provider is None:
            self._model_limits[key] = None
            return None
        models = provider.get("models")
        model = models.get(model_id) if isinstance(models, dict) else None
        raw = model.get("limit") if isinstance(model, dict) else None
        limit = raw.get("context") if isinstance(raw, dict) else raw
        answer = _context_limit_of(limit)
        self._model_limits[key] = answer
        return answer

    def model_limits(self, provider_id: str, model_id: str) -> dict | None:
        """Round 153: the model's whole ``limit`` from a *fresh* ``GET /provider``.

        The read-back of a ``PATCH /config`` — never :attr:`_model_limits`, which
        may hold the number the patch was meant to replace. Live, 7.6.2: the
        patch answers 200 and writes ``.kilo/kilo.jsonc``, yet a model whose
        ``limit`` the server's own config content (the spawn's
        ``KILO_CONFIG_CONTENT``) sets keeps that limit field by field, and this
        is the only read that shows it — the field this patch compared against.

        ``None`` when the offer cannot be read, when the provider or the model is
        absent, when the model carries no ``limit``, or when it is not a dict: a
        caller that cannot see the limit it asked for must keep its fallback
        armed, never guess a match. Nothing here raises.
        """
        try:
            offer = self.providers()
        except (KiloHttpError, ValueError, TypeError, OSError):
            return None
        if not isinstance(offer, dict):
            return None
        for entry in offer.get("all") or []:
            if not isinstance(entry, dict) or entry.get("id") != str(provider_id):
                continue
            models = entry.get("models")
            if not isinstance(models, dict):
                continue
            model = models.get(model_id)
            raw = model.get("limit") if isinstance(model, dict) else None
            return dict(raw) if isinstance(raw, dict) else None
        return None

    # ── the session ────────────────────────────────────────────────────────

    def create_session(self, provider_id: str, model_id: str, *, rules: list,
                       title: str, agent: str | None = None,
                       variant: str | None = None) -> SessionRef:
        """``POST /session`` — the probe's body, ``model`` as ``{"providerID", "id"}``.

        *variant* (KC-49) is the model's reasoning variant as ``GET /provider``
        lists it (``high``, ``max``, …): it goes into ``model.variant``, and the
        returned ref carries it so every ``prompt`` repeats it. ``None`` sends
        no key — the body is byte-for-byte what it was without it.
        """
        model = {"providerID": provider_id, "id": model_id}
        if variant:
            model["variant"] = variant
        body = {
            "title": title,
            "model": model,
            "permission": list(rules),
        }
        if agent:
            body["agent"] = agent
        status, resp = self._request("POST", "/session", body)
        self._check(status, resp, "POST", "/session")
        if not isinstance(resp, dict) or not isinstance(resp.get("id"), str):
            raise ValueError(f"POST /session returned no session id: {resp!r}")
        return SessionRef(id=resp["id"], provider_id=provider_id, model_id=model_id,
                          agent=agent, directory=self.directory, variant=variant or None)

    def prompt(self, session: SessionRef, text: str) -> None:
        """``prompt_async`` into an existing session; 200 and 204 both succeed.

        The model is ``{"providerID", "modelID"}`` here — a different shape
        than ``create_session``, which is how the live server wants it. The
        session's variant, when it has one, is sent again as the top-level
        ``variant`` (7.6.2's ``prompt_async`` schema): each prompt names its
        model afresh, so the variant is named with it rather than trusted to
        survive on the session.
        """
        path = f"/session/{session.id}/prompt_async"
        body = {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": session.provider_id, "modelID": session.model_id},
        }
        variant = getattr(session, "variant", None)
        if variant:
            body["variant"] = variant
        status, resp = self._request("POST", path, body)
        self._check(status, resp, "POST", path)
        return None

    def compact(self, session: SessionRef) -> None:
        """``POST /session/{id}/summarize`` — the server-side compact (KC-10,
        called by KC-67): the session's own history is shrunk and nothing comes
        back to the caller.

        The model is ``{"providerID", "modelID"}``, the shape :meth:`prompt`
        sends rather than the ``{"providerID", "id"}`` of :meth:`create_session`,
        and ``auto`` is ``False``: the summary alone, never the agent loop after it. The caller waits
        for the resulting ``session.idle`` through the normal tap — a
        ``session.compacted`` event precedes it and is recorded in the tap's log.
        """
        path = f"/session/{session.id}/summarize"
        body = {
            "providerID": session.provider_id,
            "modelID": session.model_id,
            "auto": False,
        }
        # auto:true runs Kilo's own overflow compact, so the next call hangs; the summary alone is used (KC-69).
        status, resp = self._request("POST", path, body, timeout=COMPACT_TIMEOUT_SEC)
        self._check(status, resp, "POST", path)
        return None

    def set_model_limit(self, provider_id: str, model_id: str, limit: dict) -> None:
        """Round 145: ``PATCH /config`` — the model's ``limit`` for this client's
        directory, taken by the running server at once.

        Live, 7.6.2: sent with the session's own ``directory`` the server answers
        200, ``GET /provider`` shows the new ``limit.context``, and the next step
        of a session past ``limit.input`` is compacted by Kilo itself (83 657
        tokens to a summary and a 2 885-token reply). Sent without it, the patch
        lands in the *server's* project and no session sees it. The server keeps
        it as ``.kilo/kilo.jsonc`` in that directory — the caller keeps the file
        out of git (`KiloBackend.set_model_limit`).
        """
        body = {"provider": {provider_id: {"models": {model_id: {"limit": dict(limit)}}}}}
        status, resp = self._request("PATCH", "/config", body)
        self._check(status, resp, "PATCH", "/config")

    def delete_session(self, session: SessionRef) -> None:
        """``DELETE /session/{id}`` — 200 on 7.6.2; KC-49's variant probe
        removes each throwaway session with it."""
        path = f"/session/{session.id}"
        status, resp = self._request("DELETE", path)
        self._check(status, resp, "DELETE", path)
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

    def session_tokens(self, session: SessionRef) -> dict:
        """KC-10: the last assistant message's ``tokens``, plus the session total.

        ``GET /session/{id}/message`` -> the last assistant message -> its
        ``tokens`` — ``total``, ``input``, ``output`` and ``reasoning`` at the
        top level, and ``cache.read`` / ``cache.write`` under ``cache``. That
        is the size the next call will carry, so it is what a fill is read
        from. The session's own ``tokens`` from ``GET /session/{id}`` rides
        along under ``session`` for the summary.

        ``{}`` when the session has no assistant message, when it has one with
        no ``tokens``, when a transcript cannot be read or is not a list, and
        when ``GET /session/{id}`` fails — the session total is simply absent
        then. A reader of a fill treats ``{}`` as "no number", which never
        compacts anything: an unreadable transcript is not an error out of a
        round.
        """
        try:
            messages = self.messages(session)
        except (KiloHttpError, ValueError, TypeError, OSError):
            return {}
        last = None
        for message in reversed(messages if isinstance(messages, list) else []):
            info = message.get("info") if isinstance(message, dict) else None
            if isinstance(info, dict) and info.get("role") == "assistant":
                last = info.get("tokens")
                break
        if not isinstance(last, dict):
            return {}
        out = {key: last.get(key) for key in ("total", "input", "output", "reasoning")}
        cache = last.get("cache")
        if isinstance(cache, dict):
            out["cache"] = {key: cache.get(key) for key in ("read", "write")}
        try:
            info = self.session_info(session)
            tokens = info.get("tokens")
            if isinstance(tokens, dict):
                out["session"] = {key: tokens.get(key) for key in
                                  ("total", "input", "output", "reasoning")}
        except (KiloHttpError, ValueError, TypeError, OSError):
            pass
        return out

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
                  on_question: Callable[[dict], None],
                  on_deadline: Callable[[float], float | None] | None = None,
                  since: int | None = None,
                  max_retry_wait: float | None = None,
                  quota_re: "re.Pattern | None" = None,
                  max_retry_attempts: int | None = None,
                  quiet_after: float | None = None) -> IdleResult:
        """Block until this session goes idle, answering on the way.

        The probe's ``wait_idle`` with the decisions delegated:
        ``on_permission(event) -> (reply, message[, gate_sec])`` decides a
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

        ``on_deadline`` (KC-36) is asked once, with the elapsed seconds, at
        the moment the overall ``timeout`` is reached and before ``abort`` is
        sent: the caller decides then whether the deadline moves. A positive
        number is added to it and the wait keeps going; ``None``, ``0``, a
        negative number, anything that is not a number, or an exception
        (logged, never raised) aborts exactly as today. Omitted — which is
        every existing caller — the loop is byte for byte what it was: the
        callback is never called and ``abort`` goes at ``timeout``. The
        silence clock is untouched and keeps racing it, so an extension never
        resurrects a session that has gone quiet.

        KC-47 widens that window while the session has a ``bash`` call in
        flight. A ``message.part.updated`` of this session whose part is
        ``type == "tool"``, ``tool == "bash"`` and in ``state.status``
        ``running`` (or ``pending``) is *open*, keyed by part id; the same id
        in any other status closes it. While one is open the silence bound is
        the widest of ``idle_event_timeout`` and each call's own
        ``state.input.timeout`` (milliseconds → seconds) plus
        ``idle_event_timeout`` as grace, measured from that call's ``running``
        event: Kilo kills the call first, and its ``completed`` event is what
        resets the clock. A call that names no ``timeout`` uses
        ``_KILO_BASH_DEFAULT_TIMEOUT_MS``, never "forever". Nothing else is
        suspended — a ``task``, an ``edit``, a ``read`` keeps KC-12's clock —
        and with no part open the loop is KC-12's, event for event. When the
        bound fires with a part still open, ``IdleResult.open_tool`` carries
        it; ``timeout`` still bounds everything above it.

        Events that a permission or a question was *answered* for are
        collected in ``permissions`` / ``questions``, so a caller can audit
        or persist the turn. A timeout sends ``abort`` first; a failure of
        that abort (the session is already gone) is logged, not raised. A
        failure of the reply itself is logged and skipped — the session then
        reaches the timeout like any other, which is the only honest outcome.

        ``since`` (KC-63) is a :meth:`EventTap.mark` taken right before the
        prompt this wait belongs to. Every unread event before it is skipped
        and never examined: it is an earlier turn's. ``IdleResult.stale_skipped``
        counts the skipped ones of this session that a wait acts on (an idle,
        an error, a permission, a question). Omitted, nothing is skipped and
        the wait is as before.

        ``max_retry_wait`` and ``quota_re`` (KC-61) make a ``session.status`` of
        this session whose ``status`` is ``{"type": "retry", "next": <ms>}`` a
        terminal event instead of a beat. ``next`` is epoch milliseconds of the
        time Kilo will retry on its own, so it is compared with the wall clock,
        ``time.time()`` — the rest of the wait is ``time.monotonic()``. A retry
        scheduled further out than ``max_retry_wait`` seconds ends the wait as
        ``status="error"`` with ``error["name"] == "ProviderQuota"``, carrying
        the provider's text in ``data.message`` and ``next`` in ``data.retryAt``,
        and the session is aborted first: that is a quota reset until midnight,
        not a blip, and the silence clock must not be the thing that names it.
        Round 146: that is the *first* retry of a streak, a retry whose text is a
        quota phrase, or a delay past ``_BACKOFF_CROSSING_FACTOR`` times the
        bound. A later retry that is past the bound without any of them is
        Kilo's own doubling of an error it keeps repeating (round 146: a 400,
        nine attempts, 519 s): the wait ends and the session is aborted just
        the same, but as ``ProviderUnavailable`` with ``data.attempts`` — the
        provider named no reset (`_retry_past_the_bound`).
        A retry inside the bound is only a beat, and a ``next`` that is missing
        or is not a number is judged by ``quota_re``, a compiled pattern over the
        provider's text. Both omitted — every pre-KC-61 caller — a
        ``session.status`` is never acted on and the loop is byte for byte what
        it was, silence clock or not.

        ``max_retry_attempts`` (KC-64) is the other end of the same
        ``session.status``: a Kilo retry *nearby in time*, over and over. Counted
        are this session's ``type == "retry"``, and the count resets on any
        assistant-side ``message.part.updated`` of this session — a
        ``step-start``, ``reasoning``, ``tool`` or ``step-finish`` part, which is
        the model answering. A ``text`` part does not reset it: the runner's own
        prompt comes back as ``text`` parts (round 106's laguna: nine of them,
        zero tokens out), and on this provider that is the *whole* of the turn.
        When both Kilo's own ``attempt`` and that local count reach the limit, the
        wait ends as ``status="error"`` with ``error["name"] ==
        "ProviderUnavailable"`` — the provider's text in ``data.message`` and the
        count in ``data.attempts`` — and the session is aborted first, so Kilo
        never keeps its own retry loop running past the round's decision. Kilo
        resets ``attempt`` after every success, so a provider that answers now and
        then never reaches the limit: the count and the limit have to be crossed
        in one go, with no output between. Omitted or ``0`` — every pre-KC-64
        caller — nothing is counted and the loop is byte for byte KC-61's,
        silence clock or not. The KC-61 quota check runs first: a daily quota must
        end at ``attempt: 1``, not at ``attempt: 10``.

        ``quiet_after`` (KC-73) is for a session whose turn has already ended in
        a ``session.error``: is Kilo still working in it? Every event of this
        session (and of its subagents) is read, as with the silence clock on.
        When none has come ``quiet_after`` seconds into the wait, the wait ends
        as ``status="quiet"`` — nothing is running, and nothing is aborted. Once
        one has come, the wait is the ordinary one to the end, asks answered.
        A ``session.compacted`` read on the way sets ``IdleResult.compacted``.
        Omitted — every other caller — the loop is byte for byte KC-64's.
        """
        # Events from before this prompt are an earlier turn's, so they are skipped (KC-63).
        stale = _skip_stale(tap, session.id, since) if since is not None else 0
        # KC-63, diagnostic only: did this session go busy in this wait?
        saw_busy = False
        # KC-64: retries Kilo reported since this session last produced output
        retries_without_output = 0
        provider_errors = 0
        started = time.monotonic()
        deadline = started + float(timeout)
        # A provider retry streak counts as the provider's time, so it moves the turn deadline (round 146).
        retry_mark: float | None = None
        retry_waited = 0.0
        retry_streak = 0.0
        retry_cap = max(0.0, float(timeout))
        session_id = session.id
        # KC-12: a None or non-positive silence clock is off, and the loop is then KC-1's.
        silence = float(idle_event_timeout) if idle_event_timeout is not None else None
        if silence is not None and silence <= 0:
            silence = None
        last_seen = started
        # KC-47: open bash parts map to their own deadlines; empty, the loop is KC-12's.
        open_parts: dict = {}
        # A limit that is not a positive count turns the rule off, rather than raising (KC-64).
        try:
            retries_limit = int(max_retry_attempts)
        except (TypeError, ValueError):
            retries_limit = 0
        if retries_limit <= 0:
            retries_limit = None
        permissions: list = []
        questions: list = []
        # KC-73: has this session sent anything yet, and did Kilo compact it
        quiet_window = float(quiet_after) if quiet_after is not None else None
        heard = False
        compacted = False
        # Child sessions started by the task tool are tracked too, so their asks are answered (KC-71, round 87).
        children: set = set()

        def settle_retries(now: float, *, ended: bool = False) -> None:
            """Round 146: add the streak's seconds up to *now* to the deadline."""
            nonlocal deadline, retry_mark, retry_waited, retry_streak
            if retry_mark is None:
                return
            if on_deadline is not None:
                gave = max(0.0, min(now - retry_mark, retry_cap - retry_waited))
                deadline += gave
                retry_waited += gave
                retry_streak += gave
            if ended:
                if retry_streak >= 1.0:
                    _log.info("%s: the provider's retries ended after %.0f s — the turn's "
                              "deadline moved by as much (%.0f s so far)",
                              session_id, retry_streak, retry_waited)
                retry_streak = 0.0
                retry_mark = None
            else:
                retry_mark = now

        def wanted(event: dict) -> bool:
            nonlocal saw_busy
            etype = event.get("type")
            if etype == "tap.closed":
                # A tap-level event has no session id, and applies to whatever stream is read.
                return True
            child = _child_session(event, children | {session_id})
            if child is not None:
                children.add(child)
            event_session = (event.get("properties") or {}).get("sessionID")
            if event_session != session_id:
                # KC-71: a subagent's ask is answered as the agent's own (the clock counts its work too).
                return event_session in children and (
                    etype in _CHILD_ASKS or silence is not None or quiet_window is not None)
            if not saw_busy and _is_busy(event):
                # KC-63: noted before the filter, so it is seen even with the clock off.
                saw_busy = True
            # With the silence clock on, each event of this session wakes the wait, handled or not.
            if (silence is not None or quiet_window is not None or etype in _SESSION_EVENTS
                    or (max_retry_wait is not None and etype == "session.status")
                    or (on_deadline is not None and etype == "session.status")
                    or (retries_limit and etype in ("session.status",
                                                     "message.part.updated"))):
                return True
            return False

        while True:
            now = time.monotonic()
            settle_retries(now)
            overall_left = deadline - now
            silence_left = None
            if silence is not None:
                # Whichever bound runs out first ends the wait; an open bash call widens the silence bound (KC-47).
                silence_left = _silence_left(silence, last_seen, open_parts, now)
                left = min(overall_left, silence_left)
            else:
                left = overall_left
            if quiet_window is not None and not heard:
                # KC-73: nothing has come from the session yet, so it is not a stall and is left as it is.
                quiet_left = started + quiet_window - now
                if quiet_left <= 0:
                    return IdleResult(status="quiet", elapsed=now - started,
                                      permissions=permissions, questions=questions,
                                      stale_skipped=stale)
                left = min(left, quiet_left)
            if left <= 0:
                # The turn deadline is checked first; the silence clock is not extended by the worktree (KC-36).
                quiet = silence_left is not None and silence_left <= 0
                if not quiet and overall_left <= 0 and on_deadline is not None:
                    grant = _deadline_grant(on_deadline, time.monotonic() - started)
                    # A grant that leaves the deadline in the past aborts, rather than re-asking in a hot loop.
                    if grant is not None and deadline + grant > time.monotonic():
                        deadline += grant
                        continue
                self._abort_quietly(session)
                # Only the silence bound reports the open tool; a turn-deadline stop says the turn never idled.
                open_tool = _open_tool_report(open_parts, now) if quiet else None
                return IdleResult(status="timeout", elapsed=time.monotonic() - started,
                                  permissions=permissions, questions=questions,
                                  open_tool=open_tool, stale_skipped=stale,
                                  retry_waited=retry_waited)
            event = tap.wait(wanted, left)
            if event is None:
                continue
            last_seen = time.monotonic()
            if event.get("type") != "tap.closed":
                heard = True

            etype = event.get("type")
            props = event.get("properties") or {}
            # KC-71: a subagent's event has its asks answered below, on the subagent's session.
            asker = session
            if props.get("sessionID") in children:
                if etype not in _CHILD_ASKS:
                    # The subagent is working, so the clock was reset, and its bash holds it open.
                    if etype == "message.part.updated":
                        _track_open_part(open_parts, props.get("part"), last_seen)
                    continue
                asker = replace(session, id=props["sessionID"])

            if etype == "message.part.updated":
                # KC-47: an open bash part widens the silence bound; the event that opened it reset the clock.
                _track_open_part(open_parts, props.get("part"), last_seen)
                # Any assistant-side part resets the silence clock; a plain text part does not (KC-64).
                part = props.get("part")
                if isinstance(part, dict) and \
                        part.get("type") in ("step-start", "reasoning", "tool", "step-finish"):
                    retries_without_output = 0
                    settle_retries(last_seen, ended=True)

            if etype in ("permission.asked", "permission.v2.asked"):
                try:
                    reply, message, granted = _permission_answer(on_permission(event))
                    self.reply_permission(asker, props.get("id"), reply, message)
                    # Gate waits are the agent's time, so they are granted back to the deadline (KC-66, KC-58).
                    if granted > 0:
                        deadline += granted
                        _log.info("%s: gate/suite waits granted +%gs to the turn",
                                  session_id, granted)
                except (KiloHttpError, ValueError, TypeError) as e:
                    _log.warning("permission %s not answered: %s", props.get("id"), e)
                # The silence clock runs from the reply, since a suite slot can outlast the window (KC-58).
                last_seen = time.monotonic()
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
            if etype == "session.status":
                _st = props.get("status")
                if isinstance(_st, dict) and _st.get("type") == "retry":
                    provider_errors += 1
                    if retry_mark is None:
                        retry_mark = last_seen
            if etype == "session.status" and max_retry_wait is not None:
                # Kilo's own retry time is epoch milliseconds, compared with the wall clock (KC-61).
                status = props.get("status") or {}
                if isinstance(status, dict) and status.get("type") == "retry":
                    message = str(status.get("message") or "")
                    nxt = status.get("next")
                    wait = (float(nxt) / 1000.0 - time.time()
                            if isinstance(nxt, (int, float))
                            and not isinstance(nxt, bool) else None)
                    attempt = status.get("attempt")
                    far = _retry_past_the_bound(
                        wait, attempt,
                        quota_re is not None and quota_re.search(message) is not None,
                        float(max_retry_wait))
                    if far == "quota":
                        # A quota reset ends the wait at once, with the provider's text (KC-61).
                        self._abort_quietly(session)
                        return IdleResult(
                            status="error",
                            error={"name": "ProviderQuota",
                                   "data": {"message": message, "retryAt": nxt}},
                            elapsed=elapsed, permissions=permissions,
                            questions=questions, stale_skipped=stale)
                    if far == "backoff":
                        # A backoff the provider did not name ends as ProviderUnavailable, not a quota (round 146).
                        self._abort_quietly(session)
                        return IdleResult(
                            status="error",
                            error={"name": "ProviderUnavailable",
                                   "data": {"message": message, "attempts": attempt}},
                            elapsed=elapsed, permissions=permissions,
                            questions=questions, stale_skipped=stale)
            if etype == "session.status" and retries_limit:
                # A nearby retry is counted per attempt; the quota check above runs first (KC-64).
                status = props.get("status")
                if isinstance(status, dict) and status.get("type") == "retry":
                    retries_without_output += 1
                    attempt = status.get("attempt")
                    count = (attempt if isinstance(attempt, int)
                             and not isinstance(attempt, bool)
                             else retries_without_output)
                    if min(count, retries_without_output) >= retries_limit:
                        # Kilo's endless retry is stopped, since its busy beats would reset the clock forever.
                        self._abort_quietly(session)
                        return IdleResult(
                            status="error",
                            error={"name": "ProviderUnavailable",
                                   "data": {"message": str(status.get("message") or ""),
                                            "attempts": count}},
                            elapsed=elapsed, permissions=permissions,
                            questions=questions, stale_skipped=stale)
                continue
            if etype == "session.compacted":
                compacted = True
                continue
            if etype == "session.error":
                settle_retries(last_seen, ended=True)
                return IdleResult(status="error", error=props.get("error", props),
                                  elapsed=elapsed, permissions=permissions,
                                  questions=questions, stale_skipped=stale,
                                  compacted=compacted, retry_waited=retry_waited)
            if etype == "tap.closed":
                return IdleResult(status="closed", error=props.get("error"),
                                  elapsed=elapsed, permissions=permissions,
                                  questions=questions, stale_skipped=stale)
            if etype != "session.idle":
                # Only with the silence clock on: an event of this session that reset it and asks nothing.
                continue
            if not saw_busy:
                # KC-63, diagnostic only: notes how a stale idle is noticed; the result does not change.
                _log.warning("%s: idle without busy after %.1fs", session_id, elapsed)
            settle_retries(last_seen, ended=True)
            return IdleResult(status="idle", elapsed=elapsed,
                              permissions=permissions, questions=questions,
                              stale_skipped=stale, compacted=compacted,
                              provider_errors=provider_errors,
                              retry_waited=retry_waited)
