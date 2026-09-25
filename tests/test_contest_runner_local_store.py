"""KC-62: Kilo's own store errors are retried in the same session.

Round 107, 09:49–09:51 UTC: four of the five live agents went `ERROR` within two
minutes, each on

    {"name": "UnknownError", "data": {"message": "Failed to execute statement"}}

in their `initial` turn after about 28 minutes of work, with 3–4 changed files
and no commit in each worktree. The message is not the provider's: it is the
round's own `kilo serve` refusing a write in the SQLite that every Kilo process
of the user shares, and that the operator's `py_model_test.py -j 12` was writing
at the same time. `_retryable` knew the provider's signals only, so the error
went to the fallback `ERROR` path, `max_error_retries` was never spent on it,
and KC-21 could not harvest a tree with no commit.

Every case here is settled against a scripted backend or a scripted `/proc`, so
nothing waits on a wall clock, a socket or a provider:

  * each of the store's wordings — `Failed to execute statement`,
    `Failed query: insert into "project" …`, `database is locked`,
    `SQLITE_BUSY` — is retried in the same session with `RETRY_PROMPT`, never as
    an `ERROR` after one turn;
  * the store keeps its own budget and its own jittered backoff, and a provider
    429 in the same turn spends the provider's counter, not the local one, and
    the other way round;
  * a spent budget ends `ERROR` with `after N kilo store retries:` in front, and
    leaves `resumable: true` in `state.json` when the tree is dirty, `false`
    when it is clean;
  * `_plan` restarts a resumable `ERROR` the way it restarts a mid-flight agent
    — with `dirty_on_resume` — and does not restart a plain `ERROR`;
  * an old `state.json` that has no `resumable` key reads `False`;
  * two agents that hit the lock in the same second back off at different times;
  * intake names the shared store above the threshold and stays silent below it,
    with the data dir read off the server's own environment;
  * the model check holds `-j` to four next to a live `kilo serve` unless
    `--force` says otherwise, and `--find-free` names the count before it asks
    a provider anything.

`_RETRYABLE_MSG_RE` is not extended: the store's texts need their own budget, and
that rule is also the gate's.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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

import scripts.py_model_test as model_check  # noqa: E402
import tools.contest.cli as cli  # noqa: E402
import tools.contest.kilo_client as kc  # noqa: E402
import tools.contest.runner as runner_module  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest.kilo_client import IdleResult, SessionRef  # noqa: E402
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import (  # noqa: E402
    CONTEST_KEYS,
    AgentSpec,
    ContestConfig,
    load_roster,
)
from tools.contest.runner import (  # noqa: E402
    RETRY_PROMPT,
    AgentRun,
    AgentState,
    RoundState,
    _Heartbeat,
    _is_local_store,
    _plan,
    _retryable,
    run_agent,
)
from tools.contest.workspace import Workspace  # noqa: E402

COMMITTED = REPO_ROOT / "contest.ini"
MODEL_CHECK = REPO_ROOT / "scripts" / "py_model_test.py"

#: The wordings Kilo's own store uses, from round 107's kilo-serve.log and from
#: the model check's own stderr on the same day.
STORE_MESSAGES = (
    "Failed to execute statement",
    'Failed query: insert into "project" (id) values (?)',
    "database is locked",
    "Database is busy",
    "SQLITE_BUSY: database is locked",
    "disk I/O error",
)
#: Not a store: the provider's transients keep KC-19's rule, and a model that
#: cannot be found is a refusal.
NOT_STORE_MESSAGES = (
    "the model's provider interrupted the response stream",
    "upstream unavailable for model x",
    "429 Too Many Requests",
    "Model not found: kenary/x",
    "This model's maximum context length is 128000 tokens",
)

_ECONNRESET = {"name": "APIError", "data": {"message": "Connection reset by server",
                "isRetryable": True, "metadata": {"code": "ECONNRESET"}}}

SESSION = SessionRef(id="ses_store", provider_id="p", model_id="m", directory="/nowhere")

GATE = LlmSettings(base_url="https://gate-test/v1", api_key="k", model="test/gate",
                   api_format="openai", response_format=True, temperature=0.0,
                   max_tokens=256)


def _store_error(message: str = STORE_MESSAGES[0]):
    """One `session.error` in the shape round 107 recorded: name `UnknownError`,
    the text in `data.message`, no `isRetryable`, no `metadata`."""
    return IdleResult(status="error", elapsed=1.0,
                      error={"name": "UnknownError", "data": {"message": message}})


def _idle():
    return IdleResult(status="idle", elapsed=2.0)


class _ScriptedBackend:
    """One agent's backend: every `wait_idle` returns the next scripted result.

    Nothing here touches a socket or a clock, so the round's store errors can be
    rehearsed with a backoff of zero. The last result repeats, which is how a
    script with two results still drives the harvest turn.
    """

    def __init__(self, results):
        self._results = list(results)
        self.prompts: list = []
        self.sessions: list = []

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title,
                       agent=None, variant=None):
        self.sessions.append(provider_id)
        return SESSION

    def prompt(self, session, text):
        self.prompts.append(text)

    def mark(self):
        return None

    def wait_idle(self, session, timeout, **kwargs):
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    def abort(self, session):
        pass

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


def _spec(name: str) -> AgentSpec:
    return AgentSpec(name=name, provider_id="kenary", model_id=f"{name}:free")


def _config(name: str = "hy3", **over) -> ContestConfig:
    """Round 107's limits with the store's budget on, both backoffs at zero so no
    test spends a second on a wait."""
    kw = dict(
        agents=(_spec(name),),
        max_parallel=1,
        max_rework=0,
        turn_timeout_sec=300,
        turn_extend_sec=0,
        idle_event_timeout_sec=900,
        max_error_retries=2,
        error_retry_backoff_sec=0,
        max_local_store_retries=5,
        local_store_retry_backoff_sec=0,
        neighbour_kilo_warn=4,
        gate_max_calls_per_session=20,
        gate_settings=GATE,
    )
    kw.update(over)
    return ContestConfig(**kw)


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _repo(tmp_path, name: str = "repo", agent: str = "hy3") -> Workspace:
    """One base commit and nothing above it: a clean tree, no KC-21 harvest."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "pkg").mkdir()
    (root / "pkg" / "thing.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return Workspace(agent=agent, path=root.resolve(), branch=f"contest/109/{agent}",
                     base_sha=_git(root, "rev-parse", "HEAD"), kind="clone")


def _ticket(tmp_path) -> Path:
    path = tmp_path / "epic-tasks" / "109-kc62-test.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# KC-62 test ticket\n\n**File:** `pkg/thing.py`\n\nbody\n",
                    encoding="utf-8")
    return path


def _run(tmp_path, results, cfg=None, *, dirty: str | None = None, agent: str = "hy3"):
    """One agent through `run_agent`, against the scripted *results*.

    *dirty* is the text to leave uncommitted in the worktree, which is what makes
    an `ERROR` resumable.
    """
    ws = _repo(tmp_path)
    if dirty is not None:
        (ws.path / "pkg" / "wip.py").write_text(dirty, encoding="utf-8")
    config = cfg or _config(agent)
    backend = _ScriptedBackend(results)
    run = AgentRun(agent=config.agents[0], workspace=ws)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_agent(run, backend=backend, policy=Policy(config), config=config,
              ticket_path=_ticket(tmp_path), out_dir=out_dir,
              on_transition=lambda r: None)
    return run, backend, ws, out_dir


def _jsonl(path: Path) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── the matcher ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", STORE_MESSAGES)
def test_each_store_wording_is_a_local_store_error(message):
    """Round 107's payload, in every wording Kilo's store uses for the same
    refusal — the text Kilo put in `data.message`."""
    assert _is_local_store({"name": "UnknownError", "data": {"message": message}})
    assert _is_local_store({"message": message})      # a payload with no `data`


@pytest.mark.parametrize("message", NOT_STORE_MESSAGES)
def test_the_providers_wordings_are_not_local_store(message):
    """KC-19's transients and the model's refusals keep their own path."""
    assert not _is_local_store({"name": "UnknownError", "data": {"message": message}})


def test_the_store_wordings_are_not_provider_retries():
    """§1: they are not added to `_RETRYABLE_MSG_RE` — they need their own
    budget, and that rule is also the gate's."""
    for message in STORE_MESSAGES:
        payload = {"name": "UnknownError", "data": {"message": message}}
        assert not _retryable(payload), message


@pytest.mark.parametrize("junk", [None, "", "Failed to execute statement",
                                  {"data": {"message": 42}}, {"data": {}}, 42])
def test_a_payload_that_is_not_a_dict_or_has_no_message_is_no_store_error(junk):
    """Fail-open: a store error is read out of `data.message`, and nothing else
    can raise on it."""
    assert not _is_local_store(junk)


# ── the runner: the session goes on ──────────────────────────────────────────

@pytest.mark.parametrize("message", STORE_MESSAGES)
def test_a_store_error_is_retried_in_the_same_session(tmp_path, message):
    """Acceptance 1: `Failed to execute statement` on the first turn is a
    `RETRY_PROMPT` in the same session after the backoff, not an `ERROR`.

    Round 107's four agents each had one turn and a commit nothing to be
    exported for; here the second turn idles, so the run ends in the harvest
    instead of in `ERROR` at all.
    """
    run, backend, _ws, out_dir = _run(
        tmp_path, [_store_error(message), _idle()])

    assert run.state is AgentState.GAVE_UP, (run.state, run.last_error)
    assert run.state is not AgentState.ERROR
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert run.turns[0]["idle_status"] == "error"
    assert run.turns[1]["idle_status"] == "idle"
    # one session for both turns: the transcript is not lost, only the write was
    assert len(backend.sessions) == 1
    assert len(backend.prompts) == 2
    retry_text = backend.prompts[1]
    assert RETRY_PROMPT.split("{reason}")[0] in retry_text
    assert "kilo store error" in retry_text
    # and the turn that hit the store is recorded with its cause, so the next
    # reader does not have to dig into the server log
    turns = _jsonl(out_dir / "hy3" / "turns.jsonl")
    assert turns[0]["cause"] == "local_store"
    assert turns[0]["idle_status"] == "error"
    assert "cause" not in turns[1]


def test_a_store_error_is_not_recorded_as_a_provider_retry(tmp_path, caplog):
    """The log line names the store and its own budget, not the provider's."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    _run(tmp_path, [_store_error(), _idle()])
    lines = [r.getMessage() for r in caplog.records if r.name == "tools.contest.runner"]
    assert any(l.startswith("hy3: kilo store error — retry 1/5 in") for l in lines), lines


def test_the_budget_is_exhausted_to_an_error_with_the_store_named(tmp_path):
    """Acceptance 2: six store errors with a budget of five — the sixth ends the
    run `ERROR`, after five retries, with the store named in front."""
    run, backend, _ws, _out = _run(tmp_path, [_store_error() for _ in range(6)])

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 5 kilo store retries:"), run.last_error
    assert "session.error:" in run.last_error
    assert "Failed to execute statement" in run.last_error
    # the initial prompt plus five retries: nothing was spent on the provider
    assert len(backend.prompts) == 6
    assert [t["idle_status"] for t in run.turns] == ["error"] * 6


def test_the_backoff_doubles_per_retry(monkeypatch, tmp_path):
    """Acceptance 5: the backoff doubles per retry, each one jittered ±30 %.

    The threading stand-in records the delay the runner asked for instead of
    sleeping it — the fake clock, so no wall time is spent on a ten-second
    backoff.
    """
    clock = _BackoffClock()
    monkeypatch.setattr(runner_module, "threading", clock)
    cfg = _config(local_store_retry_backoff_sec=10)

    ws = _repo(tmp_path)
    backend = _ScriptedBackend([_store_error(), _store_error(), _idle()])
    run = AgentRun(agent=cfg.agents[0], workspace=ws)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_agent(run, backend=backend, policy=Policy(cfg), config=cfg,
              ticket_path=_ticket(tmp_path), out_dir=out_dir,
              on_transition=lambda r: None)

    assert run.state is AgentState.GAVE_UP
    assert [t["kind"] for t in run.turns] == ["initial", "retry", "retry"]
    first, second = clock.delays
    assert 7.0 <= first <= 13.0, first          # 10 ± 30 %
    assert 14.0 <= second <= 26.0, second       # doubled, then jittered again


def test_two_agents_that_failed_in_the_same_second_retry_at_different_times(
        monkeypatch, tmp_path):
    """Acceptance 5, the other half: two agents that hit the lock together get
    different delays, and neither one sleeps for the un-jittered base."""
    clock = _BackoffClock()
    monkeypatch.setattr(runner_module, "threading", clock)
    cfg = _config(local_store_retry_backoff_sec=10)

    delays = []
    for n in range(2):
        ws = _repo(tmp_path, f"repo-{n}")
        backend = _ScriptedBackend([_store_error(), _idle()])
        run = AgentRun(agent=cfg.agents[0], workspace=ws)
        out_dir = tmp_path / f"out-{n}"
        out_dir.mkdir(parents=True, exist_ok=True)
        run_agent(run, backend=backend, policy=Policy(cfg), config=cfg,
                  ticket_path=_ticket(tmp_path), out_dir=out_dir,
                  on_transition=lambda r: None)
        assert run.state is AgentState.GAVE_UP
        delays.append(clock.delays[-1])

    assert 7.0 <= delays[0] <= 13.0 and 7.0 <= delays[1] <= 13.0
    assert delays[0] != delays[1], delays
    assert all(d != 10.0 for d in delays), delays


def test_the_local_budget_is_zero_means_no_local_retry(tmp_path):
    """`max_local_store_retries = 0` is off: today's fallback, with the prefix
    left out because nothing was tried."""
    run, backend, _ws, _out = _run(
        tmp_path, [_store_error()], cfg=_config(max_local_store_retries=0))

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("session.error: {")
    assert "Failed to execute statement" in run.last_error
    assert "kilo store retries" not in run.last_error
    assert len(backend.prompts) == 1


def test_a_provider_429_and_a_store_error_each_spend_their_own_budget(tmp_path):
    """Acceptance 4: a 429 in the same turn uses the provider's budget and a
    store error the local one — neither eats the other's counter."""
    cfg = _config(max_error_retries=1, max_local_store_retries=1)
    run, backend, _ws, _out = _run(
        tmp_path, [_provider_error(), _store_error(), _store_error(), _idle()], cfg=cfg)

    # 429 → provider retry 1/1, two store errors → local retry 1/1, then ERROR
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 1 kilo store retries:"), run.last_error
    assert "after 1 retries:" in run.last_error
    assert "session.error:" in run.last_error
    assert len(backend.prompts) == 3            # initial + provider retry + local retry


def test_the_local_budget_alone_is_spent_by_a_store_error(tmp_path):
    """Acceptance 4, the other way round: a store error never touches the
    provider's counter, so a 429 that follows still has its retry."""
    cfg = _config(max_error_retries=1, max_local_store_retries=1)
    run, backend, _ws, _out = _run(
        tmp_path, [_store_error(), _provider_error(), _provider_error(), _idle()],
        cfg=cfg)

    assert run.state is AgentState.ERROR
    # the provider's budget was spent on the last error, the local one on the first
    assert run.last_error.startswith("after 1 retries:"), run.last_error
    assert "kilo store retries" not in run.last_error
    assert len(backend.prompts) == 3


def _provider_error():
    return IdleResult(status="error", elapsed=1.0, error=_ECONNRESET)


# ── the work survives the last retry ─────────────────────────────────────────

def test_a_spent_budget_on_a_dirty_tree_is_resumable(tmp_path):
    """Acceptance 2: round 107's shape — six store errors, no commit, a dirty
    tree — ends `ERROR` with `resumable: true` in `state.json`."""
    run, _backend, ws, _out = _run(tmp_path, [_store_error() for _ in range(6)],
                                   dirty="x = 1\n")

    assert run.state is AgentState.ERROR
    assert run.resumable is True
    data = run.to_dict()
    assert data["resumable"] is True
    assert data["state"] == "ERROR"
    assert ws.path.exists()


def test_a_spent_budget_on_a_clean_tree_is_not_resumable(tmp_path):
    """Acceptance 2: nothing uncommitted means nothing to restart for."""
    run, _backend, _ws, _out = _run(tmp_path, [_store_error() for _ in range(6)])

    assert run.state is AgentState.ERROR
    assert run.resumable is False
    assert run.to_dict()["resumable"] is False


def test_a_plain_error_is_never_resumable(tmp_path):
    """An `ERROR` for a reason outside the store is not made resumable by this
    change, whatever the tree holds."""
    run, _backend, _ws, _out = _run(
        tmp_path, [_store_error("Model not found: kenary/x")], dirty="x = 1\n")

    assert run.state is AgentState.ERROR
    assert run.resumable is False


def test_a_state_json_without_resumable_reads_false(tmp_path):
    """Acceptance 3: a `state.json` written before the key is one the runner
    still reads, and the agent reads `False` — no restart."""
    ws = _repo(tmp_path)
    run = AgentRun(agent=_spec("hy3"), workspace=ws, state=AgentState.ERROR,
                   last_error="session.error: boom")
    data = run.to_dict()
    data.pop("resumable", None)
    assert "resumable" not in data

    old = AgentRun.from_dict(data)
    assert old.resumable is False
    assert old.state is AgentState.ERROR
    state = RoundState.from_dict({
        "round_no": 109, "ticket": "109-kc62-test.md", "base_sha": "0" * 40,
        "started_at": 0.0, "agents": [data],
    })
    assert [r.resumable for r in state.agents] == [False]


def test_plan_restarts_only_the_resumable_error(tmp_path):
    """Acceptance 2: `--resume` restarts the resumable `ERROR` in its own
    worktree, with `dirty_on_resume`, and leaves the plain `ERROR` alone."""
    ws_dirty = _repo(tmp_path, "repo-dirty", agent="a")
    (ws_dirty.path / "pkg" / "wip.py").write_text("x = 1\n", encoding="utf-8")
    ws_clean = _repo(tmp_path, "repo-clean", agent="b")

    resume = RoundState(
        round_no=109, ticket="109-kc62-test.md", base_sha="0" * 40, started_at=0.0,
        agents=[
            AgentRun(agent=_spec("a"), workspace=ws_dirty,
                     state=AgentState.ERROR, resumable=True,
                     last_error="after 5 kilo store retries: session.error: Failed to execute statement"),
            AgentRun(agent=_spec("b"), workspace=ws_clean,
                     state=AgentState.ERROR, last_error="session.error: boom"),
        ])
    config = _config(agents=(_spec("a"), _spec("b")))
    runs = _plan(config, [ws_dirty, ws_clean], _ticket(tmp_path), resume)

    by = {r.agent.name: r for r in runs}
    # the resumable one goes back to CREATED in its own worktree, with the work
    assert by["a"].state is AgentState.CREATED
    assert not by["a"].terminal
    assert by["a"].workspace is ws_dirty
    assert by["a"].resumable is False          # spent on this restart
    assert "wip.py" in getattr(by["a"], "dirty_on_resume", "")
    assert by["a"].session_id is None and by["a"].attempt == 0
    # the plain ERROR stays terminal: a resume that did not mean to restart it
    assert by["b"].state is AgentState.ERROR
    assert by["b"].terminal
    assert not getattr(by["b"], "dirty_on_resume", "")


def test_plan_restarts_a_resumable_error_that_has_a_commit(tmp_path):
    """The harvest still scores a commit under the turn: a resumable `ERROR` with
    a commit above the base comes back READY rather than restarted."""
    ws = _repo(tmp_path)
    (ws.path / "pkg" / "thing.py").write_text("def thing():\n    return 42\n",
                                              encoding="utf-8")
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", "KC-62: thing")

    resume = RoundState(
        round_no=109, ticket="109-kc62-test.md", base_sha="0" * 40, started_at=0.0,
        agents=[AgentRun(agent=_spec("hy3"), workspace=ws,
                         state=AgentState.ERROR, resumable=True)])
    runs = _plan(_config(), [ws], _ticket(tmp_path), resume)

    run = runs[0]
    # the verdict decides: no claim row yet, so the turn is not READY and the
    # agent is restarted rather than scored done
    assert run.state is AgentState.CREATED
    assert run.resumable is False
    # the paragraph is for uncommitted work, and a commit is not that
    assert not getattr(run, "dirty_on_resume", "")


# ── the heartbeat names the neighbours ───────────────────────────────────────

def _empty_state() -> RoundState:
    return RoundState(round_no=109, ticket="109-kc62-test.md", base_sha="0" * 40,
                      started_at=time.time(), agents=[])


def test_the_heartbeat_names_the_neighbours_above_the_threshold(monkeypatch):
    """The line gains ` · kilo neighbours N` only when the count is above the
    threshold; below it, and without a pid, it is today's line."""
    state = _empty_state()
    monkeypatch.setattr(runner_module, "kilo_neighbours",
                        lambda pid, proc_root="/proc": (5, "/home/op/.local/share/kilo"))
    line = _Heartbeat(state, {}, 60.0, server_pid=100, neighbour_warn=4).line()
    assert line.endswith("· kilo neighbours 5"), line

    monkeypatch.setattr(runner_module, "kilo_neighbours",
                        lambda pid, proc_root="/proc": (3, "/home/op/.local/share/kilo"))
    assert "kilo neighbours" not in _Heartbeat(
        state, {}, 60.0, server_pid=100, neighbour_warn=4).line()
    # exactly at the threshold: no suffix
    monkeypatch.setattr(runner_module, "kilo_neighbours",
                        lambda pid, proc_root="/proc": (4, "/home/op/.local/share/kilo"))
    assert "kilo neighbours" not in _Heartbeat(
        state, {}, 60.0, server_pid=100, neighbour_warn=4).line()


def test_the_heartbeat_with_no_pid_or_no_threshold_reads_nothing(monkeypatch):
    """Every earlier caller passes neither, and the line is unchanged."""
    calls = []

    def probe(pid, proc_root="/proc"):
        calls.append(pid)
        return (9, "/x")

    monkeypatch.setattr(runner_module, "kilo_neighbours", probe)
    state = _empty_state()
    assert "kilo neighbours" not in _Heartbeat(state, {}, 60.0).line()
    assert "kilo neighbours" not in _Heartbeat(state, {}, 60.0, server_pid=100).line()
    # a threshold that is not a number degrades to no check rather than a crash
    assert "kilo neighbours" not in _Heartbeat(
        state, {}, 60.0, server_pid=100, neighbour_warn="soon").line()
    assert calls == []


# ── the neighbours themselves ────────────────────────────────────────────────

def _write_proc(root: Path, pid: int, *, argv, env: dict, ppid: int = 1) -> None:
    """One `/proc/<pid>` entry: the three files `kilo_neighbours` reads."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode("utf-8") for a in argv) + b"\0")
    (d / "environ").write_bytes(
        b"\0".join(f"{k}={v}".encode("utf-8") for k, v in env.items()) + b"\0")
    (d / "stat").write_text(f"{pid} ({os.path.basename(argv[0])}) S {ppid}\n",
                            encoding="utf-8")


def _neighbour_proc(tmp_path, server_pid=100, *, neighbours=5,
                    home="/home/op", other_home="/home/other") -> Path:
    """A `/proc` with the round's server, *neighbours* sibling Kilo processes on
    the same store, one child of the server, and two that must not count."""
    root = tmp_path / "proc"
    (root / str(server_pid)).mkdir(parents=True)
    _write_proc(root, server_pid, argv=("kilo", "serve", "--port", "8123"),
                env={"HOME": home})
    # the server's own child is part of the round, not a neighbour
    _write_proc(root, server_pid + 1, argv=("kilo", "serve", "--port", "8124"),
                env={"HOME": home}, ppid=server_pid)
    for i in range(neighbours):
        pid = 200 + i
        _write_proc(root, pid, argv=("kilo", "run", "-m", "kenary/hy3:free"),
                    env={"HOME": home})
    # a different user's store, and a non-Kilo process
    _write_proc(root, 900, argv=("kilo", "run", "-m", "kenary/hy3:free"),
                env={"HOME": other_home})
    _write_proc(root, 901, argv=("python3", "scripts/py_model_test.py"),
                env={"HOME": home})
    _write_proc(root, 902, argv=("kilo", "run", "-m", "kenary/hy3:free"),
                env={"XDG_DATA_HOME": "/tmp/other"})
    return root


def test_the_neighbours_are_the_processes_on_the_same_store(tmp_path):
    """Acceptance 6: the count is the other Kilo processes whose own environ
    resolves to the server's data dir — the server's tree, a foreign store and a
    non-Kilo command are all outside it."""
    root = _neighbour_proc(tmp_path, neighbours=5)
    count, data_dir = kc.kilo_neighbours(100, proc_root=str(root))
    assert count == 5
    assert data_dir == "/home/op/.local/share/kilo"

    assert kc.kilo_neighbours(100, proc_root=str(_neighbour_proc(tmp_path / "b",
                                                                 neighbours=3))) == (3,
                                              "/home/op/.local/share/kilo")


def test_the_data_dir_comes_from_the_servers_own_environ(tmp_path):
    """An `XDG_DATA_HOME` on the server decides the store, so two boxes with the
    same pid layout do not read the same answer."""
    root = tmp_path / "proc"
    (root / "100").mkdir(parents=True)
    _write_proc(root, 100, argv=("kilo", "serve", "--port", "8123"),
                env={"XDG_DATA_HOME": "/srv/kilodata", "HOME": "/home/op"})
    _write_proc(root, 201, argv=("kilo", "run", "-m", "m"),
                env={"XDG_DATA_HOME": "/srv/kilodata", "HOME": "/home/op"})
    _write_proc(root, 202, argv=("kilo", "run", "-m", "m"),
                env={"HOME": "/home/op"})

    count, data_dir = kc.kilo_neighbours(100, proc_root=str(root))
    assert (count, data_dir) == (1, "/srv/kilodata/kilo")


def test_the_neighbours_are_fail_open():
    """No pid, no environment, no `/proc` at all: `(0, "")`, never a raise."""
    assert kc.kilo_neighbours(None, proc_root="/proc") == (0, "")
    assert kc.kilo_neighbours("100", proc_root="/proc") == (0, "")
    assert kc.kilo_neighbours(0, proc_root="/proc") == (0, "")
    assert kc.kilo_neighbours(True, proc_root="/proc") == (0, "")
    assert kc.kilo_neighbours(1, proc_root="/definitely/not/a/proc") == (0, "")
    assert kc.kilo_data_dir({}) == ""
    assert kc.kilo_data_dir({"XDG_DATA_HOME": "/x/", "HOME": "/home/op"}) == "/x/kilo"
    assert kc.kilo_data_dir({"HOME": "/home/op/"}) == "/home/op/.local/share/kilo"


def test_the_intake_warns_above_the_threshold_and_stays_silent_below(
        tmp_path, capsys, monkeypatch):
    """Acceptance 6: five neighbours print the line with the store read off the
    server's environment, three print nothing. A warning, not a refusal, and a
    scan that cannot run is no line at all — the round starts either way."""
    root = _neighbour_proc(tmp_path, neighbours=5)
    note = cli._kilo_neighbour_note(_config(neighbour_kilo_warn=4), _FakeServer(100),
                                    proc_root=str(root))
    store = kc.kilo_data_dir({"HOME": "/home/op"})      # from the server's environ
    assert note is not None
    assert note.startswith("kilo: 5 other Kilo processes share ")
    assert store in note
    assert "database errors likely" in note

    root3 = _neighbour_proc(tmp_path / "b", neighbours=3)
    assert cli._kilo_neighbour_note(_config(neighbour_kilo_warn=4), _FakeServer(100),
                                    proc_root=str(root3)) is None

    # no server, an attached server with no pid, no threshold: all silent
    assert cli._kilo_neighbour_note(_config(), None, proc_root=str(root)) is None
    assert cli._kilo_neighbour_note(_config(), _FakeServer(None),
                                    proc_root=str(root)) is None

    # a `/proc` that cannot be read, and one whose scan raises: no line and no
    # exception, so intake starts the round either way
    assert cli._kilo_neighbour_note(_config(), _FakeServer(100),
                                    proc_root=str(tmp_path / "no-such-proc")) is None
    monkeypatch.setattr(cli, "kilo_neighbours",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("denied")))
    assert cli._kilo_neighbour_note(_config(), _FakeServer(100),
                                    proc_root=str(root)) is None
    capsys.readouterr()      # nothing was printed: the line goes out at intake only


class _FakeServer:
    """The one fact intake needs of the server: its pid."""

    def __init__(self, pid):
        self.pid = pid


# ── the roster keys ──────────────────────────────────────────────────────────

def test_the_keys_are_in_the_roster_and_on_the_config():
    """The three keys are loadable, and a roster without them gets the
    documented defaults."""
    for key in ("max_local_store_retries", "local_store_retry_backoff_sec",
                "neighbour_kilo_warn"):
        assert key in CONTEST_KEYS
    cfg = ContestConfig()
    assert cfg.max_local_store_retries == 5
    assert cfg.local_store_retry_backoff_sec == 10
    assert cfg.neighbour_kilo_warn == 4


def _write_ini(tmp_path, extra: str) -> Path:
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


def test_an_absent_key_is_the_default_and_the_committed_ini_carries_them(
        monkeypatch, tmp_path):
    """A roster without the keys is today's behaviour plus the documented
    defaults; the committed contest.ini names the same three numbers."""
    cfg = load_roster(_write_ini(tmp_path, ""))
    assert cfg.max_local_store_retries == 5
    assert cfg.local_store_retry_backoff_sec == 10
    assert cfg.neighbour_kilo_warn == 4

    monkeypatch.setenv("CONTEST_GATE_API_KEY", "test-gate-key")
    committed = load_roster(COMMITTED)
    assert committed.max_local_store_retries == 5
    assert committed.local_store_retry_backoff_sec == 10
    assert committed.neighbour_kilo_warn == 4


def test_a_roster_value_wins(tmp_path):
    """The operator's own numbers, including the switch that turns the retry
    off."""
    cfg = load_roster(_write_ini(
        tmp_path, "max_local_store_retries = 9\n"
                  "local_store_retry_backoff_sec = 25\n"
                  "neighbour_kilo_warn = 0\n"))
    assert cfg.max_local_store_retries == 9
    assert cfg.local_store_retry_backoff_sec == 25
    assert cfg.neighbour_kilo_warn == 0


class _BackoffClock:
    """A `threading` stand-in that records the backoff instead of sleeping it.

    The `Timer` fires its callback at once, so the runner's wait loop exits with
    the event set and no wall time passes; `Event.wait` answers True the same
    way. Everything else in `threading` is the real thing.
    """

    def __init__(self):
        self._real = threading
        self.delays: list = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Event(self):
        class _Event:
            def __init__(self):
                self._set = False

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

            def clear(self):
                self._set = False

            def wait(self, timeout=None):
                return True

        return _Event()

    def Timer(self, delay, fn, *args, **kwargs):
        self.delays.append(float(delay))
        fn()

        class _Timer:
            daemon = True

            def start(self):
                pass

            def cancel(self):
                pass

        return _Timer()


# ── the model check does not outrun a round ──────────────────────────────────

def test_the_check_caps_the_jobs_next_to_a_live_round(tmp_path, capsys):
    """Acceptance 7: a `kilo serve` of this user and `-j 12` is warned and held
    to four; `--force` keeps twelve."""
    root = tmp_path / "proc"
    (root / "777").mkdir(parents=True)
    _write_proc(root, 777, argv=("kilo", "serve", "--port", "8123"), env={})

    args = argparse.Namespace(parallel=12, force=False)
    model_check.cap_jobs(args, str(root))
    assert args.parallel == model_check.JOBS_NEXT_TO_A_ROUND == 4
    out = capsys.readouterr()
    assert "живой раунд (kilo serve pid 777)" in out.err
    assert "-j снижен до 4" in out.err
    assert "--force" in out.err

    args = argparse.Namespace(parallel=12, force=True)
    model_check.cap_jobs(args, str(root))
    assert args.parallel == 12


def test_the_check_leaves_the_jobs_alone_without_a_live_round(tmp_path, capsys):
    """Fail-open: no server, an unreadable `/proc`, or a smaller `-j` — no
    warning and no change."""
    for root in (tmp_path / "no-such-proc", _serveless_proc(tmp_path / "serveless")):
        args = argparse.Namespace(parallel=12, force=False)
        model_check.cap_jobs(args, str(root))
        assert args.parallel == 12
    capsys.readouterr()

    args = argparse.Namespace(parallel=4, force=False)
    model_check.cap_jobs(args, str(tmp_path / "no-such-proc"))
    assert args.parallel == 4
    assert capsys.readouterr().err == ""


def _serveless_proc(root: Path) -> Path:
    """A `/proc` with processes, none of them a `kilo serve`."""
    _write_proc(root, 10, argv=("python3", "scripts/py_model_test.py"), env={})
    _write_proc(root, 11, argv=("kilo", "run", "-m", "kenary/hy3:free"), env={})
    return root


def test_the_check_does_not_count_another_users_serve(tmp_path, capsys):
    """A `kilo serve` owned by another user does not share this store."""
    root = tmp_path / "proc"
    (root / "888").mkdir(parents=True)
    _write_proc(root, 888, argv=("kilo", "serve", "--port", "8123"), env={})
    other = os.getuid() + 1000
    (root / "888" / "status").write_text(f"Uid:\t{other}\t{other}\t{other}\t{other}\n",
                                         encoding="utf-8")

    args = argparse.Namespace(parallel=12, force=False)
    model_check.cap_jobs(args, str(root))
    assert args.parallel == 12
    assert capsys.readouterr().err == ""


def test_the_check_names_the_force_flag():
    """`--force` is in the parser, so the operator can keep the count."""
    result = subprocess.run([sys.executable, str(MODEL_CHECK), "--help"],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert result.returncode == 0
    assert "--force" in result.stdout
    assert "-j" in result.stdout


def _store_proc(root: Path) -> Path:
    """A `/proc` with a live round and one other Kilo process, plus one that
    belongs to another user and so must not count."""
    _write_proc(root, 777, argv=("kilo", "serve", "--port", "8123"), env={})
    _write_proc(root, 201, argv=("kilo", "run", "-m", "kenary/hy3:free"), env={})
    other = os.getuid() + 1000
    _write_proc(root, 900, argv=("kilo", "run", "-m", "kenary/hy3:free"), env={})
    (root / "900" / "status").write_text(f"Uid:\t{other}\t{other}\t{other}\t{other}\n",
                                         encoding="utf-8")
    return root


def test_find_free_names_the_store_it_will_write(tmp_path):
    """Acceptance 7: the check says how many Kilo processes of this user write
    the same store before it asks a provider anything. An empty box and a
    missing `/proc` stay silent rather than raising."""
    root = _store_proc(tmp_path / "proc")
    assert model_check.store_note(str(root)) == (
        "kilo: 2 Kilo processes of this user write the same store (kilo serve pid 777)")

    only = tmp_path / "one"
    _write_proc(only, 777, argv=("kilo", "serve", "--port", "8123"), env={})
    assert model_check.store_note(str(only)) == (
        "kilo: 1 Kilo process of this user writes the same store (kilo serve pid 777)")

    empty = tmp_path / "empty"
    empty.mkdir()
    assert model_check.store_note(str(empty)) is None
    assert model_check.store_note(str(tmp_path / "no-such-proc")) is None


def test_the_find_free_line_goes_out_at_intake(monkeypatch, tmp_path, capsys):
    """The line comes out of `main` for `--find-free`, on stderr, before a
    provider is asked anything: the second pass starts knowing the count."""
    root = _store_proc(tmp_path / "proc")
    monkeypatch.setattr(model_check, "_PROC_ROOT", str(root))
    monkeypatch.setattr(model_check, "find_free", lambda *a, **kw: 0)
    monkeypatch.setattr(sys, "argv", ["py_model_test.py", "--find-free", "openrouter"])

    assert model_check.main() == 0
    captured = capsys.readouterr()
    assert "kilo: 2 Kilo processes of this user write the same store" in captured.err
    assert "pid 777" in captured.err
