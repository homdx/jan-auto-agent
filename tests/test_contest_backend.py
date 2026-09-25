"""tests/test_contest_backend.py — KC-34: the backend protocol and its two backends.

`tools/contest/backend.py` is the seam `runner.py` used to reach into a
`KiloClient` and a `EventTap` by name. Nothing here starts a `kilo` binary or
calls a live provider: the Kilo side is `tests/_kilo_fake.py` (or a
monkeypatched client and tap), and the OpenRouter side is
`_kilo_fake.FakeOpenRouter`, a scripted `/chat/completions`, with the agent's
subprocess pipes faked in-process for the mechanics and real for the one loop
test at the bottom.

The cases, from the ticket:

  * both backends satisfy `isinstance(b, ContestBackend)` — the Protocol is
    `@runtime_checkable`, so a backend that forgets a method is caught here;
  * `KiloBackend.create_session` delegates to the existing `KiloClient` and a
    `KiloHttpError` becomes a `ContestBackendError`, the runner's one new
    exception to catch;
  * `KiloBackend.tool_parts` delegates, so the gate still sees the session's
    history;
  * `KiloBackend.close` stops and joins its own tap — the per-agent cleanup
    `run_round` used to do itself, now the backend's;
  * `OpenRouterBackend.prompt` writes the text to the subprocess stdin, and
    `wait_idle` gives `idle` on the sentinel, `timeout` on a silent agent and
    `error` on a provider failure, `data.isRetryable` set for a 429;
  * the agent loop asks about every bash call in the shape `policy.decide`
    already reads, so the gate enforces this backend without a Kilo server —
    the ticket's Open Question, resolved in favour of the gate rather than a
    wide-open backend.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _kilo_fake import FakeKiloServer, FakeOpenRouter, _openai_reply, _openai_tool  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest import backend as backend_mod  # noqa: E402
from tools.contest.backend import (  # noqa: E402
    ContestBackend,
    ContestBackendError,
    KiloBackend,
    OpenRouterBackend,
)
from tools.contest.kilo_client import IdleResult, KiloHttpError, KiloServer  # noqa: E402
from tools.contest.policy import HARD_DENYLIST, Policy, PolicyContext  # noqa: E402
from tools.contest.roster import ContestConfig  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


# ─────────────────────────────────────────────────────────────────────────────
# fakes for the two backends' transports
# ─────────────────────────────────────────────────────────────────────────────

class AgentPipe:
    """The agent's two pipes as one in-process object, for the backend tests.

    ``stdout.readline`` blocks until a line is pushed — like a real pipe — and
    ``close`` pushes the EOF marker the way the process dying would, so the
    backend's reader thread wakes on an EOF instead of holding a deadline.
    ``write`` collects what the backend sends on stdin, in order.
    """

    def __init__(self) -> None:
        self._lines: "queue.Queue" = queue.Queue()
        self.stdin_lines: list = []

    def push(self, line) -> None:
        self._lines.put(None if line is None else line)

    def readline(self) -> str:
        line = self._lines.get()
        return "" if line is None else line

    def write(self, text: str) -> int:
        self.stdin_lines.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.push(None)

    def sent(self) -> list:
        """Every stdin line as a parsed dict, in order."""
        out = []
        for line in self.stdin_lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append(line)
        return out


class FakeAgentProcess:
    """A `subprocess.Popen` with the parts `OpenRouterBackend` touches."""

    def __init__(self, pipe: AgentPipe) -> None:
        self._pipe = pipe
        self.stdin = pipe
        self.stdout = pipe
        self.returncode: "int | None" = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self._pipe.close()

    def kill(self) -> None:
        self.returncode = -9


def _spawn(pipe: AgentPipe):
    """A fake `spawn_agent`: a process with the pipe as both directions."""
    proc = FakeAgentProcess(pipe)
    return proc


def _agent(*, url: str = "http://127.0.0.1:1/v1", directory: str = "/tmp/ws",
           key: str = "sk-test", timeout: float = 10.0, pipe: AgentPipe | None = None):
    """An `OpenRouterBackend` over one in-process pipe, with nothing spawned."""
    pipe = pipe or AgentPipe()
    return OpenRouterBackend(key, url, directory, timeout=timeout,
                             spawn_agent=lambda *args: _spawn(pipe)), pipe


def _decision(event: dict, worktree: Path) -> "tuple":
    """One `permission.asked` through the real policy, with a gate that refuses
    to be called — so an answer that arrives is the mechanical layer's."""
    called = []

    def gate(*args, **kwargs):
        called.append(args)
        raise AssertionError("the gate must not be called")

    config = ContestConfig(tmp_roots=("/tmp/kilo/*",))
    policy = Policy(config, completion_fn=gate)
    ctx = PolicyContext(worktree=worktree, tmp_roots=("/tmp/kilo/*",),
                        forbidden=tuple(HARD_DENYLIST) + (worktree.parent,),
                        ticket_title="73-kc34.md", ticket_files=("tools/x.py",))
    return policy.decide(event, ctx), called


# ─────────────────────────────────────────────────────────────────────────────
# the protocol
# ─────────────────────────────────────────────────────────────────────────────

def test_both_backends_satisfy_the_protocol_at_runtime(tmp_path):
    """`@runtime_checkable` means a backend missing a method fails here."""
    with FakeKiloServer({"turns": []}) as fake:
        server = KiloServer.attach(fake.url)
        kilo = KiloBackend(server, str(tmp_path),
                           events_log=str(tmp_path / "e.jsonl"))
        assert isinstance(kilo, ContestBackend)
        kilo.close()

    pipe = AgentPipe()
    openrouter = OpenRouterBackend("sk", "http://127.0.0.1:1/v1", str(tmp_path),
                                   spawn_agent=lambda *args: _spawn(pipe))
    assert isinstance(openrouter, ContestBackend)
    openrouter.close()


def test_the_protocol_names_every_method_a_backend_has_to_own():
    """A method dropped from a backend shows up here, not on the first round."""
    required = {"wait_ready", "create_session", "prompt", "mark", "abort", "interrupt",
                "interrupted", "wait_idle", "tool_parts", "session_info",
                "messages", "close"}
    protocol_methods = {name for name in dir(ContestBackend) if not name.startswith("_")}
    assert required == protocol_methods, protocol_methods ^ required
    for name in required:
        assert callable(getattr(KiloBackend, name)), name
        assert callable(getattr(OpenRouterBackend, name)), name


def test_contest_backend_error_is_the_runner_s_new_exception():
    exc = ContestBackendError("no such route")
    assert isinstance(exc, Exception) and not isinstance(exc, ValueError)
    assert str(exc) == "no such route"


# ─────────────────────────────────────────────────────────────────────────────
# KiloBackend
# ─────────────────────────────────────────────────────────────────────────────

class _RecordingClient:
    """A `KiloClient` stand-in: records every call, answers scripted bodies."""

    def __init__(self, server, directory):
        self.server, self.directory = server, directory
        self.calls: list = []
        self.create_result = None
        self.create_error = None
        self.tool_result = []

    def create_session(self, provider_id, model_id, *, rules, title, agent=None, variant=None):
        call = ("create_session", provider_id, model_id, rules, title, agent)
        self.calls.append(call + (variant,) if variant is not None else call)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def prompt(self, session, text):
        self.calls.append(("prompt", session, text))

    def abort(self, session):
        self.calls.append(("abort", session))

    def tool_parts(self, session):
        self.calls.append(("tool_parts", session))
        return self.tool_result

    def session_info(self, session):
        self.calls.append(("session_info", session))
        return {}

    def messages(self, session):
        self.calls.append(("messages", session))
        return []

    def wait_idle(self, tap, session, timeout, *, idle_event_timeout=None,
                  on_permission, on_question, since=None):
        self.calls.append(("wait_idle", tap, session, timeout, idle_event_timeout))
        return IdleResult(status="idle")


class _RecordingTap:
    """A `EventTap` stand-in: records `stop` and `join` with their arguments."""

    def __init__(self, base_url, directory, log_path):
        self.base_url, self.directory, self.log_path = base_url, directory, log_path
        self.stopped = 0
        self.joined = []

    def start(self):
        return self

    def stop(self):
        self.stopped += 1
        return self

    def join(self, timeout=5.0):
        self.joined.append(timeout)
        return True


class _Server:
    """A `KiloServer` with only what `KiloBackend` reads: the base URL."""

    base_url = "http://127.0.0.1:1"


def test_kilo_backend_builds_its_client_and_tap_from_the_server(monkeypatch, tmp_path):
    """Nothing is faked here but the two transports `KiloBackend` owns."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    log = str(tmp_path / "agent-a" / "events.jsonl")
    server = _Server()
    ws = str(tmp_path / "ws")
    backend = KiloBackend(server, ws, events_log=log)
    client, tap = backend._client, backend._tap
    assert client.server is server and client.directory == ws
    assert (tap.base_url, tap.directory, tap.log_path) == (
        "http://127.0.0.1:1", str(tmp_path / "ws"), log)
    assert client.calls == []


def test_kilo_backend_without_an_events_log_is_a_backend_error(tmp_path):
    """The tap has to log somewhere; a missing path is a construction error."""
    with pytest.raises(ContestBackendError):
        KiloBackend(_Server(), str(tmp_path / "ws"))


def test_kilo_backend_create_session_delegates_to_the_client(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    session = backend.create_session("kenary", "hy3:free", rules=[{"permission": "*"}],
                                     title="contest/73/hy3", agent=None)
    (call,) = backend._client.calls
    assert call == ("create_session", "kenary", "hy3:free", [{"permission": "*"}],
                    "contest/73/hy3", None)
    assert session is backend._client.create_result


def test_kilo_backend_turns_a_kilo_http_error_into_a_backend_error(monkeypatch, tmp_path):
    """The runner catches one exception class, not both."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    backend._client.create_error = KiloHttpError(400, {"error": {"message": "unknown model"}},
                                                 "POST", "/session")
    with pytest.raises(ContestBackendError, match="unknown model"):
        backend.create_session("kenary", "nope:free", rules=[], title="t")


def test_kilo_backend_prompt_raises_a_backend_error_too(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    backend._client.prompt = lambda session, text: (_ for _ in ()).throw(
        KiloHttpError(0, "URLError: timed out", "POST", "/session/s/prompt_async"))
    with pytest.raises(ContestBackendError):
        backend.prompt(None, "text")


def test_kilo_backend_tool_parts_delegates(monkeypatch, tmp_path):
    """The gate reads the session history through this one line."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    parts = [{"tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}}}]
    backend._client.tool_result = parts
    session = object()
    assert backend.tool_parts(session) is parts
    assert backend._client.calls[-1] == ("tool_parts", session)


def test_kilo_backend_wait_idle_passes_the_tap_and_the_rounds_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    session = object()
    result = backend.wait_idle(session, 1800.0, idle_event_timeout=300.0,
                               on_permission=lambda e: ("once", ""),
                               on_question=lambda e: None)
    assert result.status == "idle"
    (call,) = backend._client.calls
    assert call == ("wait_idle", backend._tap, session, 1800.0, 300.0)


def test_kilo_backend_close_stops_then_joins_its_own_tap(monkeypatch, tmp_path):
    """The per-agent `tap.stop()` / `tap.join(2.0)` `run_round` used to do itself."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    backend.close()
    tap = backend._tap
    assert tap.stopped == 1
    assert tap.joined == [2.0], "close stops and joins, in that order, with the runner's grace"
    # idempotent: the runner may call it again on the Ctrl-C path
    backend.close()
    assert tap.stopped == 2 and tap.joined == [2.0, 2.0]


def test_kilo_backend_interrupt_stops_the_tap_and_reports_it(monkeypatch):
    """`interrupt` is what the runner's `stall` and Ctrl-C call instead of `tap.stop`;
    `interrupted` is what the retry backoff checks instead of `tap._stop`."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), "/tmp/ws", events_log="/tmp/ws/e.jsonl")
    assert backend.interrupted() is False
    backend.interrupt(None)
    assert backend.interrupted() is True
    assert backend._tap.stopped == 1
    assert backend._tap.joined == [], "interrupt stops; only `close` joins"


def test_kilo_backend_aborts_without_raising(monkeypatch, tmp_path):
    """A session that is already gone is not an error: the runner calls this on Ctrl-C."""
    monkeypatch.setattr(backend_mod, "KiloClient", _RecordingClient)
    monkeypatch.setattr(backend_mod, "EventTap", _RecordingTap)
    backend = KiloBackend(_Server(), str(tmp_path / "ws"), events_log=str(tmp_path / "e.jsonl"))
    backend._client.abort = lambda session: (_ for _ in ()).throw(
        KiloHttpError(404, {"error": "gone"}, "POST", "/session/s/abort"))
    backend.abort(None)  # nothing raises
    assert backend._tap.stopped == 0, "abort is not interrupt: it does not close the stream"


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouterBackend — the mechanics, over an in-process pipe
# ─────────────────────────────────────────────────────────────────────────────

def test_openrouter_prompt_writes_the_text_to_the_subprocess_stdin():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    backend.prompt(session, "implement the ticket")
    (first,) = pipe.sent()
    assert first == {"type": "prompt", "text": "implement the ticket"}
    backend.close()


def test_openrouter_create_session_records_the_agent_and_the_model():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "agnes-2-5-flash:free", rules=[],
                                     title="contest/73/agnes", agent=None)
    assert session.provider_id == "openrouter"
    assert session.model_id == "agnes-2-5-flash:free"
    assert session.agent is None
    assert backend.session_info(session)["tokens"]["total"] == 0
    backend.close()


def test_openrouter_wait_idle_returns_idle_on_the_sentinel():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    pipe.push(json.dumps({"type": "message", "parts": [{"type": "text", "text": "done"}]}))
    pipe.push(json.dumps({"type": "usage", "input": 12, "output": 34}))
    pipe.push(json.dumps({"type": "idle"}))
    questions = []
    result = backend.wait_idle(session, 60.0, on_permission=lambda e: ("once", ""),
                               on_question=lambda e: questions.append(e))
    assert result.status == "idle"
    assert result.error is None and result.permissions == [] and result.questions == []
    assert questions == [], "on_question is never called: this backend has no question event"
    assert len(backend.messages(session)) == 1
    assert backend.session_info(session)["tokens"] == {"total": 46, "input": 12, "output": 34}
    backend.close()


def test_openrouter_wait_idle_times_out_on_a_silent_agent():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    started = time.monotonic()
    result = backend.wait_idle(session, 0.3, on_permission=lambda e: ("once", ""),
                               on_question=lambda e: None)
    elapsed = time.monotonic() - started
    assert result.status == "timeout"
    # FL-1 (round 84): the lower bound is the claim (it waited out the 0.3 s
    # deadline rather than returning early) and only gets safer under load.
    # The upper bound is a "did not hang" guard, so it is generous.
    assert 0.25 < elapsed < 30.0, elapsed
    assert 0.25 < result.elapsed < 30.0
    backend.close()


def test_openrouter_timeout_terminates_the_subprocess():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    backend.wait_idle(session, 0.1, on_permission=lambda e: ("once", ""),
                      on_question=lambda e: None)
    assert backend._records[session.id].proc.returncode == -15
    backend.close()


def test_openrouter_records_a_permission_event_and_replies_on_stdin():
    """The gate runs here: the runner's on_permission decides, and the reply
    goes back to the agent on the same pipe the prompt came in on."""
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    event = {"type": "permission.asked",
             "properties": {"id": "call_1-bash", "sessionID": "hy3:free",
                            "permission": "bash", "patterns": [],
                            "metadata": {"command": "git status"},
                            "tool": {"messageID": "call_1", "callID": "call_1"}}}
    pipe.push(json.dumps(event))
    pipe.push(json.dumps({"type": "idle"}))
    asked = []

    def on_permission(candidate):
        asked.append(candidate)
        return "reject", "the reviewer refused this command"

    result = backend.wait_idle(session, 60.0, on_permission=on_permission,
                               on_question=lambda e: None)
    assert result.status == "idle"
    assert asked == [event] and len(result.permissions) == 1
    replies = [record for record in pipe.sent() if record.get("type") == "permission.reply"]
    assert replies == [{"type": "permission.reply", "id": "call_1-bash", "reply": "reject",
                        "message": "the reviewer refused this command"}]
    backend.close()


def test_openrouter_a_broken_policy_answers_reject_not_a_crash():
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    pipe.push(json.dumps({"type": "permission.asked",
                          "properties": {"id": "p", "permission": "bash", "patterns": [],
                                         "metadata": {"command": "ls"}}}))
    pipe.push(json.dumps({"type": "idle"}))

    def explode(event):
        raise RuntimeError("a policy that raises")

    result = backend.wait_idle(session, 60.0, on_permission=explode,
                               on_question=lambda e: None)
    assert result.status == "idle"
    replies = [record for record in pipe.sent() if record.get("type") == "permission.reply"]
    assert replies[0]["reply"] == "reject" and "policy failed" in replies[0]["message"]
    backend.close()


def test_openrouter_a_provider_error_is_a_retryable_idle_result():
    """`data.isRetryable` is what the runner's `_retryable` reads (KC-19)."""
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    pipe.push(json.dumps({"type": "error", "data": {"name": "AgentError", "message": "429",
                                                    "isRetryable": True,
                                                    "metadata": {"code": "429", "status": "429"}}}))
    result = backend.wait_idle(session, 60.0, on_permission=lambda e: ("once", ""),
                               on_question=lambda e: None)
    assert result.status == "error"
    assert result.error["isRetryable"] is True and result.error["message"] == "429"
    backend.close()


def test_openrouter_a_dead_agent_is_a_closed_stream():
    """The session is gone; the worktree is not — the runner harvests what is there."""
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    backend.abort(session)          # EOF on the pipe
    pipe.push("   ")                # a blank line is not an event, not an EOF
    result = backend.wait_idle(session, 60.0, on_permission=lambda e: ("once", ""),
                               on_question=lambda e: None)
    assert result.status == "closed"
    assert "exit" in result.error
    backend.close()


def test_openrouter_tool_parts_returns_the_agents_own_calls():
    """Not `[]`: the gate reads these as recent tools, same as on a Kilo session."""
    backend, pipe = _agent()
    session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    first = {"type": "tool", "tool": "bash",
             "state": {"status": "completed", "input": {"command": "ls"}, "output": "a"}}
    second = {"type": "tool", "tool": "write",
              "state": {"status": "completed", "input": {"path": "x.py"}, "output": "wrote x.py"}}
    pipe.push(json.dumps(first))
    pipe.push(json.dumps(second))
    pipe.push(json.dumps({"type": "idle"}))
    backend.wait_idle(session, 60.0, on_permission=lambda e: ("once", ""),
                      on_question=lambda e: None)
    assert backend.tool_parts(session) == [first, second]
    backend.close()


def test_openrouter_unknown_session_is_a_backend_error():
    backend, pipe = _agent()
    from tools.contest.kilo_client import SessionRef

    session = SessionRef(id="nope", provider_id="openrouter", model_id="hy3:free",
                         directory="/tmp")
    for method in (lambda: backend.prompt(session, "x"), lambda: backend.tool_parts(session),
                   lambda: backend.session_info(session), lambda: backend.messages(session),
                   lambda: backend.abort(session), lambda: backend.interrupt(session),
                   lambda: backend.wait_idle(session, 1.0, on_permission=lambda e: ("once", ""),
                                             on_question=lambda e: None)):
        with pytest.raises(ContestBackendError, match="no session"):
            method()
    backend.close()


def test_openrouter_close_terminates_every_agent_and_gives_them_back():
    """One `close` per agent in a round; the processes do not outlive it."""
    spawned = []

    def spawn(*args):
        pipe = AgentPipe()
        proc = _spawn(pipe)
        spawned.append(proc)
        return proc

    backend = OpenRouterBackend("sk", "http://127.0.0.1:1/v1", "/tmp", spawn_agent=spawn)
    backend.create_session("openrouter", "hy3:free", rules=[], title="t")
    backend.create_session("openrouter", "hy3:pro", rules=[], title="t")
    assert len(spawned) == 2
    backend.close()
    assert backend._records == {}, "close gives the processes back"
    assert [proc.returncode for proc in spawned] == [-15, -15]
    backend.close()  # idempotent: nothing left to terminate
    assert [proc.returncode for proc in spawned] == [-15, -15]


def test_openrouter_wait_ready_is_a_noop_for_a_fresh_subprocess():
    """A spawned process has nothing to race: the buffer holds what it writes."""
    backend, pipe = _agent()
    assert backend.wait_ready() is None


def test_openrouter_needs_a_base_url():
    with pytest.raises(ContestBackendError, match="base_url"):
        OpenRouterBackend("sk", "", "/tmp")


# ─────────────────────────────────────────────────────────────────────────────
# the agent loop, for real — a spawned subprocess against the fake endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_the_agent_loop_runs_tool_calls_and_asks_about_bash(tmp_path, monkeypatch):
    """One real subprocess over `_kilo_fake.FakeOpenRouter`: the file is written,
    the bash call is gated on the way, and the turn ends on the sentinel."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    script = [
        _openai_reply(calls=[_openai_tool(
            "write", {"path": "pkg/thing.py", "content": "def thing():\n    return 42\n"})]),
        _openai_reply(calls=[_openai_tool("bash", {"command": "ls pkg"})]),
        _openai_reply("done"),
    ]
    with FakeOpenRouter(script) as fake:
        backend = OpenRouterBackend("sk-test", fake.url, str(tmp_path), timeout=20.0)
        session = backend.create_session("openrouter", "agnes-2-5-flash:free", rules=[],
                                         title="contest/73/agnes")
        backend.prompt(session, "implement the ticket")
        asked = []
        result = backend.wait_idle(
            session, 25.0,
            on_permission=lambda event: (asked.append(event) or ("once", "fine")),
            on_question=lambda event: None)
    parts = backend.tool_parts(session)
    info = backend.session_info(session)
    backend.close()

    assert result.status == "idle", result.error
    assert (tmp_path / "pkg" / "thing.py").read_text(encoding="utf-8") == \
        "def thing():\n    return 42\n"
    assert len(fake.requests) == 3, [record["path"] for record in fake.requests]
    assert fake.requests[0]["authorization"] == "Bearer sk-test"
    assert fake.requests[0]["body"]["model"] == "agnes-2-5-flash:free"
    # a write is asked about too, with the path in `patterns`; the worktree
    # decides it mechanically, the gate is never called for either
    assert [event["properties"]["permission"] for event in asked] == ["write", "bash"]
    assert asked[0]["properties"]["patterns"] == ["pkg/thing.py"]
    assert asked[1]["properties"]["metadata"]["command"] == "ls pkg"

    assert [part["tool"] for part in parts] == ["write", "bash"]
    assert parts[0]["state"]["status"] == "completed"
    assert info["tokens"]["total"] == 90


def test_the_agent_loop_rejects_a_refused_command_without_running_it(tmp_path, monkeypatch):
    """A `reject` reply is the tool's error: the command never runs."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    script = [
        _openai_reply(calls=[_openai_tool("bash", {"command": "touch /tmp/kc34-refused"})]),
        _openai_reply("gave up"),
    ]
    with FakeOpenRouter(script) as fake:
        backend = OpenRouterBackend("sk-test", fake.url, str(tmp_path), timeout=20.0)
        session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
        backend.prompt(session, "try")
        result = backend.wait_idle(
            session, 25.0,
            on_permission=lambda event: ("reject", "the reviewer refused this command"),
            on_question=lambda event: None)
    parts = backend.tool_parts(session)
    backend.close()
    assert result.status == "idle"
    assert not (tmp_path / "kc34-refused").exists()
    assert not Path("/tmp/kc34-refused").exists()
    assert parts[0]["state"]["status"] == "error"
    assert "refused" in parts[0]["state"]["output"]


def test_a_bash_call_is_decided_mechanically_not_by_the_gate(tmp_path):
    """The point of feeding the gate the Kilo event shape: a bash call inside the
    worktree costs no gate call at all, and one with an outside path does not
    get decided by geometry alone."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    config = ContestConfig(tmp_roots=("/tmp/kilo/*",))
    policy = Policy(config, completion_fn=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the gate must not be called")))
    ctx = PolicyContext(worktree=worktree, tmp_roots=("/tmp/kilo/*",),
                        forbidden=tuple(HARD_DENYLIST) + (worktree.parent,),
                        ticket_title="73-kc34.md", ticket_files=("pkg/thing.py",))

    inside = {"type": "permission.asked",
              "properties": {"id": "c-bash", "sessionID": "hy3:free", "permission": "bash",
                             "patterns": [], "metadata": {"command": "ls pkg"},
                             "tool": {"messageID": "c", "callID": "c"}}}
    decision = policy.decide(inside, ctx)
    assert decision.reply == "once" and decision.layer == "mechanical"

    outside = {"type": "permission.asked",
               "properties": {"id": "c2-bash", "sessionID": "hy3:free", "permission": "bash",
                              "patterns": [],
                              "metadata": {"command": "cp /etc/hosts /tmp/hosts"},
                              "tool": {"messageID": "c2", "callID": "c2"}}}
    calls = []
    policy_with_gate = Policy(config, completion_fn=lambda *a, **k: (
        calls.append(a) or '{"verdict": "reject", "reason": "off the worktree"}'))
    gate_decision = policy_with_gate.decide(outside, ctx)
    assert calls, "a bash call with an outside path must reach the gate"
    assert gate_decision.reply == "reject" and gate_decision.layer == "gate"


def test_an_out_of_tree_write_needs_no_permission_at_all(tmp_path, monkeypatch):
    """`read` and `write` stay inside the worktree by construction, so the loop
    refuses an escaping path itself instead of asking."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    script = [
        _openai_reply(calls=[_openai_tool("write", {"path": "../../out.py", "content": "x"})]),
        _openai_reply("done"),
    ]
    with FakeOpenRouter(script) as fake:
        backend = OpenRouterBackend("sk-test", fake.url, str(tmp_path), timeout=20.0)
        session = backend.create_session("openrouter", "hy3:free", rules=[], title="t")
        backend.prompt(session, "try")
        asked = []
        result = backend.wait_idle(
            session, 25.0,
            on_permission=lambda event: (asked.append(event) or ("once", "")),
            on_question=lambda event: None)
    parts = backend.tool_parts(session)
    backend.close()
    assert result.status == "idle"
    assert asked == [], "an escaping path is refused by the loop, not asked about"
    assert parts[0]["state"]["status"] == "error"
    assert not (tmp_path.parent / "out.py").exists()
