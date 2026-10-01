"""KC-61: a provider out of quota ends the agent at once.

Round 107 (09:22:59–09:23:00 UTC) and round 106 (the same day) show the same
event. Five `kenary` agents on one free key each got one `session.status`, then
nothing:

    {"type": "session.status", "properties": {"sessionID": "…", "status": {
      "type": "retry", "attempt": 1,
      "message": "free-model daily limit reached; the counter resets at 00:00
                  UTC. Top up from Rp1.000 at …",
      "next": 1790380800370}}}

`next` is 2026-09-26 00:00 UTC — 14 h 37 min after the event, in epoch
milliseconds. Kilo sleeps until then and sends nothing more. Before this change
the runner had no branch for a `session.status` of `type: retry`, so it waited
out `idle_event_timeout_sec` (900) and ended `STALLED: no event for 900s`:
fifteen minutes of a wrong view, with the real reason only in `events.jsonl`.

Every case here is settled over a scripted tap or a stub backend, so nothing
runs on a wall clock or a provider:

  * a retry further out than `provider_retry_max_wait_sec` ends the wait as
    `status="error"` with `error["name"] == "ProviderQuota"`, and the session
    is aborted first;
  * a retry inside the bound is a beat, exactly as a `session.status busy` is;
  * with no `next`, the provider's text decides, and a transient text — the
    bynara 503 that also means an exhausted plan — is not a quota;
  * the runner names the state `provider_quota:` with the reset time, before
    `_RETRYABLE_MSG_RE` gets to spend `max_error_retries` against the 429;
  * `quota_patterns` in a roster replaces the default list, and an empty or
    absent key is "no quota phrases", which is today's behaviour and never a
    `re.error`;
  * intake asks about a named variant, so an exhausted key is seen before the
    round instead of in the middle of it.

Nothing in the runner names a provider: every phrase a round matches lives in
the config defaults, and this file asserts that.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tools.contest.cli as cli  # noqa: E402
import tools.contest.kilo_client as kc  # noqa: E402
import tools.contest.runner as runner_module  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest.cli import agents_from_models, resolve_variants  # noqa: E402
from tools.contest.kilo_client import IdleResult, KiloClient, SessionRef  # noqa: E402
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
    RoundState,
    _is_quota,
    _quota_re,
    run_agent,
)
from tools.contest.workspace import Workspace  # noqa: E402

COMMITTED = REPO_ROOT / "contest.ini"

#: What round 107's five kenary agents got, as Kilo recorded it in events.jsonl.
QUOTA_MESSAGE = (
    "free-model daily limit reached; the counter resets at 00:00 UTC. "
    "Top up from Rp1.000 at https://kenari.id/pay"
)
#: `1790380800370` — the reset Kilo named: 2026-09-26 00:00:00.370 UTC.
RETRY_AT_MS = 1_790_380_800_370
UTC_AT_RESET = time.strftime("%Y-%m-%d %H:%M", time.gmtime(RETRY_AT_MS / 1000))

#: The committed `quota_patterns`, phrase by phrase. Every phrase a round
#: matches lives in contest.ini, and this is where the runner's is asserted.
QUOTA_PHRASES = (
    "daily limit",
    "limit reached",
    "quota",
    "insufficient_quota",
    "RESOURCE_EXHAUSTED",
    "free-models-per-day",
    "exceeded your current quota",
    "insufficient credits",
    "would exceed your available credits",
    "top up",
    "billing",
)
QUOTA_DEFAULT = " | ".join(QUOTA_PHRASES)

#: Every provider text on record as a quota, provider by provider (the ticket's
#: table), matched against the committed default list.
QUOTA_TEXTS = (
    QUOTA_MESSAGE,
    "Rate limit exceeded: free-models-per-day",
    "This request would exceed your available credits. Add credits or upgrade.",
    "RESOURCE_EXHAUSTED",
    "Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests",
    "insufficient_quota",
    "You exceeded your current quota. Please check your plan and billing details.",
)

#: What is not a quota. The bynara 503 first: on 2026-09-25 it was also what an
#: exhausted plan looked like, so it cannot be a quota signal. The rest are
#: KC-19's transients, which keep their own retry path.
NOT_QUOTA_TEXTS = (
    "The model service is temporarily unavailable. Please try again.",
    "Server is busy",
    "interrupted the response before it finished",
    "429 Too Many Requests",
)

_OFFER = {
    "all": [
        {
            "id": "kenary",
            "models": {
                "hy3:free": {"variants": {"low": {}, "medium": {}, "high": {}}},
                "agnes-2-5-flash:free": {"variants": {"low": {}, "medium": {}, "high": {}}},
            },
        }
    ],
    "connected": ["kenary"],
}

SESSION = SessionRef(id="ses_quota", provider_id="p", model_id="m",
                     directory="/nowhere")


def _reject(event):
    return "reject", "quota test"


def _ignore(event):
    del event


# ── the round-107 event, as a scripted stream ─────────────────────────────────

def _quota_event(next_ms=None, message=QUOTA_MESSAGE, session="ses_quota"):
    """The round-107 line, with `next` rewritten off the fake clock."""
    status = {"type": "retry", "attempt": 1, "message": message}
    if next_ms is not None:
        status["next"] = next_ms
    return {"type": "session.status",
            "properties": {"sessionID": session, "status": status}}


def _busy():
    return {"type": "session.status",
            "properties": {"sessionID": "ses_quota", "status": {"type": "busy"}}}


def _idle():
    return {"type": "session.idle", "properties": {"sessionID": "ses_quota"}}


class _Clock:
    """Two faces of the time module: the wait's `monotonic`, and the wall
    clock a provider's epoch milliseconds are compared against."""

    def __init__(self, wall=None, monotonic=1000.0):
        self.wall = float(time.time()) if wall is None else float(wall)
        self.monotonic_now = float(monotonic)

    def monotonic(self):
        return self.monotonic_now

    def time(self):
        return self.wall

    def advance(self, seconds):
        """One step of the wait: both clocks move, by exactly the step."""
        self.monotonic_now += float(seconds)
        self.wall += float(seconds)


class _ScriptedTap:
    """One turn's event stream, one event at a time, on the fake clock.

    Faithful to `EventTap.wait` in the way that matters for a stall: the cursor
    moves past every event looked at, and an event the predicate rejects is
    consumed without being returned. Nothing here waits on a transport, so a
    loaded box cannot turn an assertion into a timeout.
    """

    def __init__(self, events, clock):
        self._events = list(events)
        self._clock = clock
        self._next_at = clock.monotonic_now + 0.4

    def wait(self, pred, timeout):
        deadline = self._clock.monotonic_now + max(0.0, float(timeout))
        while True:
            if not self._events or self._next_at > deadline:
                self._clock.advance(deadline - self._clock.monotonic_now)
                return None
            self._clock.advance(self._next_at - self._clock.monotonic_now)
            self._next_at = self._clock.monotonic_now + 0.4
            event = self._events.pop(0)
            if pred(event):
                return event


class _CountingClient(KiloClient):
    """A `KiloClient` with no transport: `abort` is counted, never sent."""

    def __init__(self):
        self.aborts = []

    def _abort_quietly(self, session):
        self.aborts.append(session.id)


def _wait(monkeypatch, events, *, max_retry_wait=None, quota_re=None,
          timeout=10_000.0, wall=None):
    """`KiloClient.wait_idle` over *events* — no transport, no real clock."""
    clock = _Clock(wall=wall)
    monkeypatch.setattr(kc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kc.time, "time", clock.time)
    client = _CountingClient()
    tap = _ScriptedTap(events, clock)
    res = client.wait_idle(tap, SESSION, timeout, on_permission=_reject,
                           on_question=_ignore, max_retry_wait=max_retry_wait,
                           quota_re=quota_re)
    return client, res


def _config_with_patterns(patterns=QUOTA_DEFAULT):
    return ContestConfig(quota_patterns=patterns)


# ── wait_idle: a retry past the bound is a quota ──────────────────────────────

def test_a_retry_past_the_bound_ends_the_wait_as_provider_quota(monkeypatch):
    """The round-107 event, `next` 14 h out: `error`, `ProviderQuota`, one abort."""
    clock = _Clock()
    next_ms = int((clock.wall + 14 * 3600) * 1000)
    client, res = _wait(monkeypatch, [_quota_event(next_ms=next_ms)],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))

    assert res.status == "error"
    assert res.error == {"name": "ProviderQuota",
                         "data": {"message": QUOTA_MESSAGE, "retryAt": next_ms}}
    assert res.permissions == [] and res.questions == []
    # caught by the event, not by the silence clock: 0.4 s of stream, not 900 s
    assert res.elapsed < 1.0
    assert client.aborts == ["ses_quota"]     # Kilo's sleep-and-retry is stopped


def test_a_retry_inside_the_bound_keeps_the_wait_going(monkeypatch):
    """`next` 20 s out is a blip: the wait survives it and still reaches idle.

    The message is the quota wording, so this also says the bound decides
    before the phrases do.
    """
    clock = _Clock()
    next_ms = int((clock.wall + 20) * 1000)
    client, res = _wait(monkeypatch, [_busy(), _quota_event(next_ms=next_ms), _idle()],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))

    assert res.status == "idle", res
    assert client.aborts == []
    assert res.elapsed > 0.8          # three events apart, nothing was cut short


def test_a_retry_without_a_time_is_judged_by_the_phrases(monkeypatch):
    """Kilo names no reset: the provider's text decides, and `retryAt` is `None`."""
    client, res = _wait(monkeypatch, [_busy(), _quota_event(next_ms=None,
                                                             message=NOT_QUOTA_TEXTS[1]),
                                      _idle()],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))
    assert res.status == "idle", res
    assert client.aborts == []

    client, res = _wait(monkeypatch, [_busy(), _quota_event(next_ms=None,
                                                             message="insufficient_quota")],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))
    assert res.status == "error"
    assert res.error["name"] == "ProviderQuota"
    assert res.error["data"] == {"message": "insufficient_quota", "retryAt": None}
    assert client.aborts == ["ses_quota"]


def test_a_next_that_is_not_a_number_falls_back_to_the_phrases(monkeypatch):
    """`next` is not an integer, so the provider's text decides instead."""
    client, res = _wait(monkeypatch, [_quota_event(next_ms="soon",
                                                   message="insufficient_quota")],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))
    assert res.status == "error"
    assert res.error == {"name": "ProviderQuota",
                         "data": {"message": "insufficient_quota", "retryAt": "soon"}}


def test_a_retry_without_a_time_and_no_phrases_is_not_a_quota(monkeypatch):
    """No `next` and no `quota_re`: a transient 503 is a beat, never a quota."""
    client, res = _wait(monkeypatch, [_busy(), _quota_event(next_ms=None,
                                                             message=NOT_QUOTA_TEXTS[0]),
                                      _idle()],
                        max_retry_wait=300.0, quota_re=None)
    assert res.status == "idle", res
    assert client.aborts == []


@pytest.mark.parametrize("status", [
    {"type": "busy"},
    {"type": "retry"},                                        # no `next`, no message
    {"type": "retry", "next": None, "message": "Server is busy"},
    {"type": "retry", "next": "soon", "message": "Server is busy"},
    {"type": "retry", "next": {"at": "midnight"}, "message": "Server is busy"},
    "busy",
])
def test_a_retry_the_bound_cannot_reach_is_not_a_quota(monkeypatch, status):
    """A `status` that is not the shape Kilo sends is a beat, and a `next` that
    is not a number is judged by the phrases alone. None of these is a quota."""
    event = {"type": "session.status",
             "properties": {"sessionID": "ses_quota", "status": status}}
    client, res = _wait(monkeypatch, [_busy(), event, _idle()],
                        max_retry_wait=300.0,
                        quota_re=_quota_re(_config_with_patterns()))
    assert res.status == "idle", (status, res)
    assert client.aborts == []


def test_a_retry_is_invisible_without_a_bound_or_phrases(monkeypatch):
    """Both new keys `None` is every pre-KC-61 caller, so the wait is unchanged.

    This is the round-107 behaviour the ticket fixes at intake: a named variant
    that was never asked about burned its whole timeout on a key that was empty
    until midnight. It is also what `provider_retry_max_wait_sec = 0` buys.
    """
    client, res = _wait(monkeypatch, [_quota_event(next_ms=RETRY_AT_MS)],
                        max_retry_wait=None, quota_re=None, timeout=3.0)
    assert res.status == "timeout"
    assert res.error is None
    assert res.elapsed == pytest.approx(3.0)
    assert client.aborts == ["ses_quota"]


def test_the_bound_is_measured_on_the_wall_clock(monkeypatch):
    """`next` is epoch ms, so `time.time` decides, not the wait's own `monotonic`.

    Here the two are ten hours apart: a `next` 400 s past `monotonic` is
    centuries in the past in wall time. Measured on the wait's clock it would be
    a quota; on the wall clock it is not one.
    """
    clock = _Clock(wall=1_790_380_000.0, monotonic=1000.0)
    next_ms = int((clock.monotonic_now + 400) * 1000)
    monkeypatch.setattr(kc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kc.time, "time", clock.time)
    client = _CountingClient()
    tap = _ScriptedTap([_quota_event(next_ms=next_ms), _idle()], clock)
    res = client.wait_idle(tap, SESSION, 10_000.0, on_permission=_reject,
                           on_question=_ignore, max_retry_wait=300.0, quota_re=None)
    assert res.status == "idle", res
    assert client.aborts == []


def test_the_bound_fires_on_the_wall_clock_too(monkeypatch):
    """And the other way: `next` 400 s past the wall clock is a quota."""
    clock = _Clock(wall=1_790_380_000.0, monotonic=1000.0)
    next_ms = int((clock.wall + 400) * 1000)
    monkeypatch.setattr(kc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kc.time, "time", clock.time)
    client = _CountingClient()
    tap = _ScriptedTap([_quota_event(next_ms=next_ms)], clock)
    res = client.wait_idle(tap, SESSION, 10_000.0, on_permission=_reject,
                           on_question=_ignore, max_retry_wait=300.0, quota_re=None)
    assert res.status == "error"
    assert res.error["name"] == "ProviderQuota"
    assert res.error["data"]["retryAt"] == next_ms


# ── the phrases: these are quotas, those are not ──────────────────────────────

@pytest.mark.parametrize("text", QUOTA_TEXTS)
def test_every_provider_quota_text_is_a_quota(text):
    """The ticket's table, provider by provider: each wording is a quota."""
    patterns = _quota_re(_config_with_patterns())
    assert patterns is not None
    assert _is_quota({"name": "APIError", "data": {"message": text}}, patterns)
    assert _is_quota(text, patterns)        # the intake probe answers with text


@pytest.mark.parametrize("text", NOT_QUOTA_TEXTS)
def test_transient_texts_are_not_quotas(text):
    """Bynara's exhausted plan is also a real 503; KC-19's transients retry."""
    patterns = _quota_re(_config_with_patterns())
    assert not _is_quota({"name": "APIError", "data": {"message": text}}, patterns)
    assert not _is_quota(text, patterns)


def test_the_provider_quota_shape_needs_no_phrase_at_all():
    """`wait_idle` names the shape; the phrases are for the other providers."""
    assert _is_quota({"name": "ProviderQuota",
                      "data": {"message": "Server is busy",
                               "retryAt": RETRY_AT_MS}}, None)


def test_the_phrases_are_literals_and_case_insensitive():
    """`re.escape`d literals, so a phrase is a phrase and nothing more."""
    patterns = _quota_re(_config_with_patterns(
        "would exceed your available credits|insufficient_quota"))
    assert _is_quota("Would Exceed Your Available Credits", patterns)
    assert _is_quota("insufficient_QUOTA", patterns)
    assert not _is_quota("You exceeded your current quota", patterns)
    assert not _is_quota("rate limit exceeded", patterns)


@pytest.mark.parametrize("raw", ["", "   ", "| | "])
def test_an_empty_or_malformed_phrase_list_is_no_phrases(raw):
    """Fail-open: an empty key is today's behaviour, never a `re.error`."""
    assert _quota_re(_config_with_patterns(raw)) is None


def test_a_config_without_the_key_is_no_phrases():
    """A config that predates the key degrades to "no quota phrases", not an error."""
    class _OldConfig:
        pass

    assert _quota_re(_OldConfig()) is None
    assert _quota_re(None) is None
    assert not _is_quota("insufficient_quota", _quota_re(_OldConfig()))


# ── the runner: a quota ends the agent, not the silence clock ─────────────────

class _WaitSpy:
    """A `ContestBackend` that records one `wait_idle` and answers the same way."""

    def __init__(self, result):
        self.kwargs = None
        self.result = result
        self.prompts = []
        self.aborts = []

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title,
                       agent=None, variant=None):
        return SessionRef(id="ses_quota", provider_id=provider_id, model_id=model_id,
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
    """Round 107's limits, as the runner sees them, with the quota rule on."""
    kwargs = dict(
        agents=(AgentSpec(name="hy3", provider_id="kenary", model_id="hy3:free"),),
        turn_timeout_sec=300,
        turn_extend_sec=0,
        idle_event_timeout_sec=900,
        max_error_retries=2,
        error_retry_backoff_sec=0,
        provider_retry_max_wait_sec=300.0,
        quota_patterns=QUOTA_DEFAULT,
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
    return Workspace(agent="hy3", path=root.resolve(), branch="contest/108/hy3",
                     base_sha=_git(root, "rev-parse", "HEAD"), kind="clone")


def _ticket(tmp_path):
    path = tmp_path / "epic-tasks" / "108-kc61-test.md"
    path.parent.mkdir(parents=True)
    path.write_text("# KC-61 test ticket\n\n**File:** `pkg/thing.py`\n\nbody\n",
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
    return run, backend


def _quota_result(message=QUOTA_MESSAGE, retry_at=RETRY_AT_MS):
    return IdleResult(status="error", elapsed=2.0,
                      error={"name": "ProviderQuota",
                             "data": {"message": message, "retryAt": retry_at}})


def test_a_scheduled_retry_ends_the_agent_as_provider_quota(tmp_path):
    """Acceptance 1: `ERROR` within seconds, with the reset, and no retries."""
    run, backend = _run(tmp_path, _quota_result())

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_quota:"), run.last_error
    assert UTC_AT_RESET in run.last_error, run.last_error   # "2026-09-26 00:00 UTC"
    assert "free-model daily limit reached" in run.last_error
    assert "(retry at" in run.last_error
    assert len(run.turns) == 1
    assert [t["kind"] for t in run.turns] == ["initial"]
    assert [t["idle_status"] for t in run.turns] == ["error"]
    # no retry: one prompt, and it is the round prompt, not RETRY_PROMPT
    assert len(backend.prompts) == 1
    assert RETRY_PROMPT.split("{reason}")[0] not in backend.prompts[0]
    assert run.commit is None
    assert backend.aborts == []
    # the runner wired the bound and the phrases through, and left the stall edge
    assert backend.kwargs["max_retry_wait"] == 300.0
    assert backend.kwargs["quota_re"] is not None
    assert backend.kwargs["idle_event_timeout"] == 900


def test_a_quota_session_error_ends_the_agent_the_same_way(tmp_path):
    """Acceptance 3: a provider that reports a quota as a plain `session.error`.

    Kilo names no reset time, so there is no `(retry at …)` — but the reason is
    still the quota, not `after 2 retries: session.error:`.
    """
    result = IdleResult(status="error", elapsed=2.0,
                        error={"name": "APIError", "data": {
                            "message": "Rate limit exceeded: free-models-per-day",
                            "isRetryable": True, "statusCode": 429}})
    run, backend = _run(tmp_path, result)

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_quota:"), run.last_error
    assert "free-models-per-day" in run.last_error
    assert "retry at" not in run.last_error
    assert len(backend.prompts) == 1
    assert RETRY_PROMPT.split("{reason}")[0] not in backend.prompts[0]


def test_a_provider_quota_with_no_reset_names_no_time(tmp_path):
    """`retryAt` missing: the phrase alone carries the reason."""
    run, _ = _run(tmp_path, _quota_result(message="insufficient_quota", retry_at=None))
    assert run.state is AgentState.ERROR
    assert run.last_error == "provider_quota: insufficient_quota"


def test_a_provider_quota_with_no_message_still_names_the_reset(tmp_path):
    """The shape decides: an empty message and no phrases still end the agent."""
    run, _ = _run(tmp_path, _quota_result(message=""))
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_quota:"), run.last_error
    assert UTC_AT_RESET in run.last_error


def test_a_transient_error_still_retries(tmp_path):
    """The quota check comes first and must not swallow KC-19's retry path."""
    result = IdleResult(status="error", elapsed=2.0,
                        error={"name": "APIError", "data": {
                            "message": "ECONNRESET", "isRetryable": True}})
    run, backend = _run(tmp_path, result, cfg=_config(provider_retry_max_wait_sec=0))

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 2 retries:"), run.last_error
    assert not run.last_error.startswith("provider_quota:")
    assert len(backend.prompts) == 3          # the initial plus two retries


def test_a_bound_of_zero_arms_nothing(tmp_path):
    """`provider_retry_max_wait_sec = 0` is off: the kwarg is not passed at all."""
    ws = _repo(tmp_path)
    cfg = _config(provider_retry_max_wait_sec=0)
    backend = _WaitSpy(IdleResult(status="idle", elapsed=0.0))
    runner_module._wait_turn(backend, SESSION, cfg, on_permission=_reject,
                             on_question=_ignore)
    assert "max_retry_wait" not in backend.kwargs
    assert "quota_re" in backend.kwargs       # the phrases are still there
    assert backend.kwargs["idle_event_timeout"] == 900
    assert ws.path.exists()


def test_the_runner_names_no_provider():
    """Acceptance 7: every phrase a round matches lives in the config, not here.

    Comments are stripped before the check, because the runner's prose may well
    explain the reason — `OpenRouterBackend` is a backend class, not a provider
    name from contest.ini, and neither is `openrouter`, a backend value.
    """
    code = "\n".join(line.split("#", 1)[0] for line in
                     Path(runner_module.__file__).read_text(encoding="utf-8").splitlines())
    code = code.lower()
    for phrase in ("kenary", "kenari", "bynara", "sensenova", "gemini",
                   "openai.com", "generativelanguage", "free-models-per-day",
                   "insufficient_quota", "resource_exhausted", "top up",
                   "billing", "daily limit"):
        assert phrase not in code, phrase


# ── intake: a named variant is asked too ──────────────────────────────────────

def test_a_named_variant_that_answers_a_quota_is_not_started():
    """Acceptance 5: one console line, the agent is not started, the rest runs.

    A note, not a failure: a failure refuses the whole round, and one dry key is
    no reason to stop the agents on the others."""
    agents = agents_from_models("hy3:free@high,agnes-2-5-flash:free@high")
    probes = []

    def probe_for(agent):
        assert agent.variant == "high"

        def try_one(variant):
            assert variant == "high"
            probes.append(agent.name)
            return QUOTA_MESSAGE if agent.name == "hy3" else None
        return try_one

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == []
    assert notes == [f"[hy3] kenary/hy3:free: provider_quota — {QUOTA_MESSAGE} — not started"]
    assert [a.name for a in resolved] == ["agnes-2-5-flash"]
    assert probes == ["hy3", "agnes-2-5-flash"]
    assert resolved[0].variant == "high"


def test_highest_whose_every_rung_answers_a_quota_is_left_out():
    """`highest` on a dry key: every rung is the same quota, so the agent is left
    out with one line, like a named variant — not a failure that stops the round."""
    agents = agents_from_models("hy3:free@highest,agnes-2-5-flash:free@high")

    def probe_for(agent):
        return lambda variant: QUOTA_MESSAGE if agent.name == "hy3" else None

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == []
    assert [a.name for a in resolved] == ["agnes-2-5-flash"]
    assert notes[-1] == f"[hy3] kenary/hy3:free: provider_quota — {QUOTA_MESSAGE} — not started"


def test_highest_with_a_rung_refused_for_its_own_reason_is_still_a_failure():
    """Only an all-quota ladder is left out: a model that cannot talk still refuses."""
    agents = agents_from_models("hy3:free@highest")

    def probe_for(agent):
        return lambda variant: QUOTA_MESSAGE if variant == "high" else "rejected"

    _resolved, failures, _notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert len(failures) == 1 and "no variant answered" in failures[0]


def test_a_named_variant_is_probed_once_per_model_and_variant():
    """One request per provider/model@variant, however many agents ask for it."""
    agents = agents_from_models(
        "hy3:free@high,hy3:free@high,hy3:free@low,agnes-2-5-flash:free@high")
    probes = []

    def probe_for(agent):
        def try_one(variant):
            probes.append((agent.model_id, variant))
            return None
        return try_one

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == [] and notes == []
    assert [a.name for a in resolved] == ["hy3-var1", "hy3-var2", "hy3-var3",
                                          "agnes-2-5-flash"]
    assert probes == [("hy3:free", "high"), ("hy3:free", "low"),
                      ("agnes-2-5-flash:free", "high")]


def test_a_named_variant_refused_for_its_own_reason_is_a_note():
    """Not a quota, not a failure: the agent still runs at the variant it asked for."""
    agents = agents_from_models("hy3:free@high")

    def probe_for(agent):
        return lambda variant: "the model's provider rejected the request"

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == []
    assert [a.name for a in resolved] == ["hy3"]
    assert len(notes) == 1
    assert "kenary/hy3:free@high" in notes[0]


def test_a_named_variant_not_listed_is_a_failure_and_probes_nothing():
    """The refusal is the list: nothing is asked, and the round never starts.

    A probe here would only spend a request against the key that is already
    being refused, and OpenRouter counts a failed request against the quota.
    """
    agents = agents_from_models("hy3:free@max")
    probes = []

    def probe_for(agent):
        def try_one(variant):
            probes.append(variant)
            return None
        return try_one

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == ["[hy3] kenary/hy3:free: no variant 'max' — listed: "
                        "low, medium, high"]
    assert probes == []
    assert notes == []
    assert [a.name for a in resolved] == ["hy3"]


def test_a_named_variant_without_a_probe_is_unchanged():
    """No probe attached is today's behaviour: the variant is sent as named."""
    agents = agents_from_models("hy3:free@high,hy3:free@max")
    resolved, failures, notes = resolve_variants(_OFFER, agents, None)

    assert resolved == agents
    assert notes == []
    assert failures == ["[hy3-var2] kenary/hy3:free: no variant 'max' — listed: "
                        "low, medium, high"]


def test_a_broken_probe_is_a_note_not_a_round_failure():
    """A probe that raises is a refusal, exactly as a rung that refused."""
    agents = agents_from_models("hy3:free@high")

    def probe_for(agent):
        def try_one(variant):
            raise OSError("socket closed")
        return try_one

    resolved, failures, notes = resolve_variants(
        _OFFER, agents, probe_for, quota_re=_quota_re(_config_with_patterns()))

    assert failures == []
    assert [a.name for a in resolved] == ["hy3"]
    assert len(notes) == 1


def test_a_quota_without_phrases_is_no_quota_at_intake():
    """Fail-open: no phrases means a quota is invisible at intake, as today."""
    agents = agents_from_models("hy3:free@high")

    def probe_for(agent):
        return lambda variant: QUOTA_MESSAGE

    resolved, failures, notes = resolve_variants(_OFFER, agents, probe_for, quota_re=None)

    assert failures == []
    assert [a.name for a in resolved] == ["hy3"]
    assert len(notes) == 1


# ── the summary ───────────────────────────────────────────────────────────────

def _run_in_state(name, provider, state, last_error):
    ws = Workspace(agent=name, path=Path(f"/nowhere/{name}"),
                   branch=f"contest/108/{name}", base_sha="0" * 40, kind="clone")
    return AgentRun(agent=AgentSpec(name=name, provider_id=provider,
                                    model_id=f"{name}:free"), workspace=ws,
                    state=state, last_error=last_error)


def _quota_reason(message=QUOTA_MESSAGE, retry_at=RETRY_AT_MS):
    """The runner's own `last_error` for a ProviderQuota, built the same way."""
    from tools.contest.runner import _brief, _error_message
    error = {"name": "ProviderQuota", "data": {"message": message, "retryAt": retry_at}}
    when = (f" (retry at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(retry_at / 1000))})"
            if isinstance(retry_at, (int, float)) else "")
    text = _brief(_error_message(error))
    return f"provider_quota: {text}{when}".rstrip()


def test_the_summary_names_one_line_per_provider():
    """Acceptance 6: `provider_quota: kenary × 2 — retry at 2026-09-26 00:00 UTC`.

    The reason is what the runner writes, so this is the line a real round prints.
    """
    reason = _quota_reason()
    assert reason == (f"provider_quota: {QUOTA_MESSAGE} (retry at {UTC_AT_RESET} UTC)")
    state = RoundState(
        round_no=108, ticket="108-kc61-test", base_sha="0" * 40, started_at=0.0,
        agents=[
            _run_in_state("hy3", "kenary", AgentState.ERROR, reason),
            _run_in_state("agnes-2-5-flash", "kenary", AgentState.ERROR, reason),
            _run_in_state("mimo-v2-5", "other", AgentState.READY, None),
        ])

    assert cli._provider_quota_lines(state) == [
        f"provider_quota: kenary × 2 — retry at {UTC_AT_RESET} UTC"]

    solo = RoundState(round_no=108, ticket="108-kc61-test", base_sha="0" * 40,
                      started_at=0.0,
                      agents=[_run_in_state("hy3", "kenary", AgentState.ERROR,
                                            "provider_quota: insufficient_quota")])
    assert cli._provider_quota_lines(solo) == ["provider_quota: kenary × 1"]
    assert cli._provider_quota_lines(RoundState(
        round_no=108, ticket="t", base_sha="0" * 40, started_at=0.0,
        agents=[])) == []


# ── the roster key ────────────────────────────────────────────────────────────

def test_the_keys_are_exported():
    """The two keys are in `CONTEST_KEYS` and on `ContestConfig`."""
    assert "provider_retry_max_wait_sec" in CONTEST_KEYS
    assert "quota_patterns" in CONTEST_KEYS
    cfg = ContestConfig()
    assert cfg.provider_retry_max_wait_sec == 300.0
    assert cfg.quota_patterns == ""


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
        "[contest.agent.hy3]\n"
        "model = kenary/hy3:free\n",
        encoding="utf-8")
    return ini


def test_a_test_ini_replaces_the_default_list(tmp_path):
    """Acceptance 4: a made-up phrase matches, a default phrase no longer does."""
    cfg = load_roster(_write_ini(
        tmp_path, "provider_retry_max_wait_sec = 120\n"
                  "quota_patterns = zebra quota | no such limit\n"))

    assert cfg.provider_retry_max_wait_sec == 120.0
    assert isinstance(cfg.provider_retry_max_wait_sec, float)

    patterns = _quota_re(cfg)
    assert _is_quota("zebra quota", patterns)
    assert not _is_quota(QUOTA_MESSAGE, patterns)
    assert not _is_quota("insufficient_quota", patterns)


def test_a_negative_bound_is_refused_at_load(tmp_path):
    """A typo that would cap every quota at zero seconds is a `RosterError`."""
    with pytest.raises(Exception) as excinfo:
        load_roster(_write_ini(tmp_path, "provider_retry_max_wait_sec = -1\n"))
    assert "provider_retry_max_wait_sec" in str(excinfo.value)


def test_an_absent_key_is_the_default_and_off_phrases_are_off(tmp_path):
    """Neither key in the roster: 300 s bound, no phrases, no error."""
    cfg = load_roster(_write_ini(tmp_path, ""))
    assert cfg.provider_retry_max_wait_sec == 300.0
    assert cfg.quota_patterns == ""
    assert _quota_re(cfg) is None


def test_the_committed_defaults_are_loaded(monkeypatch):
    """The committed contest.ini carries the list and a bound that is on."""
    monkeypatch.setenv("CONTEST_GATE_API_KEY", "test-gate-key")
    cfg = load_roster(COMMITTED)
    assert cfg.provider_retry_max_wait_sec == 300.0
    assert len(_quota_re(cfg).pattern.split("|")) == len(QUOTA_PHRASES)
    assert _is_quota(QUOTA_TEXTS[0], _quota_re(cfg))
    for text in QUOTA_TEXTS:
        assert _is_quota(text, _quota_re(cfg)), text
    for text in NOT_QUOTA_TEXTS:
        assert not _is_quota(text, _quota_re(cfg)), text
