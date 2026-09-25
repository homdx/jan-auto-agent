"""KC-64: a provider that keeps failing ends the agent after N retries.

Round 106 (08:30 UTC) is the other half of KC-61. Two `bynara` agents got one
`session.status` and then nothing:

    {"type": "session.status", "properties": {"sessionID": "…", "status": {
      "type": "retry", "attempt": 1,
      "message": "Upstream temporarily unavailable (was malformed body: upstream
                  sent an error chunk inside a 200 stream: The model service is
                  temporarily unavailable. Please try again.). Retry after 10s.",
      "next": 1790325106062}}}

`next` is ten seconds out, so KC-61's quota check does not fire. Kilo retries
on its own, and each attempt is a `session.status busy` — which resets the
runner's silence clock. `laguna-s-2-1` reached `attempt: 51` and `nex-n2-5-pro`
`attempt: 50`, over 60 minutes, with no assistant part in either stream, and
both ended `STALLED: no idle after 60m` with the real reason nowhere in
`state.json`.

The cases here, from the ticket:

  * ten retries in a row, no assistant output in between, end the wait as
    `status="error"` with `error["name"] == "ProviderUnavailable"` and one abort;
  * a stream of 51 ends at the tenth, not the first and not the last;
  * nine do not: a provider that answers now and then never reaches the limit;
  * an assistant part in between resets the count, so 6 + 6 is not 12;
  * omitted or 0 is off, event for event;
  * the runner never sends `RETRY_PROMPT` for it, and names the end
    `provider_unavailable after 10 retries: … (can be an exhausted plan …)`;
  * `provider_retry_max_attempts` is a roster key: default 10, 0 accepted.

Nothing here calls a provider. The counting arithmetic is settled on a scripted
tap on a fake clock; only the cases that go over the fake's real HTTP and SSE
transport are on a wall clock.
"""

from __future__ import annotations

import json
import subprocess
import sys
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

import tools.contest.kilo_client as kc_module  # noqa: E402
import tools.contest.runner as runner_module  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest.kilo_client import (  # noqa: E402
    EventTap,
    IdleResult,
    KiloClient,
    KiloServer,
    SessionRef,
)
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import (  # noqa: E402
    CONTEST_KEYS,
    AgentSpec,
    ContestConfig,
    load_roster,
)
from tools.contest.runner import (  # noqa: E402
    AgentRun,
    AgentState,
    RETRY_PROMPT,
    _is_provider_unavailable,
    _quota_re,
    run_agent,
)
from tools.contest.workspace import Workspace  # noqa: E402

# This module replays a turn through the fake's real HTTP server and SSE stream,
# so it shares the xdist_group with the other port-binding suites: pytest.ini's
# --dist=loadgroup pins them to one worker, so none of them can race another
# for an OS-assigned port.
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

COMMITTED = REPO_ROOT / "contest.ini"

#: The round's own limit: ten short retries in a row is ~12 min on round 106's
#: cadence, instead of the 60 min both bynara agents sat WAITING.
LIMIT = 10

SESSION = SessionRef(id="ses_retry", provider_id="bynara", model_id="laguna-s-2-1",
                     directory="/nowhere")

#: Round 106's `laguna-s-2-1`, verbatim from events.jsonl. The plan on the
#: bynara site was used up, and the provider answered the same text for that as
#: for a real outage, so the end says where to look instead of guessing.
LAGUNA_RETRY = (
    "Upstream temporarily unavailable (was malformed body: upstream sent an error "
    "chunk inside a 200 stream: The model service is temporarily unavailable. "
    "Please try again.). Retry after 10s."
)

#: A provider text that would otherwise spend `max_error_retries`:
#: `_RETRYABLE_MSG_RE` matches `503` and `upstream unavailable`.
RETRYABLE_TEXT = "503 upstream unavailable"

RULES = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "external_directory", "pattern": "*", "action": "ask"},
    {"permission": "doom_loop", "pattern": "*", "action": "ask"},
]


def _reject(event):
    return "reject", "kc64 test"


def _ignore(event):
    del event


def _ev(etype, session_id=SESSION.id, **props):
    return {"type": etype, "properties": {"sessionID": session_id, **props}}


def _busy():
    return _ev("session.status", status={"type": "busy"})


def _retry(attempt, message=LAGUNA_RETRY, next_ms=0):
    """One of Kilo's own retries, the round-106 shape."""
    status = {"type": "retry", "message": message}
    if attempt is not None:
        status["attempt"] = attempt
    if next_ms:
        status["next"] = next_ms
    return _ev("session.status", status=status)


def _idle():
    return _ev("session.idle")


def _step_start(part_id="p_step"):
    """The first assistant-side part: Kilo's answer, in every shape on record."""
    return _ev("message.part.updated",
               part={"id": part_id, "type": "step-start"})


def _text(part_id="p_text"):
    """Our own prompt coming back: the nine `text` parts laguna never left."""
    return _ev("message.part.updated",
               part={"id": part_id, "type": "text", "text": "create hello.txt"})


def _retry_pairs(count, attempt_offset=0, attempt=None,
                 message=LAGUNA_RETRY, next_ms=0):
    """*count* busy/retry pairs, as the round-106 stream runs them."""
    events = []
    for n in range(1, count + 1):
        events.append(_busy())
        events.append(_retry(attempt_offset + n if attempt is None else attempt,
                             message=message, next_ms=next_ms))
    return events


# ─────────────────────────────────────────────────────────────────────────────
# the fake, for a turn over the real HTTP and SSE transport
# ─────────────────────────────────────────────────────────────────────────────

class _Probe:
    def __init__(self, fake, client, tap, session):
        self.fake = fake
        self.client = client
        self.tap = tap
        self.session = session


@contextmanager
def _probe(tmp_path, scenario, *,
           reply_timeout: float = _kilo_fake.REPLY_TIMEOUT_S):
    """A fake server, an attached KiloServer, a client, a tap and a session."""
    directory = str(tmp_path)
    fake = FakeKiloServer(scenario, directory=directory,
                          reply_timeout=reply_timeout).start()
    server = KiloServer.attach(fake.url)
    client = KiloClient(server, directory)
    tap = EventTap(fake.url, directory, str(tmp_path / "events.jsonl")).start()
    # a tap only receives events after its /event connection is open, on the
    # fake as on a real server — wait for it so nothing is lost.
    deadline = time.monotonic() + 30
    while not fake.subscribers and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake.subscribers, "the tap never connected"
    session = client.create_session("bynara", "laguna-s-2-1", rules=RULES,
                                    title="kc64-test")
    try:
        yield _Probe(fake, client, tap, session)
    finally:
        tap.stop()
        tap.join()
        server.close()
        fake.stop()


# ─────────────────────────────────────────────────────────────────────────────
# a scripted tap, for the counting arithmetic
# ─────────────────────────────────────────────────────────────────────────────

class _FakeClock:
    """`time.monotonic` that only moves when a test moves it."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now


class _ScriptedTap:
    """One event per step of the fake clock.

    There is no transport and no wall clock, so a starved box cannot turn an
    assertion into a timeout. Faithful to `EventTap.wait` in the way that
    matters for a wait: the cursor moves past every event looked at, and an
    event the predicate rejects is consumed without being returned.
    """

    def __init__(self, events, clock, step: float = 0.4):
        self._events = list(events)
        self._clock = clock
        self._step = float(step)
        self._next_at = clock.now + self._step

    def wait(self, pred, timeout):
        deadline = self._clock.now + max(0.0, float(timeout))
        while True:
            if not self._events or self._next_at > deadline:
                self._clock.now = deadline
                return None
            self._clock.now = self._next_at
            self._next_at = self._clock.now + self._step
            event = self._events.pop(0)
            if pred(event):
                return event


class _CountingClient(KiloClient):
    """A `KiloClient` with no transport: `abort` is counted, never sent."""

    def __init__(self):
        self.aborts: list = []

    def _abort_quietly(self, session):
        self.aborts.append(session.id)


def _wait(monkeypatch, events, *, max_retry_attempts=None, max_retry_wait=None,
          quota_re=None, timeout=10_000.0):
    """`KiloClient.wait_idle` over *events* — no transport, no real clock."""
    clock = _FakeClock()
    monkeypatch.setattr(kc_module.time, "monotonic", clock.monotonic)
    client = _CountingClient()
    result = client.wait_idle(_ScriptedTap(events, clock), SESSION, timeout,
                              on_permission=_reject, on_question=_ignore,
                              max_retry_wait=max_retry_wait, quota_re=quota_re,
                              max_retry_attempts=max_retry_attempts)
    return client, result


# ─────────────────────────────────────────────────────────────────────────────
# 1 — ten retries in a row end the wait
# ─────────────────────────────────────────────────────────────────────────────

def test_ten_retries_without_output_end_the_wait(tmp_path):
    """A turn that only retries, with `max_retry_attempts = 10`: `error`,
    `ProviderUnavailable`, the provider's text, the count, and one abort — the
    session is stopped before Kilo's next `busy` resets the silence clock."""
    with _probe(tmp_path, {"turns": [{"retries": LIMIT, "idle": False}]}) as h:
        h.client.prompt(h.session, "create hello.txt with Hello world")
        res = h.client.wait_idle(h.tap, h.session, 30.0, on_permission=_reject,
                                 on_question=_ignore, max_retry_attempts=LIMIT)

    assert res.status == "error"
    assert res.error["name"] == "ProviderUnavailable"
    assert res.error["data"]["attempts"] == LIMIT
    assert "temporarily unavailable" in res.error["data"]["message"]
    # no assistant part was ever on the stream
    assert h.fake.events_of("message.part.updated") == []
    assert h.fake.recorded_abort_for(h.session.id)


def test_retry_message_is_the_provider_text(tmp_path):
    """`retry_message` is what the fake puts on each retry and what the end
    names: the provider's own words, not a guess."""
    text = "The model service is temporarily unavailable. Please try again."
    with _probe(tmp_path, {"turns": [{"retries": LIMIT, "idle": False,
                                      "retry_message": text}]}) as h:
        h.client.prompt(h.session, "create hello.txt with Hello world")
        res = h.client.wait_idle(h.tap, h.session, 30.0, on_permission=_reject,
                                 on_question=_ignore, max_retry_attempts=LIMIT)

    assert res.status == "error"
    assert res.error["data"]["message"] == text


def test_a_stream_of_fifty_one_retries_ends_at_the_tenth(monkeypatch):
    """Round 106's `laguna-s-2-1` ran to `attempt: 51`. The limit is crossed on
    the tenth retry, not the first and not the last, and the turn ends there."""
    client, res = _wait(monkeypatch, _retry_pairs(51), max_retry_attempts=LIMIT)

    assert res.status == "error"
    assert res.error == {"name": "ProviderUnavailable",
                         "data": {"message": LAGUNA_RETRY, "attempts": LIMIT}}
    assert client.aborts == [SESSION.id]


def test_nine_retries_do_not(tmp_path):
    """One below the limit, then the answer: the wait ends `idle` and no abort
    is sent. A provider that answers now and then must never reach the limit."""
    with _probe(tmp_path, {"turns": [{"retries": LIMIT - 1, "events": ["busy", "idle"],
                                      "assistant": "created hello.txt"}]}) as h:
        h.client.prompt(h.session, "create hello.txt with Hello world")
        res = h.client.wait_idle(h.tap, h.session, 30.0, on_permission=_reject,
                                 on_question=_ignore, max_retry_attempts=LIMIT)

    assert res.status == "idle"
    assert res.error is None
    assert not h.fake.recorded_abort_for(h.session.id)


def test_output_between_retries_resets_the_count(monkeypatch):
    """Six retries, one assistant part, six more: 6 + 6 is not 12. `min()` means
    both Kilo's counter and the local one have to cross the limit, and the part
    in between resets the local one — so this ends `idle`."""
    events = _retry_pairs(6) + [_step_start()] + _retry_pairs(6, attempt_offset=6) \
        + [_busy(), _idle()]
    client, res = _wait(monkeypatch, events, max_retry_attempts=LIMIT)

    assert res.status == "idle"
    assert res.error is None
    assert client.aborts == []


def test_a_retry_without_an_attempt_is_counted_locally(monkeypatch):
    """The second guard: a Kilo build that sends no `attempt` at all. The local
    counter is what ends the turn, and it carries the count back."""
    client, res = _wait(monkeypatch, _retry_pairs(LIMIT, attempt=None),
                        max_retry_attempts=LIMIT)

    assert res.status == "error"
    assert res.error["name"] == "ProviderUnavailable"
    assert res.error["data"]["attempts"] == LIMIT
    assert client.aborts == [SESSION.id]


def test_an_attempt_from_an_earlier_wait_does_not_end_a_new_one(monkeypatch):
    """The first guard: `attempt` is Kilo's, and it survives across turns. Nine
    retries in one wait, then a new wait that sees ten, eleven and twelve — the
    local count is one, two, three, so the limit is never crossed twice over."""
    client, first = _wait(monkeypatch, _retry_pairs(9), max_retry_attempts=LIMIT)
    assert first.status == "timeout"

    client, second = _wait(monkeypatch, _retry_pairs(3, attempt_offset=9) + [_idle()],
                           max_retry_attempts=LIMIT)
    assert second.status == "idle"
    assert client.aborts == []


def test_a_text_part_does_not_reset_the_count(monkeypatch):
    """Our own prompt comes back as `text` parts, not a `step-start`: laguna
    had nine of them and zero tokens out. Counting one of them as an answer
    would reset the counter forever on this provider."""
    events = []
    for n in range(1, LIMIT + 1):
        events.append(_busy())
        events.append(_text(part_id=f"p{n}"))
        events.append(_retry(n))
    client, res = _wait(monkeypatch, events, max_retry_attempts=LIMIT)

    assert res.status == "error"
    assert res.error["name"] == "ProviderUnavailable"
    assert res.error["data"]["attempts"] == LIMIT
    assert client.aborts == [SESSION.id]


def test_a_malformed_part_or_status_is_a_beat_not_a_reset(monkeypatch):
    """Fail-open: a `part` that is not a dict and a `status` that is not one
    neither reset nor count — nine retries and three malformed events end
    `idle`. Without the `isinstance` guards this raises into a round."""
    bad_part = _ev("message.part.updated", part=["not", "a", "dict"])
    no_part = _ev("message.part.updated")
    not_a_dict = _ev("session.status", status="retry")
    events = (_retry_pairs(7) + [bad_part, no_part, not_a_dict, _retry(8)]
              + _retry_pairs(1, attempt_offset=8) + [_busy(), _idle()])
    client, res = _wait(monkeypatch, events, max_retry_attempts=LIMIT)

    assert res.status == "idle"
    assert res.error is None
    assert client.aborts == []


def test_a_neighbour_sessions_retries_do_not_count(monkeypatch):
    """The tap reads every session of the directory: a neighbour's ten retries
    are consumed without ending this wait, and this session's own idle is the
    end."""
    neighbour = _ev("session.status", session_id="ses_someone_else",
                    status={"type": "retry", "attempt": LIMIT,
                            "message": LAGUNA_RETRY})
    events = [neighbour for _ in range(LIMIT)] + [_idle()]
    client, res = _wait(monkeypatch, events, max_retry_attempts=LIMIT)

    assert res.status == "idle"
    assert client.aborts == []


def test_the_quota_check_runs_first(monkeypatch):
    """KC-61's quota check and this one read the same event. A retry scheduled
    fourteen hours out ends at `attempt: 1` as `ProviderQuota` — a daily quota
    must not wait for the tenth retry to be named."""
    far = int((time.time() + 14 * 3600) * 1000)
    patterns = _quota_re(ContestConfig(quota_patterns="temporarily unavailable"))
    client, res = _wait(monkeypatch, _retry_pairs(LIMIT, next_ms=far),
                        max_retry_attempts=LIMIT, max_retry_wait=300.0,
                        quota_re=patterns)

    assert res.status == "error"
    assert res.error == {"name": "ProviderQuota",
                         "data": {"message": LAGUNA_RETRY, "retryAt": far}}
    assert client.aborts == [SESSION.id]


def test_a_retry_inside_the_bound_is_a_beat(monkeypatch):
    """Ten retries, each ten seconds out: KC-61 does not fire and neither does
    KC-61's phrase, so this one names the provider after ten — not the first."""
    far = int((time.time() + 10) * 1000)
    client, res = _wait(monkeypatch, _retry_pairs(LIMIT, next_ms=far),
                        max_retry_attempts=LIMIT, max_retry_wait=300.0)

    assert res.status == "error"
    assert res.error["name"] == "ProviderUnavailable"
    assert res.error["data"]["attempts"] == LIMIT
    assert client.aborts == [SESSION.id]


# ─────────────────────────────────────────────────────────────────────────────
# 2 — off by default
# ─────────────────────────────────────────────────────────────────────────────

def test_off_by_default(tmp_path):
    """`max_retry_attempts` not armed: twelve retries are twelve beats, exactly
    as `session.status busy` is, and the turn ends at its deadline."""
    with _probe(tmp_path, {"turns": [{"retries": 12, "idle": False}]}) as h:
        h.client.prompt(h.session, "create hello.txt with Hello world")
        res = h.client.wait_idle(h.tap, h.session, 3.0, on_permission=_reject,
                                 on_question=_ignore)

    # the fixture really did run: twelve pairs, and no assistant output
    assert len(h.fake.events_of("session.status")) == 24
    assert h.fake.events_of("message.part.updated") == []
    assert res.status == "timeout"
    assert res.error is None


def test_a_limit_of_zero_arms_nothing():
    """`provider_retry_max_attempts = 0` is today's behaviour: the kwarg is not
    passed at all, and the stall edge is left alone."""
    cfg = _config(provider_retry_max_attempts=0)
    backend = _WaitSpy(IdleResult(status="idle", elapsed=0.0))
    runner_module._wait_turn(backend, SESSION, cfg, on_permission=_reject,
                             on_question=_ignore)

    assert "max_retry_attempts" not in backend.kwargs
    assert backend.kwargs["idle_event_timeout"] == 900


def test_a_negative_limit_arms_nothing():
    """A typo that would count retries forever is no limit."""
    cfg = _config(provider_retry_max_attempts=-3)
    backend = _WaitSpy(IdleResult(status="idle", elapsed=0.0))
    runner_module._wait_turn(backend, SESSION, cfg, on_permission=_reject,
                             on_question=_ignore)

    assert "max_retry_attempts" not in backend.kwargs


# ─────────────────────────────────────────────────────────────────────────────
# 3 — the runner
# ─────────────────────────────────────────────────────────────────────────────

class _WaitSpy:
    """`ContestBackend` with one scripted answer and every call recorded."""

    def __init__(self, result):
        self.result = result
        self.prompts: list = []
        self.aborts: list = []
        self.kwargs: dict = {}

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title,
                       agent=None, variant=None):
        return SessionRef(id="ses_retry", provider_id=provider_id, model_id=model_id,
                          directory="/nowhere", agent=agent, variant=variant)

    def prompt(self, session, text):
        self.prompts.append(text)

    def mark(self):
        return None

    def wait_idle(self, session, timeout, **kwargs):
        self.kwargs = kwargs
        return self.result

    def abort(self, session):
        self.aborts.append(session.id)

    def interrupt(self, session=None):
        pass

    def interrupted(self):
        return False

    def close(self):
        pass

    def session_info(self, session):
        return {}

    def messages(self, session):
        return []

    def tool_parts(self, session):
        return []


def _config(**over):
    """Round 106's limits, as the runner sees them, with this rule on."""
    kwargs = dict(
        agents=(AgentSpec(name="laguna", provider_id="bynara",
                          model_id="laguna-s-2-1"),),
        turn_timeout_sec=300,
        turn_extend_sec=0,
        idle_event_timeout_sec=900,
        max_error_retries=2,
        error_retry_backoff_sec=0,
        provider_retry_max_attempts=10,
        provider_retry_max_wait_sec=300.0,
        quota_patterns="",
        gate_settings=LlmSettings(base_url="", api_key="", model="",
                                  api_format="openai", response_format=True,
                                  temperature=0.0, max_tokens=256),
    )
    kwargs.update(over)
    return ContestConfig(**kwargs)


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _repo(tmp_path):
    """One base commit and nothing above it: a clean tree, no KC-21 harvest."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "pkg").mkdir()
    (root / "pkg" / "thing.py").write_text("def thing():\n    return 1\n",
                                           encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return Workspace(agent="laguna", path=root.resolve(), branch="contest/111/laguna",
                     base_sha=_git(root, "rev-parse", "HEAD"), kind="clone")


def _ticket(tmp_path):
    path = tmp_path / "epic-tasks" / "111-kc64-test.md"
    path.parent.mkdir(parents=True)
    path.write_text("# KC-64 test ticket\n\n**File:** `pkg/thing.py`\n\nbody\n",
                    encoding="utf-8")
    return path


def _run(tmp_path, result, cfg=None):
    """One agent through `run_agent`, against a backend that answers *result*."""
    ws = _repo(tmp_path)
    config = cfg or _config()
    backend = _WaitSpy(result=result)
    run = AgentRun(agent=config.agents[0], workspace=ws)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_agent(run, backend=backend, policy=Policy(config), config=config,
              ticket_path=_ticket(tmp_path), out_dir=out_dir,
              on_transition=lambda r: None)
    return run, backend, out_dir


def _unavailable(attempts=LIMIT, message=LAGUNA_RETRY):
    return IdleResult(status="error", elapsed=12.0,
                      error={"name": "ProviderUnavailable",
                             "data": {"message": message, "attempts": attempts}})


def test_the_runner_does_not_retry_provider_unavailable(tmp_path):
    """The agent's first turn fails ten retries in a row: `ERROR`, one turn,
    and no `RETRY_PROMPT` — Kilo already spent the attempts."""
    run, backend, out_dir = _run(tmp_path, _unavailable())

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_unavailable after 10 retries:"), \
        run.last_error
    assert "temporarily unavailable" in run.last_error, run.last_error
    assert run.last_error.endswith(
        "(can be an exhausted plan — check the provider's site)"), run.last_error
    assert len(run.turns) == 1
    assert [t["kind"] for t in run.turns] == ["initial"]
    assert [t["idle_status"] for t in run.turns] == ["error"]
    # one prompt, and it is the round prompt, not RETRY_PROMPT
    assert len(backend.prompts) == 1
    assert RETRY_PROMPT.split("{reason}")[0] not in backend.prompts[0]
    # and exactly one line on disk, the initial turn — no `retry` turn
    turns = (out_dir / "laguna" / "turns.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(turns) == 1
    assert json.loads(turns[0])["kind"] == "initial"
    assert json.loads(turns[0])["idle_status"] == "error"
    # the runner armed the limit and left the stall edge alone
    assert backend.kwargs["max_retry_attempts"] == 10
    assert backend.kwargs["idle_event_timeout"] == 900
    assert run.commit is None


def test_a_retryable_shaped_provider_unavailable_is_still_not_retried(tmp_path):
    """`_RETRYABLE_MSG_RE` matches `503` and `upstream unavailable`, so the
    provider's text here would otherwise spend `max_error_retries = 2` on top of
    the ten Kilo already spent."""
    run, backend, out_dir = _run(tmp_path, _unavailable(message=RETRYABLE_TEXT))

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_unavailable after 10 retries:"), \
        run.last_error
    assert len(backend.prompts) == 1
    assert len(run.turns) == 1
    assert len((out_dir / "laguna" / "turns.jsonl")
               .read_text(encoding="utf-8").strip().splitlines()) == 1


def test_the_retry_count_in_the_message_comes_off_the_payload(tmp_path):
    """The message names the count it saw, however far past the limit Kilo got."""
    run, _backend, _out = _run(tmp_path, _unavailable(attempts=51, message="Server is busy"))

    assert run.last_error.startswith("provider_unavailable after 51 retries:"), \
        run.last_error
    assert run.last_error.endswith("(can be an exhausted plan — check the provider's site)")


def test_the_helper_reads_only_the_name():
    """Fail-open: only the payload `wait_idle` builds is one, by name."""
    assert _is_provider_unavailable({"name": "ProviderUnavailable", "data": {}})
    assert _is_provider_unavailable({"name": "ProviderUnavailable"})
    assert not _is_provider_unavailable({"name": "ProviderQuota", "data": {}})
    assert not _is_provider_unavailable("ProviderUnavailable")
    assert not _is_provider_unavailable("provider_unavailable after 10 retries:")
    assert not _is_provider_unavailable({"data": {"attempts": 10}})
    assert not _is_provider_unavailable({})
    assert not _is_provider_unavailable(None)


def test_the_plan_hint_is_not_cut_with_the_provider_text(tmp_path):
    """`_brief` cuts at 300 characters; the hint goes after the cut, so a long
    provider message cannot push it out of the summary."""
    run, _backend, _out = _run(tmp_path, _unavailable(
        message="The model service is temporarily unavailable. " * 20))

    assert "temporarily unavailable" in run.last_error
    assert run.last_error.endswith("(can be an exhausted plan — check the provider's site)")
    assert len(run.last_error) < 420, run.last_error


# ─────────────────────────────────────────────────────────────────────────────
# 4 — the roster key
# ─────────────────────────────────────────────────────────────────────────────

def test_the_keys_are_exported():
    """The key is in `CONTEST_KEYS` and on `ContestConfig`, default 10."""
    assert "provider_retry_max_attempts" in CONTEST_KEYS
    cfg = ContestConfig()
    assert cfg.provider_retry_max_attempts == 10


def _write_ini(tmp_path, extra):
    ini = tmp_path / "contest.ini"
    ini.write_text(
        "[contest]\n"
        + extra
        + "gate_llm_profile = gate\n"
        "[gate]\n"
        "base_url = https://example.invalid/v1\n"
        "api_key = test-key\n"
        "model = test/model\n"
        "[contest.agent.laguna]\n"
        "model = bynara/laguna-s-2-1\n",
        encoding="utf-8")
    return ini


def test_the_roster_reads_provider_retry_max_attempts(tmp_path):
    """Default 10 when the key is absent, 0 accepted as off, and a value from
    the roster read back."""
    assert load_roster(_write_ini(tmp_path, "")).provider_retry_max_attempts == 10
    cfg = load_roster(_write_ini(tmp_path, "provider_retry_max_attempts = 0\n"))
    assert cfg.provider_retry_max_attempts == 0
    cfg = load_roster(_write_ini(tmp_path, "provider_retry_max_attempts = 25\n"))
    assert cfg.provider_retry_max_attempts == 25
    # a typo falls back to the default, like every other limit
    cfg = load_roster(_write_ini(tmp_path, "provider_retry_max_attempts = ten\n"))
    assert cfg.provider_retry_max_attempts == 10


def test_the_committed_default_is_ten(monkeypatch):
    """The committed contest.ini carries the limit, on."""
    monkeypatch.setenv("CONTEST_GATE_API_KEY", "test-gate-key")
    cfg = load_roster(COMMITTED)
    assert cfg.provider_retry_max_attempts == 10
