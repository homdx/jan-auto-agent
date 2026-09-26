"""tests/test_contest_runner.py — KC-6: the runner against ``tests/_kilo_fake.py``.

Every test builds a sandbox repo with one git worktree per agent (KC-4's
``Workspace`` shape), scripts a ``FakeKiloServer`` turn by turn, and drives
``run_agent`` / ``run_round`` through the ticket's contract only. No test starts
a real ``kilo`` or reaches a provider: the gate is a stub ``completion_fn`` and
the server is the fake. One test per Acceptance scenario, named after it, plus
the concurrency edges a round of N agents exposes (a stall next to a chatty
neighbour, three agents reworking at once, Ctrl-C mid-turn).

``_BenchFake`` adds the two knobs the scenarios need and the fake does not have:
``bad_models`` (``POST /session`` answers 400) and a turn's ``"questions": N``.

KC-34 moved the session behind a protocol: every ``run_agent`` and ``run_round``
here takes a ``KiloBackend`` instead of a ``KiloClient`` and a ``EventTap``, and
nothing in this file touches a tap — the backend owns it and ``close()`` stops it.
The kilo round is otherwise byte-for-byte what it was.

KC-47 (round 91) adds this module's own half: a silence stall that fires with a
``bash`` call still running reads ``no event for 3s during bash: <command>`` in
``last_error``, and ``round_prompt`` names the ``timeout`` the agent's own test
run needs.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _kilo_fake  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest.backend import ContestBackendError, KiloBackend  # noqa: E402
from tools.contest.kilo_client import (  # noqa: E402
    AGENT_TEST_TIMEOUT_MS, IdleResult, KiloServer, SessionRef)
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import AgentSpec, ContestConfig, load_roster  # noqa: E402
from tools.contest.runner import (  # noqa: E402
    AgentRun,
    AgentState,
    RoundState,
    CONTEXT_FULL_SHARE,
    CUT_OFF_MESSAGE,
    TreeReadError,
    _cut_off,
    _finished_replies,
    _is_overflow,
    _reap_worktree,
    _retry_backoff,
    _retry_reason,
    _retryable,
    round_prompt,
    run_agent,
    run_round,
)
from tools.contest.workspace import Workspace, agent_tmp_dir  # noqa: E402

# every test binds an ephemeral-port HTTP server: one xdist worker for all of them
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

ROUND = 45
TICKET = "45-kc6-test.md"
TICKET_BODY = """# KC-6 test ticket

**File:** `pkg/thing.py`
**Also touches:** `tests/test_thing.py` (new)

body
"""
BRIDGE = '''"""stub"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        return raw.strip()[:10]
'''


# ─────────────────────────────────────────────────────────────────────────────
# the sandbox
# ─────────────────────────────────────────────────────────────────────────────

def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Sandbox:
    """A repo with a base commit and one worktree per agent on ``contest/45/<agent>``."""

    def __init__(self, root: Path, agents=("agent-a",)):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        self.ticket_path = root / "epic-tasks" / TICKET
        _write(self.ticket_path, TICKET_BODY)
        r = self.repo
        _git(r, "init", "-q", "-b", "main")
        _git(r, "config", "user.email", "t@example.invalid")
        _git(r, "config", "user.name", "t")
        _write(r / "tools" / "auto" / "collect_bridge.py", BRIDGE)
        _write(r / "pkg" / "__init__.py", "")
        _write(r / "pkg" / "thing.py", "def thing():\n    return 1\n")
        _write(r / ".gitignore", "runs/\n__pycache__/\n")
        _write(r / "tests" / "test_base.py", "def test_base():\n    assert True\n")
        _write(r / "epic-tasks" / TICKET, TICKET_BODY)
        _git(r, "add", "-A")
        _git(r, "commit", "-q", "-m", "base")
        self.base_sha = _git(r, "rev-parse", "HEAD")
        self.out_dir = root / "out"
        self.out_dir.mkdir()
        self.workspaces = [self._add(a) for a in agents]

    def _add(self, agent: str) -> Workspace:
        branch = f"contest/{ROUND}/{agent}"
        path = self.root / "wt" / agent
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), self.base_sha)
        return Workspace(agent=agent, path=path.resolve(), branch=branch,
                         base_sha=self.base_sha, kind="worktree")

    def ws(self, agent: str) -> Workspace:
        return next(w for w in self.workspaces if w.agent == agent)


def _agent_of(directory: str) -> str:
    return Path(directory).name


def _commits(directory: str) -> int:
    base = _git(directory, "merge-base", "main", "HEAD")
    return len(_git(directory, "log", "--oneline", f"{base}..HEAD").splitlines())


def _claim(directory: str, sha: str, outcome: str = "FIXED") -> None:
    """The row ``append_task.py`` writes (it stores ``--outcome DONE`` as FIXED)."""
    agent = _agent_of(directory)
    csv_path = Path(directory) / "runs" / agent / "PROGRESS.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("ticket,finding,outcome,commit,note\n")
        fh.write(f"{TICKET},,{outcome},{sha},test\n")


def _work(directory: str, *, test: bool, claim: bool = True) -> str:
    """The agent's turn: a change (+ a test), one commit (amended on a rework), the claim."""
    d = Path(directory)
    _write(d / "pkg" / "thing.py", f"def thing():\n    return 42  # {time.time()}\n")
    if test:
        _write(d / "tests" / "test_thing.py",
               "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
    _git(directory, "add", "-A")
    if _commits(directory) >= 1:
        _git(directory, "commit", "-q", "--amend", "--no-edit")
    else:
        _git(directory, "commit", "-q", "-m", "KC-6: thing")
    sha = _git(directory, "rev-parse", "HEAD")
    if claim:
        _claim(directory, sha)
    return sha


def work_ready(directory, text):
    _work(directory, test=True)


def work_no_test(directory, text):
    _work(directory, test=False)


def work_no_claim(directory, text):
    """A change and a test, one commit, no `runs/<agent>/PROGRESS.csv` row: the
    harvest rejects it with `no_progress_row` and resolves no claim."""
    _work(directory, test=True, claim=False)


def work_edit_no_commit(directory, text):
    """Edit a file in the worktree but commit nothing — a turn that ends idle
    with uncommitted work (KC-22)."""
    _write(Path(directory) / "pkg" / "thing.py",
           f"def thing():\n    return 7  # {time.time()}\n")


# ─────────────────────────────────────────────────────────────────────────────
# the fake, with the two knobs the scenarios need
# ─────────────────────────────────────────────────────────────────────────────

class _BenchHandler(_kilo_fake._Handler):
    def do_POST(self):
        if self.path.split("?", 1)[0] == "/session":
            body = self._body()
            self._record(body)
            model = ((body or {}).get("model") or {}).get("id")
            if model in self.fake.bad_models:
                return self._json(400, {"error": {"name": "UnknownModel",
                                                  "message": f"unknown model {model}"}})
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            directory = query.get("directory") or self.fake.directory
            return self._json(200, self.fake._create_session(body, directory))
        return super().do_POST()


class _BenchFake(FakeKiloServer):
    """``bad_models`` → 400 on ``POST /session``; ``"questions": N`` in a turn."""

    # FL-1 (round 84): the interval of the heartbeat that covers a scenario
    # hook's real git work — see _run_turn. Far inside the shortest silence
    # window any test here configures (`_stall_config`'s 3 s), because what a
    # loaded box drifts past is the absolute margin, not the ratio.
    HOOK_BEAT_S = 0.1

    def __init__(self, scenario=None, **kw):
        super().__init__(scenario, **kw)
        self.bad_models = set(self.scenario.get("bad_models") or ())

    def start(self):
        if self._httpd is not None:
            return self
        httpd = ThreadingHTTPServer((self.host, self._port), _BenchHandler)
        httpd._fake = self
        httpd.daemon_threads = True
        self._httpd = httpd
        self._base = f"http://{self.host}:{httpd.server_address[1]}"
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def _run_turn(self, session, turn, text):
        for i in range(int(turn.get("questions") or 0)):
            qid, event = self._question_event(session, {"question": f"q{i}?"})
            self._emit(event)
            box = self._pending.get(qid)
            if box is None or not box["event"].wait(self.reply_timeout):
                self.unanswered.append(qid)
                return
        plain = {k: v for k, v in turn.items() if k != "questions"}
        # FL-1 (round 84): the hook heartbeat that used to live here moved to
        # FakeKiloServer._run_turn — every consumer of the fake needs it, not
        # just this file (tests/test_contest_cli.py runs a real `git` commit
        # from a hook under a silence window too). What stays here is the
        # *completion* beat for a turn that scripts no idle: it puts "no
        # event for N s" at the end of the turn's work rather than at the
        # start of it.
        super()._run_turn(session, plain, text)
        if plain.get("idle") is False:
            self._emit({"type": "session.status",
                        "properties": {"sessionID": session.id, "status": "busy"}})

    def pulse(self, session_id: str, every: float, times: int | None,
              then_idle: bool = False,
              idle_after: "threading.Event | None" = None) -> None:
        """Emit a ``session.status busy`` every *every* seconds, on a thread.

        ``times=None`` beats until the fake is stopped, which is what a test
        wants whenever the heartbeat has to outlast something it does not
        control the length of — FL-1 (round 84): a counted heartbeat next to
        a turn whose ``on_prompt`` does real git work is a race on the *total*
        length of the beats, not just on the interval between them. Under the
        operator's 32-worker stress run the hook's commit pushed the turn's
        idle out past the end of a 6 s pulse, the silence window opened after
        the last beat, and a turn that reached READY was aborted on the way.
        Either the beats outlive the turn by construction, or the test is
        betting on how long a git commit takes.

        ``idle_after`` is the same lesson for ``then_idle``: a counted pulse
        that idles on its own thread idles when its count runs out, whether or
        not the hook next to it has finished the work. On a loaded box the
        hook's commit and ``PROGRESS.csv`` row came after that idle, the
        harvest read no row, and a turn that did everything came back
        ``GAVE_UP — no_progress_row``. With an event, the pulse keeps beating
        past its count until the event is set, then idles: the count is the
        least the session chats, and the idle is never before the work, as a
        real agent's never is.
        """
        def run():
            beats = 0
            while not self._stop.is_set() and (times is None or beats < times):
                time.sleep(every)
                beats += 1
                self._emit({"type": "session.status",
                            "properties": {"sessionID": session_id, "status": "busy"}})
            if idle_after is not None:
                while not self._stop.is_set() and not idle_after.wait(every):
                    self._emit({"type": "session.status",
                                "properties": {"sessionID": session_id, "status": "busy"}})
            if then_idle and not self._stop.is_set():
                self._emit({"type": "session.idle", "properties": {"sessionID": session_id}})
        threading.Thread(target=run, daemon=True).start()


class _OverflowFake(_BenchFake):
    """The scenario's `turns_after` replaces `turns` the moment a *second*
    session is created (KC-54).

    `turns` is indexed per session, so the fresh session the runner opens for
    an overflow's uncommitted work replays `turns[0]` — the turn that just
    overflowed — unless the script is swapped. `turns_after` is the script for
    the replacement: the work that finishes the ticket in the new session. A
    scenario without `turns_after` behaves exactly like `_BenchFake`.
    """

    def _create_session(self, body, directory):
        session = super()._create_session(body, directory)
        if len(self.sessions()) > 1 and self.scenario.get("turns_after") is not None:
            self.scenario["turns"] = self.scenario.pop("turns_after")
        return session


class _FinishedFake(_BenchFake):
    """The session opens with one assistant reply that already `finish`ed
    (`finish: "tool-calls"`): the round 74 shape, five steps done before the
    provider turned the request off.

    `turns` is unchanged — the seed is only what the transcript looks like when
    the runner reads it, not another scripted turn."""

    SEED = {"info": {"role": "assistant", "finish": "tool-calls"},
            "parts": [{"type": "text", "text": "five steps done"}]}

    def _create_session(self, body, directory):
        session = super()._create_session(body, directory)
        self._sessions[session["id"]].messages.append(dict(self.SEED))
        return session


def _prompts(fake) -> list:
    """``(session id, text)`` of every prompt_async, in order."""
    out = []
    for r in fake.calls("POST"):
        if r["path"].endswith("/prompt_async"):
            text = "".join(p.get("text", "") for p in (r["body"] or {}).get("parts", []))
            out.append((r["path"].split("/")[2], text))
    return out


def _session_posts(fake) -> list:
    return [r for r in fake.calls("POST") if r["path"] == "/session"]


def _aborted(fake) -> bool:
    return bool(fake.calls(path="/abort"))


# ─────────────────────────────────────────────────────────────────────────────
# config, policy, harness
# ─────────────────────────────────────────────────────────────────────────────

def make_config(agents, **over) -> ContestConfig:
    specs = tuple(AgentSpec(name=a, provider_id="kenary", model_id=f"{a}:free") for a in agents)
    gate = LlmSettings(base_url="https://gate-test/v1", api_key="k", model="test/gate",
                       api_format="openai", response_format=True, temperature=0.0, max_tokens=256)
    # FL-1 (round 84, round 7): `turn_timeout_sec` is a wall-clock deadline the
    # *runner* puts over the whole turn — and 58 turns in this file run a real
    # `git commit` from their `on_prompt` hook under it. That makes the default
    # a Shape-2 hang guard (postmortem §8), not an assertion: no test here
    # asserts that the default fires, and the four that are *about* the turn
    # deadline set a short one explicitly. At 30 s it was a bound around
    # unbounded work, and the operator's stress run walked through it —
    # `test_retry_backoff_is_observed` came back STALLED with
    # `no idle after 30s`. 300 s, so only a genuine hang reaches it.
    # KC-36: `turn_extend_sec = 0` keeps the pre-KC-36 clock — the hard kill
    # at `turn_timeout_sec`, `on_deadline` never passed, and the stall line the
    # turns above compare byte for byte. The turn-deadline tests set it
    # explicitly.
    kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=300,
              turn_extend_sec=0, idle_event_timeout_sec=60, max_questions_per_turn=3,
              tmp_roots=("/tmp/*",), gate_max_calls_per_session=20, gate_settings=gate)
    kw.update(over)
    return ContestConfig(**kw)


def _scratch_arg(cfg: ContestConfig, agent: str = "agent-a") -> str:
    """KC-59: the scratch dir the runner derives for *agent*, as `round_prompt` gets it.

    `make_config` sets `tmp_roots = ("/tmp/*",)`, so every prompt expectation in
    this file names `/tmp/<agent>` — the same derivation the runner runs, not a
    copy of its result."""
    path = agent_tmp_dir(cfg.tmp_roots, agent)
    return str(path) if path is not None else ""


class StubGate:
    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls = 0

    def __call__(self, url, headers, payload, timeout, **kw):
        self.calls += 1
        return json.dumps({"verdict": self.verdict, "reason": "stub"})


def make_policy(config, verdict="reject") -> Policy:
    return Policy(config, completion_fn=StubGate(verdict))


class Harness:
    """Everything ``run_agent`` needs for one agent against one fake."""

    def __init__(self, sb: Sandbox, fake, config, agent="agent-a", policy=None,
                 agent_tmp=None):
        self.sb, self.fake, self.config = sb, fake, config
        self.agent_tmp = agent_tmp
        self.ws = sb.ws(agent)
        spec = next(s for s in config.agents if s.name == agent)
        self.server = KiloServer.attach(fake.url)
        self.backend = KiloBackend(
            self.server, str(self.ws.path),
            events_log=str(sb.out_dir / agent / "events.jsonl"))
        deadline = time.monotonic() + 5
        while fake.subscribers < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.policy = policy or make_policy(config)
        self.transitions: list = []
        self.run = AgentRun(agent=spec, workspace=self.ws)

    def go(self) -> AgentRun:
        def record(run):
            self.transitions.append(run.state)
        try:
            return run_agent(self.run, backend=self.backend, policy=self.policy,
                             config=self.config, ticket_path=self.sb.ticket_path,
                             out_dir=self.sb.out_dir, on_transition=record,
                             agent_tmp=self.agent_tmp)
        finally:
            self.backend.close()


def _run_one(tmp_path, scenario, config=None, policy=None, prepare=None):
    """Run one agent against *scenario*.

    ``prepare(worktree_path)`` runs on agent-a's worktree *before* the round
    starts. FL-1 (round 84): the stall/error harvest tests used to do their
    git work from the turn's own ``on_prompt`` hook, so a real ``git commit``
    ran while the runner's wall-clock silence clock was already ticking. That
    is a race no margin closes — the hook's work is unbounded and the window
    is not — and it survived both a wider window and a heartbeat through the
    hook, because a beat that is emitted still has to be *delivered* through
    a fake HTTP server, an SSE stream and a tap thread, none of which a
    loaded box schedules on demand. The runner cannot tell when a commit was
    made (``_commits_above`` reads the branch at harvest time), so moving the
    work in front of the run loses no coverage and removes the clock from the
    question entirely.
    """
    sb = Sandbox(tmp_path)
    if prepare is not None:
        prepare(str(sb.ws("agent-a").path))
    config = config or make_config(["agent-a"])
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, config, policy=policy)
        run = h.go()
        aborted = _aborted(fake)
    return sb, fake, h, run, aborted


def _make_backend(fake, out_dir):
    """`make_backend` over the fake: one `KiloBackend` per workspace, as `cmd_run` builds.

    FL-1 (round 84): each backend waits for *its own* tap to appear on the
    fake before it is handed back, which is what `Harness` has always done
    and `run_round` never did. A `KiloBackend` opens its event stream on a
    thread, and the fake — like a real server — drops any event emitted
    before that stream is connected. So on a loaded box the round could
    prompt, the fake could start the turn and beat through the scenario
    hook, and every one of those beats could land on nobody: the silence
    clock then ran from `wait_idle` with no events at all and declared the
    stall while the hook was still committing, which is
    `test_a_terminal_harvest_runs_the_roots_under_the_rounds_lock` coming
    back `(STALLED, commit=None)`. Waiting on the count *rising* — not on
    it being non-zero — is what makes this right for a multi-agent round,
    where another agent's tap may already be up.
    """
    server = KiloServer.attach(fake.url)

    def make_backend(ws):
        before = fake.subscribers
        backend = KiloBackend(server, str(ws.path),
                              events_log=str(out_dir / ws.agent / "events.jsonl"))
        deadline = time.monotonic() + 30
        while fake.subscribers <= before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.subscribers > before, f"{ws.agent}: the tap never connected"
        return backend
    return make_backend


def _round(sb, fake, config, resume=None) -> RoundState:
    return run_round(config, ROUND, sb.ticket_path, list(sb.workspaces),
                     make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                     resume=resume)


def _by_name(state: RoundState) -> dict:
    return {r.agent.name: r for r in state.agents}


def _state_json(sb) -> dict:
    return json.loads((sb.out_dir / "state.json").read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _assert_ready(run: AgentRun, ws: Workspace) -> None:
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _git(ws.path, "rev-parse", "HEAD")


def _permission_turn(on_prompt, patterns):
    root = patterns[0].rstrip("/*")
    return {"on_prompt": on_prompt, "events": ["busy", "idle"],
            "permission": {"permission": "external_directory", "patterns": patterns,
                           "metadata": {"command": f"rm -v {root}/x", "directories": [root]}},
            "tool_parts": [{"tool": "bash", "status": "completed",
                            "input": {"command": "ls"}, "output": "ok"}]}


# ─────────────────────────────────────────────────────────────────────────────
# the contract
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_state_is_a_string_enum_whose_last_four_are_terminal():
    names = [s.name for s in AgentState]
    assert names == ["CREATED", "PROMPTED", "WAITING", "HARVESTING", "REWORK",
                     "READY", "GAVE_UP", "STALLED", "ERROR"]
    assert AgentState.READY == "READY" and json.dumps(AgentState.READY) == '"READY"'
    assert [s.terminal for s in AgentState] == [False] * 5 + [True] * 4


def test_round_prompt_carries_both_commands_the_name_the_base_and_the_rule(tmp_path):
    text = round_prompt("zeta-9", tmp_path / "45-x.md", "abc1234")
    assert "python3 scripts/next_task.py --tasks epic-tasks/ --progress runs/zeta-9/PROGRESS.csv" in text
    assert "python3 scripts/append_task.py --progress runs/zeta-9/PROGRESS.csv" in text
    assert "<YOUR NAME>" not in text and "> " not in text.splitlines()[0]
    assert "abc1234" in text
    assert "reviewer" in text and "final" in text
    assert "CollectBridge._shrink" in text and "One local commit" in text
    assert round_prompt("eta", tmp_path / "45-x.md", "abc1234").replace("eta", "zeta-9") == text


TEST_TIMEOUT_SENTENCE = (
    "Running the test suite on this machine can take up to 20 minutes under load:\n"
    "give that `bash` call a `timeout` of at least 1200000 ms."
)

RUNBOOK = REPO_ROOT / "docs" / "collect-epics" / "RUN-THE-EPIC-COMPETITION.md"


def test_round_prompt_names_the_bash_timeout_for_the_agents_own_suite(monkeypatch):
    """KC-47 §5: the runner no longer kills a `bash` that is still running, so
    what can still kill it is the call's *own* `timeout` — Kilo's
    "shell tool terminated command after exceeding timeout 300000 ms". The
    prompt names the number to ask for, as `AGENT_TEST_TIMEOUT_MS`, and the
    runbook's copy of the prompt carries the same sentence: the runbook is
    documentation and may drift, but this line is the one an agent is scored
    against and the one its own test run survives on.

    The constant must stay below the round's turn deadline: a `bash` the
    prompt tells the agent to ask for cannot outlive the turn that holds it.
    """
    assert AGENT_TEST_TIMEOUT_MS == 1_200_000
    assert f"at least {AGENT_TEST_TIMEOUT_MS} ms" in TEST_TIMEOUT_SENTENCE
    text = round_prompt("zeta-9", None, "abc1234")
    assert TEST_TIMEOUT_SENTENCE in text
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "> " + TEST_TIMEOUT_SENTENCE.replace("\n", "\n> ") in runbook

    monkeypatch.setenv("CONTEST_GATE_API_KEY", "test")
    turn_timeout = float(load_roster(str(REPO_ROOT / "contest.ini")).turn_timeout_sec)
    assert AGENT_TEST_TIMEOUT_MS / 1000 < turn_timeout, (AGENT_TEST_TIMEOUT_MS, turn_timeout)


def test_agent_run_and_round_state_round_trip_through_json(tmp_path):
    sb = Sandbox(tmp_path)
    run = AgentRun(agent=AgentSpec("agent-a", "kenary", "m:free", kilo_agent="code"),
                   workspace=sb.ws("agent-a"), state=AgentState.REWORK, attempt=1,
                   turns=[{"kind": "initial", "harvest": {"verdict": "REWORK", "reasons": ["no_test_file"]}}],
                   commit="abc", cost=0.1, tokens={"total": 3})
    run.permissions["asked"] = 2
    state = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha, started_at=1.5, agents=[run])
    back = RoundState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back == state
    assert back.agents[0].workspace.path == sb.ws("agent-a").path
    (row,) = back.table_rows()
    assert row["name"] == "agent-a" and row["model"] == "kenary/m:free"
    assert row["state"] == "REWORK" and row["attempts"] == 1 and row["turns"] == 1
    assert row["permissions"]["asked"] == 2 and row["commit"] == "abc"
    assert row["last_reason"] == "REWORK no_test_file"
    run.last_error = "boom"
    assert state.table_rows()[0]["last_reason"] == "boom"


# ─────────────────────────────────────────────────────────────────────────────
# run_agent — the Acceptance scenarios
# ─────────────────────────────────────────────────────────────────────────────

def test_happy_path_one_turn_ready_state_and_one_turns_line(tmp_path):
    scenario = {"session": {"cost": 0.42, "tokens": {"input": 10, "output": 5, "total": 15}},
                "turns": [{"on_prompt": work_ready, "events": ["busy", "file.edited", "idle"],
                           "assistant": "done"}]}
    sb, fake, h, run, _ = _run_one(tmp_path, scenario)
    ws = sb.ws("agent-a")
    _assert_ready(run, ws)
    assert run.attempt == 0 and len(run.turns) == 1
    turn = run.turns[0]
    assert turn["kind"] == "initial" and turn["idle_status"] == "idle"
    assert turn["sent_at"] <= turn["idle_at"]
    assert turn["harvest"]["verdict"] == "READY"
    assert turn["harvest"]["reasons"] == []
    assert isinstance(turn["harvest"]["elapsed"], float)
    assert run.session_id == fake.sessions()[0].id
    assert run.cost == 0.42 and run.tokens["total"] == 15
    assert len(_jsonl(sb.out_dir / "agent-a" / "turns.jsonl")) == 1
    assert len(json.loads((sb.out_dir / "agent-a.session.json").read_text())) == 2
    assert h.transitions == [AgentState.PROMPTED, AgentState.WAITING, AgentState.HARVESTING, AgentState.READY]
    (post,) = _session_posts(fake)
    assert post["body"]["title"] == f"contest/{ROUND}/agent-a"
    assert post["body"]["model"] == {"providerID": "kenary", "id": "agent-a:free"}
    assert len(post["body"]["permission"]) >= 3
    assert post["query"]["directory"] == str(ws.path)
    (_sid, text), = _prompts(fake)
    assert "runs/agent-a/PROGRESS.csv" in text and sb.base_sha in text


def test_rework_path_reprompts_the_same_session_with_the_reason_sentence(tmp_path):
    scenario = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb, fake, h, run, _ = _run_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert run.attempt == 1
    assert [t["kind"] for t in run.turns] == ["initial", "rework"]
    assert run.turns[0]["harvest"]["verdict"] == "REWORK"
    assert run.turns[0]["harvest"]["reasons"] == ["no_test_file"]
    assert isinstance(run.turns[0]["harvest"]["elapsed"], float)
    (sid1, _), (sid2, rework) = _prompts(fake)
    assert sid1 == sid2 and len(_session_posts(fake)) == 1
    assert "shipped no test file" in rework and "Attempt 1 of 2" in rework
    assert h.transitions.count(AgentState.PROMPTED) == 2 and AgentState.REWORK in h.transitions
    assert len(_jsonl(sb.out_dir / "agent-a" / "turns.jsonl")) == 2


def test_give_up_after_max_rework_keeps_the_commit(tmp_path):
    scenario = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          {"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, make_config(["agent-a"], max_rework=1))
    assert run.state is AgentState.GAVE_UP
    assert len(_prompts(fake)) == 2 and "Attempt 1 of 1" in _prompts(fake)[1][1]
    assert run.commit == _git(sb.ws("agent-a").path, "rev-parse", "HEAD")
    assert run.attempt == 1 and "no_test_file" in run.last_error


def test_permission_under_tmp_roots_is_once_and_a_mechanical_line(tmp_path):
    cfg = make_config(["agent-a"], tmp_roots=("/tmp/*",))
    sb, fake, _h, run, _ = _run_one(tmp_path, {"turns": [_permission_turn(work_ready, ["/tmp/*"])]},
                                    cfg, make_policy(cfg, "reject"))
    _assert_ready(run, sb.ws("agent-a"))
    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "once"
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "mechanical" and line["reply"] == "once"
    assert line["sessionID"] == run.session_id
    assert run.permissions == {"asked": 1, "allowed": 1, "rejected": 0, "gated": 0, "gate_failed": 0}
    assert not fake.unanswered


def test_permission_outside_tmp_roots_is_the_gates_reject_and_a_gate_line(tmp_path):
    cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",))
    policy = make_policy(cfg, "reject")
    sb, fake, _h, run, _ = _run_one(tmp_path, {"turns": [_permission_turn(work_ready, ["/var/lib/*"])]},
                                    cfg, policy)
    _assert_ready(run, sb.ws("agent-a"))
    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "reject"
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "gate" and line["reply"] == "reject"
    assert policy._completion_fn.calls == 1
    assert run.permissions == {"asked": 1, "allowed": 0, "rejected": 1, "gated": 1, "gate_failed": 0}


def test_gate_budget_left_comes_from_the_counters(tmp_path):
    cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",), gate_max_calls_per_session=1)
    policy = make_policy(cfg, "allow")
    scenario = {"turns": [_permission_turn(work_no_test, ["/var/lib/*"]),
                          _permission_turn(work_ready, ["/var/lib/*"])]}
    sb, _fake, _h, run, _ = _run_one(tmp_path, scenario, cfg, policy)
    _assert_ready(run, sb.ws("agent-a"))
    first, second = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert (first["layer"], first["reply"]) == ("gate", "once")
    assert (second["layer"], second["reply"]) == ("budget", "reject")
    assert policy._completion_fn.calls == 1
    assert run.permissions == {"asked": 2, "allowed": 1, "rejected": 1, "gated": 1, "gate_failed": 1}


def test_a_sibling_worktree_is_forbidden_ground(tmp_path):
    """Another agent's worktree is under the same rounds folder: the policy
    rejects it mechanically, without asking the gate."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    other = str(sb.ws("agent-b").path)
    cfg = make_config(["agent-a"], tmp_roots=(str(tmp_path) + "/*",))
    policy = make_policy(cfg, "allow")
    with _BenchFake({"turns": [_permission_turn(work_ready, [other + "/*"])]}) as fake:
        run = Harness(sb, fake, cfg, policy=policy).go()
        (replied,) = fake.events_of("permission.replied")
    _assert_ready(run, sb.ws("agent-a"))
    assert replied["properties"]["reply"] == "reject"
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "mechanical" and "forbidden" in line["reason"]
    assert policy._completion_fn.calls == 0


def test_own_worktree_by_absolute_path_is_not_forbidden_ground(tmp_path):
    """KC-46: the rounds folder is forbidden, but the agent's own worktree is
    in it. Reading its own file by absolute path is ``once`` from layer 1,
    without the gate, while a sibling stays forbidden (the test above)."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    own = sb.ws("agent-a").path
    command = f"head -3 {own}/pkg/thing.py"
    cfg = make_config(["agent-a"])
    policy = make_policy(cfg, "reject")
    turn = {"on_prompt": work_ready, "events": ["busy", "idle"],
            "permission": {"permission": "bash", "patterns": [command],
                           "metadata": {"command": command}},
            "tool_parts": [{"tool": "bash", "status": "completed",
                            "input": {"command": "ls"}, "output": "ok"}]}
    with _BenchFake({"turns": [turn]}) as fake:
        run = Harness(sb, fake, cfg, policy=policy).go()
        (replied,) = fake.events_of("permission.replied")
    _assert_ready(run, sb.ws("agent-a"))
    assert replied["properties"]["reply"] == "once"
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "mechanical"
    assert line["reason"] == "inside worktree/tmp_roots"
    assert policy._completion_fn.calls == 0


def test_own_scratch_dir_is_once_and_the_other_agents_are_forbidden(tmp_path):
    """KC-59: the scratch dir is per agent. This agent's own ``<tmp_root>/<agent>/``
    is a mechanical `once`; another agent's dir is a mechanical `forbidden` —
    neither spends a gate call, so a write into a sibling's dir is settled by
    geometry and cannot be approved by an exhausted reviewer."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    tmp_root = tmp_path / "tmp"
    cfg = make_config(["agent-a", "agent-b"], tmp_roots=(str(tmp_root) + "/*",))

    decisions = []
    for target, want_reply in ((str(tmp_root / "agent-a") + "/*", "once"),
                               (str(tmp_root / "agent-b") + "/*", "reject")):
        policy = make_policy(cfg, "allow")
        with _BenchFake({"turns": [_permission_turn(work_ready, [target])]}) as fake:
            run = Harness(sb, fake, cfg, policy=policy).go()
            (replied,) = fake.events_of("permission.replied")
        _assert_ready(run, sb.ws("agent-a"))
        assert replied["properties"]["reply"] == want_reply
        decisions.append(_jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")[-1])
        assert policy._completion_fn.calls == 0

    assert decisions[0]["layer"] == "mechanical"
    assert decisions[0]["reason"] == "inside worktree/tmp_roots"
    assert decisions[1]["layer"] == "mechanical"
    assert "forbidden" in decisions[1]["reason"]
    assert str(tmp_root / "agent-b") in decisions[1]["reason"]


def test_the_other_agents_dir_under_the_second_scratch_root_is_forbidden_too(tmp_path):
    """KC-59 follow-up: with two scratch roots a sibling's dir under the second
    one is a mechanical reject as well, not a `once` through the shared glob."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    kilo, contest = tmp_path / "kilo", tmp_path / "contest"
    cfg = make_config(["agent-a", "agent-b"],
                      tmp_roots=(str(kilo) + "/*", str(contest) + "/*"))
    policy = make_policy(cfg, "allow")
    with _BenchFake({"turns": [_permission_turn(work_ready, [str(contest / "agent-b") + "/*"])]}) as fake:
        run = Harness(sb, fake, cfg, policy=policy).go()
        (replied,) = fake.events_of("permission.replied")
    _assert_ready(run, sb.ws("agent-a"))
    assert replied["properties"]["reply"] == "reject"
    line = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")[-1]
    assert line["layer"] == "mechanical" and str(contest / "agent-b") in line["reason"]
    assert policy._completion_fn.calls == 0


def test_the_prompt_sent_to_the_agent_names_its_own_scratch_dir(tmp_path):
    """KC-59: the runner derives the dir from `tmp_roots` and the agent's name —
    no path is named in the runner's code, and no other agent's dir is offered."""
    tmp_root = tmp_path / "tmp"
    cfg = make_config(["agent-a"], tmp_roots=(str(tmp_root) + "/*",))
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    _assert_ready(run, sb.ws("agent-a"))
    (_sid, text), = _prompts(fake)
    assert "scratch dir is " + str(tmp_root / "agent-a") in text
    assert str(tmp_root / "agent-b") not in text


def test_round_prompt_names_the_scratch_dir_and_stays_unchanged_without_one(tmp_path):
    """KC-59: the sentence is appended when the round derived a dir, and the text
    without one is byte-identical to today's — an absent `tmp_roots` key must not
    change what the agent is told."""
    base = round_prompt("zeta-9", tmp_path / "45-x.md", "abc1234")
    named = round_prompt("zeta-9", tmp_path / "45-x.md", "abc1234", tmp_dir="/tmp/kilo/zeta-9")
    assert named.startswith(base)
    assert "scratch dir is /tmp/kilo/zeta-9" in named

    a = round_prompt("agent-a", tmp_path / "45-x.md", "abc1234", tmp_dir="/tmp/kilo/agent-a")
    b = round_prompt("agent-b", tmp_path / "45-x.md", "abc1234", tmp_dir="/tmp/kilo/agent-b")
    assert "scratch dir is /tmp/kilo/agent-a" in a and "/tmp/kilo/agent-b" not in a
    assert "scratch dir is /tmp/kilo/agent-b" in b and "/tmp/kilo/agent-a" not in b


def test_a_round_without_tmp_roots_keeps_the_prompt_and_runs(tmp_path):
    """KC-59 fail-open: no ``tmp_roots``, or only malformed values, degrades to "no
    scratch dir" — the agent gets today's prompt byte for byte and nothing raises
    into the round."""
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    cases = [("empty", {"tmp_roots": ()}),
             ("malformed", {"tmp_roots": (None, "", "not-a-path", 42)})]
    for label, over in cases:
        cfg = make_config(["agent-a"], **over)
        sb, fake, _h, run, _ = _run_one(tmp_path / label, scenario, cfg)
        _assert_ready(run, sb.ws("agent-a"))
        (_sid, text), = _prompts(fake)
        assert text == round_prompt("agent-a", sb.ticket_path, sb.base_sha)
        assert "scratch dir is" not in text

def test_three_questions_in_one_turn_stall_and_abort(tmp_path):
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy"], "questions": 3, "delay": 0.5}]}
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario)
    assert run.state is AgentState.STALLED and aborted
    assert run.questions == 3 and "questions" in run.last_error
    assert len(fake.calls(prefix="/question/")) >= 2
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert len(_jsonl(sb.out_dir / "agent-a" / "turns.jsonl")) == 1


def test_two_questions_per_turn_are_not_a_stall_and_the_count_resets(tmp_path):
    scenario = {"turns": [{"on_prompt": work_no_test, "events": ["busy"], "questions": 2},
                          {"on_prompt": work_ready, "events": ["busy"], "questions": 2}]}
    sb, _fake, _h, run, aborted = _run_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert not aborted and run.questions == 4


def test_session_error_is_error_with_the_payload(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "error": {"name": "ProviderError", "message": "boom-42"}}]}
    sb, _fake, _h, run, _ = _run_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert "boom-42" in run.last_error
    assert run.turns[0]["idle_status"] == "error"
    assert (sb.out_dir / "agent-a.session.json").is_file()


def test_unknown_model_is_error_with_the_body(tmp_path):
    scenario = {"bad_models": {"agent-a:free"}, "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _sb, fake, h, run, _ = _run_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert "unknown model agent-a:free" in run.last_error
    assert not _prompts(fake)
    assert h.transitions == [AgentState.ERROR]


def test_idle_event_timeout_stalls_a_silent_session(tmp_path):
    # 300 s vs 1 s: the two paths this test tells apart, as far apart as they go
    cfg = make_config(["agent-a"], turn_timeout_sec=300, idle_event_timeout_sec=1)
    _sb, _fake, _h, run, aborted = _run_one(tmp_path, {"turns": [{"events": [], "idle": False}]}, cfg)
    assert run.state is AgentState.STALLED and aborted
    # took the idle-event path (last_error/idle_status below), not the
    # turn_timeout path — a load-independent way to tell the two apart, since
    # a wall-clock bound on top is exactly what FL-1 (round 84) found flaky.
    assert run.last_error == "no event for 1s"
    assert run.turns[0]["idle_status"] == "stalled"


#: The round 86 command: what five of the six silence stalls in contest-out
#: were running when the window fired.
TEST_SUITE_COMMAND = "python3 -m pytest tests -n 4"


def _open_bash_part(command: str, timeout_ms: int) -> dict:
    """One `message.part.updated` opening a `bash` call, the round 86 shape."""
    return {"type": "message.part.updated",
            "properties": {"sessionID": "fillme",
                           "part": {"id": "part_test", "type": "tool",
                                    "tool": "bash",
                                    "state": {"status": "running",
                                              "input": {"command": command,
                                                        "timeout": timeout_ms}}}}}


def test_a_stall_inside_a_running_bash_names_the_call(tmp_path):
    """KC-47 §4: a silence stall that fires while the session still has a
    `bash` call in flight says so in `last_error`, instead of a bare
    "no event for 3s" that sends the next reader into events.jsonl.

    The turn scripts no idle at all. The only thing that happens on the
    stream after the turn starts is a `message.part.updated` opening a 400 ms
    `bash` call, then nothing: the bound is the call's own timeout plus the
    window of grace, so the stall lands at about 3.4 s — under the 300 s turn
    deadline, which is what makes it the silence path rather than
    "no idle after 300s".

    The window stays at the repo's 3 s stall window: what has to be delivered
    before the clock runs out is the part event itself, and 0.9 s of margin
    is inside what the operator's 32-worker stress run drifts by (FL-1).
    """
    sb = Sandbox(tmp_path)
    cfg = _stall_config()
    scenario = {"turns": [{"events": [], "idle": False}]}
    with _BenchFake(scenario) as fake:
        def open_the_call():
            part = _open_bash_part(TEST_SUITE_COMMAND, 400)
            part["properties"]["sessionID"] = fake.sessions()[0].id
            fake._emit(part)

        scenario["turns"][0]["on_prompt"] = lambda d, t: threading.Thread(
            target=open_the_call, daemon=True).start()
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)

    assert run.state is AgentState.STALLED and aborted
    assert run.last_error == "no event for 3s during bash: " + TEST_SUITE_COMMAND
    assert run.turns[0]["idle_status"] == "stalled"
    (line,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert line["idle_status"] == "stalled"


def test_events_of_the_session_keep_a_turn_alive(tmp_path):
    """A status event every `BEAT` for `KEEPALIVE_BEATS` beats, then idle:
    a turn that keeps emitting is not aborted, and the runner's
    `idle_event_timeout_sec` reaches `wait_idle` intact.

    FL-1 (round 84). This test cost four rounds of stress runs, and what
    finally settled it was giving up on proving the *semantics* here.

    A regression — `wait_idle` not counting `session.status` as an event of
    the session — cuts the turn at `window`. Proving the opposite therefore
    means *surviving longer than the window*, and the turn survives only if
    no gap between two consecutive **delivered** events exceeds it. That
    makes three numbers one number: the window, this test's runtime, and its
    tolerance for a starved box. There is no margin to widen, and narrowing
    the beat does not substitute — a version of this emitted every 0.1 s and
    the runner still saw three seconds of silence, because what starves is
    the delivery path and not the emitting thread.

    So the semantics moved to `tests/test_contest_kilo_client.py`, to
    `test_events_of_the_session_keep_wait_idle_alive` and its two siblings,
    which drive `wait_idle` over a scripted tap on a fake clock: no
    transport to starve and no real time at all. A KC-12 regression fails
    there deterministically — verified by injecting one.

    What is left here is the integration half, and it is load-proof: a turn
    that keeps emitting is not aborted. The window is far longer than the
    beats, so nothing has to outrun anything;
    `test_idle_event_timeout_stalls_a_silent_session` is what proves the
    configured value reaches `wait_idle` at all, and it wants its stall, so
    it is load-proof too.

    The span still comes from the beat *count* rather than a `delay`, with
    the pulse emitting the idle itself, so a starved box can only make the
    turn longer, never end it before the beats do — nor before the hook's
    work: the pulse idles only once `work_ready` has returned (`idle_after`),
    because a counted idle racing a loaded commit is the same bet on git's
    speed, and it lost as `GAVE_UP — no_progress_row`."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=300,
                      idle_event_timeout_sec=CHATTY_WINDOW_S)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        work_done = threading.Event()

        def on_prompt(d, t):
            fake.pulse(fake.sessions()[-1].id, KEEPALIVE_BEAT_S, CHATTY_BEATS,
                       then_idle=True, idle_after=work_done)
            try:
                work_ready(d, t)
            finally:
                work_done.set()

        scenario["turns"][0]["on_prompt"] = on_prompt
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)
    _assert_ready(run, sb.ws("agent-a"))
    assert run.turns[0]["idle_status"] == "idle"
    assert not aborted


def test_a_counted_pulse_idles_only_after_the_work_it_covers():
    """The bench's own contract (`_BenchFake.pulse(..., idle_after=)`): the
    count runs out first, the pulse keeps beating, and `session.idle` comes
    only once the work is done — never on the count alone. Without it the two
    keep-alive tests above idle before a loaded commit and its PROGRESS.csv
    row, and a turn that did everything is `GAVE_UP — no_progress_row`."""
    with _BenchFake({"turns": []}) as fake:
        seen: list = []
        fake._emit = lambda event: seen.append(event["type"])
        work_done = threading.Event()
        fake.pulse("ses_x", 0.02, 3, then_idle=True, idle_after=work_done)

        deadline = time.monotonic() + 30
        while seen.count("session.status") < 6 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen.count("session.status") >= 6, seen   # past the count of 3
        assert "session.idle" not in seen, seen          # and still no idle

        work_done.set()
        while "session.idle" not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen[-1] == "session.idle", seen


def test_turn_timeout_stalls_a_session_that_never_idles(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=1, idle_event_timeout_sec=60)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        scenario["turns"][0]["on_prompt"] = lambda d, t: fake.pulse(fake.sessions()[-1].id, 0.3, 20)
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)
    # fired on turn_timeout, not the (60 s) idle-event path — idle_status/
    # last_error say so directly and load-independently; FL-1 (round 84)
    # found the wall-clock bound this used to carry on top flaky.
    assert run.state is AgentState.STALLED and aborted
    assert run.turns[0]["idle_status"] == "timeout"
    assert run.last_error == "no idle after 1s"


def test_server_going_away_mid_turn_is_error(tmp_path):
    """The server disappears while a turn is in flight: ERROR with
    `idle_status == "closed"`, detected off the closed stream rather than by
    waiting out the turn deadline.

    FL-1 (round 84, round 7): the server used to be taken away by a
    `threading.Timer(0.8, fake.stop)` armed *before* the harness was built —
    a wall-clock bet that the setup (a backend, a tap handshake, a session,
    a prompt) finishes inside 0.8 s. On a loaded box it does not: the fake
    was already gone when the prompt went out, the run came back ERROR with
    **no turns at all**, and the assertion below died on `run.turns[0]` with
    an IndexError instead of on anything it means to check.

    The stop is now triggered *by the turn itself*, so "mid-turn" is a fact
    rather than a hope. `on_prompt` runs inside the fake's own turn thread,
    which is underneath `serve_forever` — so the stop, which joins that
    loop, has to happen on a thread of its own."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=300, idle_event_timeout_sec=60)

    def take_the_server_away(directory, text):
        threading.Thread(target=fake.stop, daemon=True).start()

    fake = _BenchFake({"turns": [{"on_prompt": take_the_server_away,
                                  "events": ["busy"], "idle": False}]}).start()
    try:
        started = time.monotonic()
        run = Harness(sb, fake, cfg).go()
        elapsed = time.monotonic() - started
    finally:
        fake.stop()
    # detected the closed connection rather than waiting out turn_timeout
    assert run.state is AgentState.ERROR
    assert elapsed < cfg.turn_timeout_sec
    assert run.turns[0]["idle_status"] == "closed"


# ─────────────────────────────────────────────────────────────────────────────
# KC-21: a STALLED / ERROR turn with a commit on its branch is harvested
# ─────────────────────────────────────────────────────────────────────────────

# FL-1 (round 84): the two tests that must *survive* a silence window rather
# than trip it — see test_events_of_the_session_keep_a_turn_alive for why the
# window, the runtime and the tolerance for a starved box are all one number.
KEEPALIVE_WINDOW_S = 8
KEEPALIVE_BEAT_S = 0.2
KEEPALIVE_BEATS = 60          # 12 s of beats, comfortably past the window

# The chatty *neighbour* does not have to outlive anything: its turn ends on
# its own, well inside the window the silent agent is tripping. See
# test_a_silent_agent_stalls_next_to_a_chatty_one.
NEIGHBOUR_BEATS = 10          # 2 s of chatter under an 8 s window

# The keep-alive turn does not have to outrun its own window any more — the
# semantics are settled deterministically in test_contest_kilo_client.py — so
# it runs for a quarter as long under a window nothing plausibly starves past.
CHATTY_WINDOW_S = 60
CHATTY_BEATS = 25             # 5 s of beats, a twelfth of the window


def _stall_config(**over) -> ContestConfig:
    """A silence stall inside the turn deadline, so the turn is `stalled`, not a
    turn timeout."""
    # FL-1 (round 84): 3 s, not 1 s. These tests want the stall to fire, so
    # the window is their *slow* path and widening it costs a green run only
    # the two extra seconds it spends proving the stall. What it buys is
    # 2.9 s of margin over the 0.1 s hook heartbeat above, where a 1 s window
    # left 0.9 s — and 0.9 s is inside what the operator's 32-worker stress
    # run drifts by.
    kw = dict(turn_timeout_sec=300, idle_event_timeout_sec=3)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def _runner_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.name == "tools.contest.runner"]


def _runner_has(caplog, needle: str) -> bool:
    return any(needle in line for line in _runner_lines(caplog))


def _branch_sha(ws) -> str:
    return _git(ws.path, "rev-parse", "HEAD")


def test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready(tmp_path, caplog):
    """The work is committed and claimed, then the session goes silent:
    READY, `run.commit` is that sha, the single turn carries
    `idle_status == "stalled"` and the READY harvest, and the KC-18 line
    reads `<agent>: READY — <sha12> after no event for 3s`.

    FL-1 (round 84): the commit is made by `prepare`, before the run — see
    `_run_one`. Doing it from the turn's hook put a real `git commit` inside
    a wall-clock silence window, which is a race no margin closes."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    cfg = _stall_config()
    scenario = {"turns": [{"events": [], "idle": False}]}
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario, cfg,
                                          prepare=lambda d: work_ready(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _branch_sha(ws)
    assert aborted
    assert run.last_error is None and run.attempt == 0
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "READY"
    assert turn["harvest"]["reasons"] == []
    assert isinstance(turn["harvest"]["elapsed"], float)
    assert _runner_has(caplog, f"agent-a: READY — {run.commit[:12]} after no event for 3s")
    (line,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert line["idle_status"] == "stalled" and line["harvest"]["verdict"] == "READY"


def test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt(tmp_path):
    """One commit with no `PROGRESS.csv` row: STALLED, `run.commit` is that
    sha, the turn's harvest is REWORK, `last_error` is the stall text,
    `attempt` is unchanged, and no second prompt went to the fake.

    FL-1 (round 84): the commit is made by `prepare`, before the run — see
    `_run_one` and the sibling READY test above."""
    sb, fake, _h, run, _ = _run_one(tmp_path,
        {"turns": [{"events": [], "idle": False}]}, _stall_config(),
        prepare=lambda d: work_no_claim(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, run.last_error
    assert run.commit == _branch_sha(ws)
    assert run.last_error == "no event for 3s"
    assert run.attempt == 0
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "REWORK"
    assert turn["harvest"]["reasons"] == ["no_progress_row"]
    assert isinstance(turn["harvest"]["elapsed"], float)
    assert len(_prompts(fake)) == 1


def test_a_stalled_turn_reads_the_rejected_commit_from_the_harvest(tmp_path, monkeypatch):
    """KC-30: the sha of a no-row stall comes from `Harvest.commit` — the harvest
    names the branch's one commit itself — so the runner holds no `rev-parse` of
    its own to drift out of step with it.

    `_commits_above` still asks git for the count (`rev-list`), so the wrapper
    refuses only `rev-parse`; the harvest's own `rev-parse` runs in another
    module and is untouched."""
    import tools.contest.gates as gates_mod
    import tools.contest.runner as runner_mod

    real_git = gates_mod.git

    def no_rev_parse(cwd, *args, **kwargs):
        if "rev-parse" in args:
            raise AssertionError("KC-30: the runner must not rev-parse a commit itself")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr("tools.contest.runner.git", no_rev_parse)

    sb, fake, _h, run, _ = _run_one(tmp_path,
        {"turns": [{"events": [], "idle": False}]}, _stall_config(),
        prepare=lambda d: work_no_claim(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, run.last_error
    assert run.commit == _branch_sha(ws)
    (turn,) = run.turns
    assert turn["harvest"]["verdict"] == "REWORK"
    assert turn["harvest"]["reasons"] == ["no_progress_row"]
    assert len(_prompts(fake)) == 1
    # the fallback is gone for good, not just unused: no `rev-parse` in runner.py
    assert "rev-parse" not in Path(runner_mod.__file__).read_text(encoding="utf-8")


def test_an_error_turn_with_a_valid_commit_is_harvested_to_ready(tmp_path, caplog):
    """`session.error` after the commit and the claim: the same harvest as the
    stall, ERROR standing in for STALLED."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    scenario = {"turns": [{"events": ["busy"],
                           "error": {"name": "ProviderError", "message": "boom-42"}}]}
    sb, _fake, _h, run, _ = _run_one(tmp_path, scenario, _stall_config(),
                                     prepare=lambda d: work_ready(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _branch_sha(ws)
    assert run.last_error is None and run.attempt == 0
    (turn,) = run.turns
    assert turn["idle_status"] == "error"
    assert turn["harvest"]["verdict"] == "READY"
    assert turn["harvest"]["reasons"] == []
    assert isinstance(turn["harvest"]["elapsed"], float)
    assert _runner_has(caplog, f"agent-a: READY — {run.commit[:12]} after session.error:")


def test_an_error_turn_with_a_rejected_commit_stays_error_without_a_reprompt(tmp_path):
    sb, fake, _h, run, _ = _run_one(tmp_path,
        {"turns": [{"events": ["busy"],
                    "error": {"name": "ProviderError", "message": "boom-42"}}]},
        _stall_config(), prepare=lambda d: work_no_claim(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.ERROR, run.last_error
    assert run.commit == _branch_sha(ws)
    assert "boom-42" in run.last_error
    assert run.attempt == 0
    (turn,) = run.turns
    assert turn["idle_status"] == "error"
    assert turn["harvest"]["verdict"] == "REWORK"
    assert turn["harvest"]["reasons"] == ["no_progress_row"]
    assert isinstance(turn["harvest"]["elapsed"], float)
    assert len(_prompts(fake)) == 1


def test_a_stall_with_no_commit_is_not_harvested(tmp_path, monkeypatch):
    """Nothing above the base: today's path byte for byte — STALLED, `commit`
    None, no `harvest` key on the turn, and the harvest is never called."""
    def boom(*args, **kwargs):
        raise AssertionError("_harvest must not run for a branch with no commit")

    monkeypatch.setattr("tools.contest.runner._harvest", boom)
    sb, fake, _h, run, aborted = _run_one(tmp_path, {"turns": [{"events": [], "idle": False}]},
                                          _stall_config())
    assert run.state is AgentState.STALLED and aborted
    assert run.last_error == "no event for 3s"
    assert run.commit is None
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled" and "harvest" not in turn
    (line,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert line["agent"] == "agent-a" and "harvest" not in line


def test_a_terminal_harvest_skips_the_commit_when_the_branch_has_two(tmp_path):
    """Two commits, the claim naming the older of the two: the verdict is REWORK
    and there is no single commit to point at, so `run.commit` stays None."""
    def two_commits(directory):
        work_ready(directory, "")
        _git(directory, "commit", "-q", "--allow-empty", "-m", "KC-21: second")

    sb, fake, _h, run, _ = _run_one(tmp_path,
        {"turns": [{"events": [], "idle": False}]}, _stall_config(),
        prepare=two_commits)
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED
    assert run.commit is None
    (turn,) = run.turns
    assert turn["harvest"]["verdict"] == "REWORK"
    assert "commits_ne_1" in turn["harvest"]["reasons"]
    assert _git(ws.path, "rev-list", "--count", f"{ws.base_sha}..HEAD") == "2"


def test_a_terminal_harvest_takes_its_commit_from_the_verdict(tmp_path, monkeypatch):
    """KC-30: the runner derives no sha of its own — the branch's one commit is
    the harvest's to set, so nothing here asks git for HEAD. That is the three
    lines five round-60 entries each had to write beside the harvest call, and
    the next caller was to forget them: `verdict.commit` is what reaches
    `run.commit` and the patch the operator reads."""
    import tools.contest.runner as runner_mod

    asks: list[tuple[str, ...]] = []
    real = runner_mod.git

    def git_no_head(cwd, *args, **kwargs):
        asks.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "git", git_no_head)
    sb, fake, _h, run, _ = _run_one(tmp_path,
        {"turns": [{"events": [], "idle": False}]}, _stall_config(),
        prepare=lambda d: work_no_claim(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, run.last_error
    assert run.commit == _branch_sha(ws)
    assert ("rev-parse", "HEAD") not in asks


def test_a_terminal_harvest_runs_the_roots_under_the_rounds_lock(tmp_path, monkeypatch):
    """`run_tests=True`: the stall's harvest is the round's harvest — the pytest
    roots run in that worktree, through the same `_harvest` that serialises them
    round-wide, and the stall still settles the run READY."""
    import tools.contest.harvest as harvest_module

    sb = Sandbox(tmp_path)
    seen: list[tuple[str, str]] = []

    def roots(cwd, **kwargs):
        # KC-60: the roots run in a detached checkout of the commit, not the tree.
        seen.append((cwd, _git(cwd, "rev-parse", "HEAD")))
        return ALL_ROOTS_PASS, []

    monkeypatch.setattr(harvest_module, "run_tests_detail", roots)
    # FL-1 (round 84): committed before the round, not from the turn's hook —
    # see `_run_one`. This one goes through `run_round`, so it does it by hand.
    work_ready(str(sb.ws("agent-a").path), "")
    scenario = {"turns": [{"events": [], "idle": False}]}
    with _BenchFake(scenario) as fake:
        state = run_round(_stall_config(), ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                          run_tests=True)
    (run,) = state.agents
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _branch_sha(sb.ws("agent-a"))
    assert seen[0][0] != str(sb.ws("agent-a").path)      # not the agent's tree
    assert seen[0][1] == _branch_sha(sb.ws("agent-a"))   # the commit it scores
    assert run.turns[0]["harvest"]["verdict"] == "READY"
    assert run.turns[0]["harvest"]["reasons"] == []
    assert isinstance(run.turns[0]["harvest"]["elapsed"], float)


# ─────────────────────────────────────────────────────────────────────────────
# KC-29: a stall the runner asked for keeps STALLED — the work is exported,
# never promoted
# ─────────────────────────────────────────────────────────────────────────────

def test_a_runner_asked_stall_harvests_the_commit_but_never_promotes(tmp_path, caplog):
    """The work is committed and claimed before the turn, and the turn asks
    three questions: the runner's own stall edge, not the session's. The
    harvest still runs — the work is exported, `run.commit` is the branch's
    sha, the turn carries the READY verdict — but the run is not READY: it
    keeps STALLED, `last_error` is the stall's reason alone, and the KC-18
    line names both the stall and the verdict."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    scenario = {"turns": [{"events": ["busy"], "questions": 3}]}
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario,
                                          prepare=lambda d: work_ready(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, (run.state, run.last_error)
    assert run.last_error == "3 questions in one turn"
    assert run.commit == _branch_sha(ws)
    assert aborted
    assert run.questions == 3
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "READY"
    assert turn["harvest"]["reasons"] == []
    assert _runner_has(caplog, "agent-a: STALLED — 3 questions in one turn (harvest: READY)")
    (line,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert line["idle_status"] == "stalled" and line["harvest"]["verdict"] == "READY"


def test_a_runner_asked_stall_with_no_commit_keeps_the_no_harvest_path(tmp_path, monkeypatch):
    """The same turn, a branch with nothing under it: KC-21's byte-for-byte
    path — STALLED, `commit` is None, no `harvest` key on the turn, and the
    harvest is never called."""
    def boom(*args, **kwargs):
        raise AssertionError("_harvest must not run for a branch with no commit")

    monkeypatch.setattr("tools.contest.runner._harvest", boom)
    scenario = {"turns": [{"events": ["busy"], "questions": 3}]}
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario)
    assert run.state is AgentState.STALLED and aborted
    assert run.last_error == "3 questions in one turn"
    assert run.commit is None
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled" and "harvest" not in turn
    (line,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert line["agent"] == "agent-a" and "harvest" not in line


def test_a_runner_asked_stall_with_a_rejected_commit_keeps_stalled_and_its_reason(tmp_path, caplog):
    """One commit the harvest rejects (no claim): STALLED, `last_error` is
    the stall's reason unchanged by the verdict, `run.commit` is the branch's
    sha, and the KC-18 line names the verdict after the reason."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    scenario = {"turns": [{"events": ["busy"], "questions": 3}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario,
                                     prepare=lambda d: work_no_claim(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, run.last_error
    assert run.last_error == "3 questions in one turn"
    assert run.commit == _branch_sha(ws)
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "REWORK"
    assert turn["harvest"]["reasons"] == ["no_progress_row"]
    assert _runner_has(caplog, "agent-a: STALLED — 3 questions in one turn (harvest: REWORK)")


def test_a_session_that_ended_on_its_own_keeps_the_kc21_line_verbatim(tmp_path, caplog):
    """KC-21, unchanged by KC-29: a turn that died on its own — the silence
    window — with a READY harvest finishes READY, and its KC-18 line is the
    KC-21 one verbatim: `<sha12> after <the error>`, no verdict suffix, no
    stall named. Only a stall the runner asked for carries the suffix."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    cfg = _stall_config()
    scenario = {"turns": [{"events": [], "idle": False}]}
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario, cfg,
                                          prepare=lambda d: work_ready(d, ""))
    ws = sb.ws("agent-a")
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _branch_sha(ws)
    assert run.last_error is None
    lines = _runner_lines(caplog)
    assert f"agent-a: READY — {run.commit[:12]} after no event for 3s" in lines
    assert not any("(harvest:" in line for line in lines)


# ─────────────────────────────────────────────────────────────────────────────
# KC-57: a harvest has a wall-clock budget, and the queue is visible
# ─────────────────────────────────────────────────────────────────────────────

class _FakeClock:
    """`time.monotonic` on a clock the test advances; nothing else is touched.

    `_TestRunsLock` is the only thing in `_harvest` that reads the clock, so the
    wait is exact: no real sleep, no margin, no bet on a loaded box. `threading`
    binds its own `monotonic` at import, so `Event.wait` and `join` keep the
    real one.
    """

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_the_test_runs_lock_reports_the_holder_and_clears_it(monkeypatch):
    """`enter` reports the wait, `status` names the holder and its own time,
    `ahead` sees the holder from an asker's side, and `exit` clears both."""
    clock = _FakeClock()
    monkeypatch.setattr(_runner_module, "time", clock)
    lock = _runner_module._TestRunsLock()

    assert lock.status("agent-a") is None and lock.ahead("agent-a") == 0
    assert lock.enter("agent-a") == 0.0            # nothing was in front
    assert lock.status("agent-a") == ("running", 0.0, 0)
    assert lock.ahead("agent-b") == 1              # the holder is ahead of an asker

    clock.advance(60.0)
    assert lock.status("agent-a") == ("running", 60.0, 0)   # the holder's own time

    lock.exit("agent-a")
    assert lock.status("agent-a") is None
    assert lock.ahead("agent-b") == 0
    assert lock.status("") is None


def _queue_state(lock, agent: str, deadline: float) -> tuple | None:
    """`lock.status(agent)`, polled until it is not None — else None on the deadline.

    The asker is not the one that can tell us it asked: it is inside `enter`.
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        status = lock.status(agent)
        if status is not None:
            return status
        time.sleep(0.01)
    return None


def test_harvests_in_series_record_the_wait_and_who_is_ahead(tmp_path, monkeypatch):
    """KC-57 §2 acceptance: three agents on the one pytest slot, patched clock, no
    real sleep. The first one's `waited` is 0 and nobody is ahead of it; the
    second one's `waited` is at least the first one's run time and one is ahead;
    the third stands two ahead. All three see the config's budget."""
    import tools.contest.harvest as harvest_module
    import tools.contest.runner as runner_mod

    names = ("agent-a", "agent-b", "agent-c")
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, harvest_budget_sec=900)
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod, "time", clock)
    lock = runner_mod._TEST_RUNS_LOCK

    seen: dict = {}
    results: dict = {}
    first_inside = threading.Event()
    released = threading.Event()

    def slow(ws, ticket_path, *, run_tests=False, budget_sec=0.0, waited=0.0, ahead=0):
        seen[ws.agent] = {"waited": waited, "ahead": ahead, "budget": budget_sec}
        if ws.agent == names[0]:
            first_inside.set()
        released.wait(30.0)
        return harvest_module.Harvest(verdict="REWORK", reasons=(), commit=None, facts={},
                                      elapsed=0.01, waited=waited, ahead=ahead)

    monkeypatch.setattr(runner_mod, "harvest", slow)

    def run_harvest(name: str):
        results[name] = runner_mod._harvest(sb.ws(name), sb.ticket_path, True, cfg)

    threads = [threading.Thread(target=run_harvest, args=(name,), daemon=True) for name in names]
    threads[0].start()
    assert first_inside.wait(30.0), "the first harvest never got inside the lock"

    threads[1].start()
    queued = _queue_state(lock, names[1], 30.0)
    assert queued and queued[0] == "queued" and queued[2] == 1     # the holder only

    threads[2].start()
    queued = _queue_state(lock, names[2], 30.0)
    assert queued and queued[0] == "queued" and queued[2] == 2     # holder, plus the one before

    clock.advance(300.0)                 # the first one's run time, on a patched clock
    released.set()
    for thread in threads:
        thread.join(30.0)
    assert not any(t.is_alive() for t in threads)

    assert seen[names[0]] == {"waited": 0.0, "ahead": 0, "budget": 900.0}
    assert seen[names[1]]["ahead"] == 1 and seen[names[1]]["waited"] >= 300.0
    assert seen[names[2]]["ahead"] == 2 and seen[names[2]]["waited"] >= 300.0
    for name in names:
        assert seen[name]["budget"] == 900.0
        assert results[name].waited == seen[name]["waited"]
        assert results[name].ahead == seen[name]["ahead"]
        assert lock.status(name) is None


def test_a_missing_lock_is_no_queue_to_stand_in(tmp_path, monkeypatch):
    """Fail-open: no `_TEST_RUNS_LOCK` at all is "no queue", so the roots still
    run, the harvest ends, and `waited` is 0 rather than an exception in the run."""
    import tools.contest.harvest as harvest_module
    import tools.contest.runner as runner_mod

    sb = Sandbox(tmp_path, ["agent-a"])
    monkeypatch.setattr(runner_mod, "_TEST_RUNS_LOCK", None)
    got: dict = {}

    def roots(ws, ticket_path, *, run_tests=False, budget_sec=0.0, waited=0.0, ahead=0):
        got.update(run_tests=run_tests, budget=budget_sec, waited=waited, ahead=ahead)
        return harvest_module.Harvest(verdict="READY", reasons=(), commit=None, facts={},
                                      elapsed=0.01, waited=waited, ahead=ahead)

    monkeypatch.setattr(runner_mod, "harvest", roots)

    runner_mod._harvest(sb.ws("agent-a"), sb.ticket_path, True, None)
    assert got == {"run_tests": True, "budget": 0.0, "waited": 0.0, "ahead": 0}


def test_the_budget_comes_from_the_config_and_gives_up():
    """`_budget_left` reads `harvest_budget_sec`, defaults to 900, and gives up
    to no budget on a missing, negative or malformed value rather than raising
    into a run."""
    assert _runner_module._budget_left(make_config(["agent-a"])) == 900.0
    assert _runner_module._budget_left(make_config(["agent-a"], harvest_budget_sec=0)) == 0.0
    assert _runner_module._budget_left(make_config(["agent-a"], harvest_budget_sec=-5)) == 0.0
    assert _runner_module._budget_left(None) == 0.0
    bad = type("Bad", (), {"harvest_budget_sec": "soon"})()
    assert _runner_module._budget_left(bad) == 0.0


class _FakeTestLock:
    """`_TEST_RUNS_LOCK.status` off a table the test writes — no threads."""

    def __init__(self, table: dict):
        self.table = table

    def status(self, agent: str = "") -> tuple | None:
        return self.table.get(agent)


def test_the_heartbeat_names_the_queue_and_the_runner(tmp_path, monkeypatch):
    """KC-57 §2 acceptance: a HARVESTING agent standing behind the round's one
    pytest slot reads `queued` with its wait and who is ahead of it; the one
    inside reads `tests`."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"])
    rm = _runner_module
    runs = [
        AgentRun(agent=cfg.agents[0], workspace=sb.ws("agent-a"), state=AgentState.HARVESTING),
        AgentRun(agent=cfg.agents[1], workspace=sb.ws("agent-b"), state=AgentState.HARVESTING),
    ]
    hb, rm, files, commits = _make_heartbeat(runs, {"agent-a": 3, "agent-b": 3})
    monkeypatch.setattr(rm, "_TEST_RUNS_LOCK", _FakeTestLock({
        "agent-a": ("running", 180.0, 0),
        "agent-b": ("queued", 720.0, 2),
    }))
    try:
        line = hb.line()
    finally:
        rm._worktree_files, rm._commits_above = files, commits
    a, b = _part(line, "agent-a"), _part(line, "agent-b")
    assert "HARVESTING" in a and "(tests 3m)" in a
    assert "HARVESTING" in b and "(queued 12m, 2 ahead)" in b


def test_the_rounds_table_totals_each_agents_test_lock_wait(tmp_path):
    """The round's final table carries the total lock wait per agent, summed
    over its turns; a turn written before `waited` existed counts as 0."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"])
    state = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha,
                       started_at=time.time() - 120, agents=[
        AgentRun(agent=cfg.agents[0], workspace=sb.ws("agent-a"), state=AgentState.HARVESTING,
                 turns=[
                     {"kind": "initial",
                      "harvest": {"verdict": "REWORK", "reasons": ["tests_slow"],
                                  "elapsed": 900.0, "waited": 720.5}},
                     {"kind": "rework",
                      "harvest": {"verdict": "READY", "reasons": [],
                                  "elapsed": 300.0, "waited": 60.0}},
                 ]),
        AgentRun(agent=cfg.agents[1], workspace=sb.ws("agent-b"), state=AgentState.READY,
                 turns=[{"kind": "initial",
                         "harvest": {"verdict": "READY", "reasons": [], "elapsed": 200.0}}]),
    ])

    rows = state.table_rows()
    assert rows[0]["test_wait"] == 780.5
    assert rows[1]["test_wait"] == 0.0
    assert set(rows[0]) == set(rows[1])


# ─────────────────────────────────────────────────────────────────────────────
# KC-22: a turn that ends idle with uncommitted work gets a continue, not a rework
# ─────────────────────────────────────────────────────────────────────────────

def _harvest_calls(monkeypatch):
    """Monkeypatch `_harvest` with a counting wrapper; return the counter list."""
    import tools.contest.runner as runner_mod

    seen = []
    real = runner_mod._harvest

    def wrap(ws, ticket_path, run_tests, config=None):
        seen.append(1)
        return real(ws, ticket_path, run_tests, config=config)

    monkeypatch.setattr("tools.contest.runner._harvest", wrap)
    return seen


def test_idle_with_uncommitted_work_continues_then_commits_ready(tmp_path, monkeypatch):
    """Turn 0 edits and goes idle without committing; turn 1 commits the entry
    and writes the row → READY, `run.attempt == 0`, the turns are
    `['initial', 'continue']`, the second prompt carries the edited file's name
    and the word `uncommitted`, and `_harvest` ran exactly once."""
    counts = _harvest_calls(monkeypatch)
    scenario = {"turns": [
        {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    (sid0, first), (_sid1, second) = _prompts(fake)
    assert "pkg/thing.py" in second and "uncommitted" in second
    assert len(counts) == 1


def test_idle_edit_never_commit_exhausts_continues_then_gives_up(tmp_path, monkeypatch):
    """Edits and never commits, `max_continues_per_attempt = 1`,
    `max_rework = 1`: turns are `initial, continue, rework, continue` and the
    run ends GAVE_UP; the harvest ran twice."""
    counts = _harvest_calls(monkeypatch)
    scenario = {"turns": [
        {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
    ]}
    cfg = make_config(["agent-a"], max_continues_per_attempt=1, max_rework=1)
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.GAVE_UP
    assert [t["kind"] for t in run.turns] == ["initial", "continue", "rework", "continue"]
    assert run.attempt == 1
    assert len(counts) == 2


def test_idle_on_a_clean_tree_is_harvested_at_once(tmp_path):
    """A turn that goes idle on a clean tree is NOT a continue: it is harvested
    at once (the first turn carries the REWORK verdict) and the run never sees a
    `continue` turn. `max_rework = 0` so the single harvest settles the run
    without the fake looping into more turns."""
    cfg = make_config(["agent-a"], max_rework=0)
    scenario = {"turns": [{"events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.GAVE_UP
    assert run.turns[0]["kind"] == "initial"
    assert run.turns[0]["harvest"]["verdict"] == "REWORK"
    assert [t["kind"] for t in run.turns if t["kind"] == "continue"] == []


def test_max_continues_zero_harvests_a_dirty_idle_turn_at_once(tmp_path):
    """`max_continues_per_attempt = 0` disables the mechanism: a dirty idle turn
    is harvested at once, exactly as before this ticket — no `continue` turn."""
    scenario = {"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]}]}
    cfg = make_config(["agent-a"], max_continues_per_attempt=0, max_rework=0)
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.GAVE_UP
    assert run.turns[0]["kind"] == "initial"
    assert run.turns[0]["harvest"]["verdict"] == "REWORK"
    assert [t["kind"] for t in run.turns if t["kind"] == "continue"] == []


def test_resume_into_a_dirty_worktree_first_prompt_names_it(tmp_path):
    """`--resume` with a dirty non-READY worktree: the new session's first prompt
    contains the `round_prompt` text AND the dirty file's name; a clean one gets
    the unchanged `round_prompt`."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2)
    _write(sb.ws("agent-b").path / "pkg" / "thing.py", "def thing():\n    return 99\n")
    prior = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha, started_at=1.0, agents=[
        AgentRun(agent=cfg.agents[0], workspace=sb.ws("agent-a"), state=AgentState.READY, commit="0" * 40),
        AgentRun(agent=cfg.agents[1], workspace=sb.ws("agent-b"), state=AgentState.WAITING, session_id="ses_gone"),
    ])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake:
        state = _round(sb, fake, cfg, resume=prior)
        prompts = _prompts(fake)
    runs = _by_name(state)
    _assert_ready(runs["agent-b"], sb.ws("agent-b"))
    b_session = fake.sessions()[-1].id
    b_prompts = [text for sid, text in prompts if sid == b_session]
    assert len(b_prompts) == 1
    assert "runs/agent-b/PROGRESS.csv" in b_prompts[0]   # round_prompt text present
    assert "pkg/thing.py" in b_prompts[0]                # dirty file named

    # a clean mid-flight worktree gets the unchanged round_prompt
    sb2 = Sandbox(tmp_path / "clean", ["agent-a", "agent-b"])
    cfg2 = make_config(["agent-a", "agent-b"], max_parallel=2)
    prior2 = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb2.base_sha, started_at=1.0, agents=[
        AgentRun(agent=cfg2.agents[0], workspace=sb2.ws("agent-a"), state=AgentState.READY, commit="0" * 40),
        AgentRun(agent=cfg2.agents[1], workspace=sb2.ws("agent-b"), state=AgentState.WAITING, session_id="ses_gone"),
    ])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake2:
        state2 = _round(sb2, fake2, cfg2, resume=prior2)
        prompts2 = _prompts(fake2)
    _assert_ready(_by_name(state2)["agent-b"], sb2.ws("agent-b"))
    b_session2 = fake2.sessions()[-1].id
    b_prompts2 = [text for sid, text in prompts2 if sid == b_session2]
    assert len(b_prompts2) == 1
    assert "uncommitted" not in b_prompts2[0]


# ─────────────────────────────────────────────────────────────────────────────
# KC-19: retryable session errors — bounded retry into the same session
# ─────────────────────────────────────────────────────────────────────────────

_ECONNRESET = {"name": "APIError", "data": {"message": "Connection reset by server",
               "isRetryable": True, "metadata": {"code": "ECONNRESET"}}}


def _make_retry_config(**over) -> ContestConfig:
    # FL-1 (round 84, round 7): 300 s, not 30. These turns commit for real and
    # none of these tests is about the turn deadline — see make_config.
    kw = dict(max_error_retries=2, error_retry_backoff_sec=0,
              turn_timeout_sec=300, idle_event_timeout_sec=60)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def test_retryable_error_reprompts_same_session_and_recovers(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    """Turn 1 errors with ECONNRESET, turn 2 answers with work_ready → READY,
    attempt == 0, kinds are ['initial', 'retry'], one POST /session, second
    prompt text contains 'dropped the connection' and 'Connection reset by
    server'."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config()
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.READY
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert run.turns[0]["idle_status"] == "error"
    assert run.turns[0]["idle_at"] >= run.turns[0]["sent_at"]
    prompts = _prompts(fake)
    assert len(prompts) == 2
    sid1, _ = prompts[0]
    sid2, retry_text = prompts[1]
    assert sid1 == sid2
    assert "dropped the connection" in retry_text
    assert "Connection reset by server" in retry_text
    # the INFO line names the cause as `data.message`, not the payload's JSON
    assert "agent-a: retry 1/2 in 0s — Connection reset by server" in [
        r.getMessage() for r in caplog.records if r.name == "tools.contest.runner"]
    assert len(_session_posts(fake)) == 1


def test_retry_backoff_is_observed(tmp_path):
    """error_retry_backoff_sec=1: the retry prompt goes out >= 1 s after the
    error event, read off the run's own turn timestamps.

    FL-1 (round 84): the old upper bound was `backoff + 10` measured around
    the whole of `_run_one` — which includes building the sandbox (real git
    worktrees) and two full turns. On the operator's 32-worker box that
    setup alone can eat the 10 s, so the bound was a box number, not a
    deadline. The backoff itself is what this test is about, and the turns
    record exactly when it started and ended."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config(error_retry_backoff_sec=1)
    started = time.monotonic()
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    elapsed = time.monotonic() - started
    assert run.state is AgentState.READY
    # the backoff was observed, measured where it actually happened: from the
    # errored turn going idle to the retry prompt going out.
    error_turn, retry_turn = run.turns
    assert retry_turn["sent_at"] - error_turn["idle_at"] >= cfg.error_retry_backoff_sec
    # No upper bound on `elapsed`. It measures the whole of `_run_one` —
    # building the sandbox out of real git worktrees, two full turns, the
    # harvest — and none of that is the backoff. The stress run measured 78 s
    # for it on a doubly-loaded box while the assertion above, which is the
    # actual claim, was true. `backoff + 2 * turn_timeout` looked like a
    # deadline and was a box number in disguise.


def test_retries_exhausted_then_error(tmp_path):
    """max_error_retries=1, two retryable errors in a row → ERROR, last_error
    starts with 'after 1 retries: session.error:', turns are
    ['initial', 'retry']."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"events": ["busy"], "error": _ECONNRESET},
    ]}
    cfg = _make_retry_config(max_error_retries=1)
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 1 retries: session.error:")
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert len(_prompts(fake)) == 2


def test_max_error_retries_zero_means_no_retry(tmp_path):
    """max_error_retries=0 with a retryable error → ERROR after one turn, no
    second prompt (today's behaviour)."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
    ]}
    cfg = _make_retry_config(max_error_retries=0)
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.ERROR
    assert len(_prompts(fake)) == 1
    assert len(run.turns) == 1


def test_non_retryable_error_is_not_retried(tmp_path):
    """A non-retryable payload → ERROR after one turn, no second prompt, even
    with retries left."""
    scenario = {"turns": [
        {"events": ["busy"],
         "error": {"name": "UnknownError",
                   "data": {"message": "Model not found: kenary/x"}}},
    ]}
    cfg = _make_retry_config()
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.ERROR
    assert len(_prompts(fake)) == 1
    assert len(run.turns) == 1


def test_502_message_is_retryable(tmp_path):
    """'502 Bad Gateway' without isRetryable → retried by the message rule."""
    scenario = {"turns": [
        {"events": ["busy"],
         "error": {"name": "APIError", "data": {"message": "502 Bad Gateway"}}},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config()
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.READY
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert len(_prompts(fake)) == 2


def test_session_error_is_error_with_the_payload_unchanged(tmp_path):
    """The existing ProviderError/boom-42 test: not retryable, ERROR."""
    scenario = {"turns": [{"events": ["busy"],
                           "error": {"name": "ProviderError", "message": "boom-42"}}]}
    sb, _fake, _h, run, _ = _run_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert "boom-42" in run.last_error
    assert run.turns[0]["idle_status"] == "error"
    assert (sb.out_dir / "agent-a.session.json").is_file()


def test_retry_does_not_increment_attempt(tmp_path):
    """A retry is not a rework: attempt stays 0."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config()
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.attempt == 0
    assert run.turns[1]["attempt"] == 0


@pytest.fixture
def sigint_raises_keyboardinterrupt():
    """Make SIGINT raise KeyboardInterrupt for the length of a test that sends one to itself.

    A process started with SIGINT ignored — a background job (`cmd &`), `nohup`, a CI
    runner — passes that on to Python, which then never installs its own handler: the
    signal is swallowed and `pytest.raises(KeyboardInterrupt)` fails with "DID NOT
    RAISE" though nothing is wrong with the code under test. xdist workers inherit the
    controller's setting, so `-n` does not help. The previous handler is put back.
    """
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)


@pytest.mark.usefixtures("sigint_raises_keyboardinterrupt")
def test_ctrl_c_during_retry_backoff_ends_the_round(tmp_path):
    """SIGINT during the backoff wait ends the round well before the backoff
    itself would (a 60 s wait, cut short by the interrupt)."""
    sb = Sandbox(tmp_path)
    cfg = _make_retry_config(error_retry_backoff_sec=60)
    pid = os.getpid()

    def on_error(directory, text):
        threading.Timer(0.5, os.kill, args=[pid, signal.SIGINT]).start()

    scenario = {"turns": [
        {"on_prompt": on_error, "events": ["busy"],
         "error": _ECONNRESET},
    ]}
    started = time.monotonic()
    with _BenchFake(scenario) as fake:
        with pytest.raises(KeyboardInterrupt):
            run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                      make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir)
        elapsed = time.monotonic() - started
    # the interrupt cut the 60 s backoff short; any loaded box still lands well
    # under the full wait, and a regression that ignored SIGINT would sit here
    # for the whole backoff.
    assert elapsed < cfg.error_retry_backoff_sec


# ─────────────────────────────────────────────────────────────────────────────
# KC-45: a dropped response stream is a retryable error, not an ended agent
# ─────────────────────────────────────────────────────────────────────────────

#: The round 86 payload, quoting included — Kilo wraps the provider's message in
#: a literal pair of quotes, and it carries no `isRetryable` and no `metadata`.
_INTERRUPTED_STREAM = {"name": "UnknownError",
                       "data": {"message": "\"the model's provider interrupted the response stream\""}}
#: The round 92 `agnes-3-0-flash` payload: the same drop in other words, which
#: the round 86 wording did not match — it ended the agent ERROR after one turn.
_INTERRUPTED_BEFORE_FINISH = {
    "name": "UnknownError",
    "data": {"message": "\"the model's provider interrupted the response before it finished. "
                        "send the request again as a new request\""}}
#: Kilo's text for the same class on the stream log, which never reached
#: `session.error` in round 86.
_UPSTREAM_UNAVAILABLE = {"name": "UnknownError",
                         "data": {"message": "upstream unavailable for model x"}}
#: The round 86 `nex-n2-5-pro` payload: refused on its very first request,
#: before any tool call.
_REJECTED_REQUEST = {"name": "UnknownError",
                     "data": {"message": "the model's provider rejected the request. "
                                          "check the model id, request fields, and context length"}}


@pytest.mark.parametrize("payload", [_INTERRUPTED_STREAM, _INTERRUPTED_BEFORE_FINISH,
                                     _UPSTREAM_UNAVAILABLE])
def test_a_dropped_response_stream_is_retryable(payload):
    """§1: every dropped-connection message is retryable from the message
    alone — no status code, no `isRetryable`, no `metadata`."""
    assert _retryable(payload) is True, payload


@pytest.mark.parametrize("payload", [
    {"name": "UnknownError", "data": {"message": "Model not found: kenary/x"}},
    {"name": "APIError", "data": {"message": "This model's maximum context length is 128000 tokens"}},
    {"name": "ProviderError", "message": "boom-42"},
    {"name": "APIError", "data": {"message": "x", "isRetryable": False}},
    {},
    "Connection reset by server",
    None,
    42,
    ["ECONNRESET"],
])
def test_the_kc19_not_retryable_shapes_stay_not_retryable(payload):
    """KC-19's list is unchanged, so a later looser pattern (`provider`,
    `stream`) cannot swallow them."""
    assert _retryable(payload) is False, payload


def test_provider_rejected_request_is_not_retryable_without_a_finished_reply():
    """§2: the pin. A refusal on the session's first call is refused again when
    resent, and a count that is not a count fails open to that answer."""
    assert _retryable(_REJECTED_REQUEST) is False
    for junk in (None, "1", [], {}, True, 1.5):
        assert _retryable(_REJECTED_REQUEST, junk) is False, junk


def test_provider_rejected_request_is_retryable_after_a_finished_reply():
    """§2a: the same payload once the session has had an answer is the free tier
    refusing under load — the 429 class, retryable under KC-19's budget."""
    assert _retryable(_REJECTED_REQUEST, 1) is True
    assert _retryable(_REJECTED_REQUEST, 5) is True


#: Round 104: the `vercel_8080` gateway's answer after ~30 min of work.
_GATEWAY_403 = {"name": "APIError", "data": {
    "message": "Forbidden: request was blocked by a gateway or proxy. You may not "
               "have permission to access this resource — check your account and "
               "provider settings.",
    "statusCode": 403, "isRetryable": False}}


def test_a_gateway_403_is_retried_only_after_a_finished_reply():
    """Round 104: the gateway's 403 is §2/§2a's class — final on the first
    call, the free tier under load once the session has answered."""
    assert _retryable(_GATEWAY_403) is False
    assert _retryable(_GATEWAY_403, 1) is True


def test_retry_backoff_doubles_up_to_the_cap_then_stays():
    """15, 30, then a minute apart; a cap of 0 keeps today's doubling."""
    assert [_retry_backoff(n, 15, 60) for n in range(1, 7)] == [15, 30, 60, 60, 60, 60]
    assert [_retry_backoff(n, 15, 0) for n in range(1, 5)] == [15, 30, 60, 120]
    assert [_retry_backoff(n, 15, None) for n in range(1, 4)] == [15, 30, 60]
    assert sum(_retry_backoff(n, 15, 60) for n in range(1, 31)) == 1725
    assert _retry_backoff(3, 0, 60) == 0


def test_a_good_turn_resets_the_retry_budget(tmp_path):
    """The budget counts errors in a row: with a budget of 1, an error, a good
    turn, then another error is retried again, not the end of the agent."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"events": ["busy", "idle"]},
        {"events": ["busy"], "error": _ECONNRESET},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config(max_error_retries=1)
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.READY
    assert [t["kind"] for t in run.turns].count("retry") == 2


def test_agent_max_sec_ends_the_agent_between_turns(tmp_path):
    """The agent's hard limit fires during a retry's backoff: no new prompt,
    the agent ends STALLED "time up"."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _ECONNRESET},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    cfg = _make_retry_config(error_retry_backoff_sec=5, agent_max_sec=1)
    sb, fake, h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.STALLED
    assert run.last_error.startswith("time up:")
    assert len(_prompts(fake)) == 1


class _Transcript:
    """A backend stand-in for `_finished_replies`: `messages()` returns or
    raises."""

    def __init__(self, messages=None, error=None):
        self._messages, self._error = messages, error

    def messages(self, session):
        if self._error is not None:
            raise self._error
        return self._messages


def test_finished_replies_counts_the_answers_that_finished():
    """§2a: one per assistant message with a `finish` — any finish, not only
    `stop`. A transcript that cannot be read, is not a list, or holds no
    assistant reply is 0."""
    tool_calls = {"info": {"role": "assistant", "finish": "tool-calls"}, "parts": []}
    length = {"info": {"role": "assistant", "finish": "length"}, "parts": []}
    open_reply = {"info": {"role": "assistant"}, "parts": []}
    # the reply Kilo writes for the refused call itself (round 88,
    # nemotron-3-super-120b-a12b): an error and no `finish` — not an answer
    refused = {"info": {"role": "assistant", "error": {"name": "UnknownError"}}, "parts": []}
    user = {"info": {"role": "user"}, "parts": []}
    assert _finished_replies(_Transcript([user, tool_calls]), None) == 1
    assert _finished_replies(_Transcript([tool_calls, length]), None) == 2
    assert _finished_replies(_Transcript([open_reply]), None) == 0
    assert _finished_replies(_Transcript([refused]), None) == 0
    assert _finished_replies(_Transcript([tool_calls, refused]), None) == 1
    assert _finished_replies(_Transcript([user]), None) == 0
    assert _finished_replies(_Transcript([]), None) == 0
    assert _finished_replies(_Transcript({"not": "a list"}), None) == 0
    assert _finished_replies(_Transcript(error=RuntimeError("GET /message: 500")), None) == 0
    assert _finished_replies(_Transcript([None, "junk", {"info": "junk"}]), None) == 0


def test_retry_reason_drops_the_surrounding_quotes():
    """§3: the reason is the message without Kilo's extra `"…"`, so
    `RETRY_PROMPT` reads as a sentence instead of a sentence inside a quote."""
    reason = _retry_reason(_INTERRUPTED_STREAM)
    assert not reason.startswith('"') and not reason.endswith('"')
    assert reason == "the model's provider interrupted the response stream"
    # one layer only, and only when both ends are the same quote
    assert _retry_reason({"name": "APIError", "data": {"message": "502 Bad Gateway"}}) == "502 Bad Gateway"
    assert _retry_reason({"name": "APIError", "data": {"message": "'502 Bad Gateway'"}}) == "502 Bad Gateway"
    assert _retry_reason("Connection reset by server") == "Connection reset by server"
    assert _retry_reason({"name": "APIError", "data": {"message": 'a "quoted" word'}}) == 'a "quoted" word'


def _run_finished_one(tmp_path, scenario, config=None):
    """`_run_one` against :class:`_FinishedFake`, so the session opens with one
    assistant reply that already finished (KC-45 §2a)."""
    sb = Sandbox(tmp_path)
    config = config or _make_retry_config()
    with _FinishedFake(scenario) as fake:
        h = Harness(sb, fake, config)
        run = h.go()
    return sb, fake, h, run, _aborted(fake)


def _assert_retried_in_the_same_session(run, fake, reason):
    """KC-45's runner contract: one session, one prompt on it, and a retry whose
    text is `RETRY_PROMPT` naming *reason* — never an `ERROR` after one turn."""
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert run.turns[0]["idle_status"] == "error"
    assert len(_session_posts(fake)) == 1
    prompts = _prompts(fake)
    assert len(prompts) == 2
    (sid1, _), (sid2, retry_text) = prompts
    assert sid1 == sid2
    assert "dropped the connection" in retry_text and reason in retry_text
    return retry_text


def test_interrupted_stream_error_reprompts_same_session_and_recovers(tmp_path, caplog):
    """Acceptance 4: the round 86 payload on the first turn — no `isRetryable`,
    no `metadata`, the message wrapped in quotes. The agent is re-prompted with
    `RETRY_PROMPT` in the same session and ends READY, not ERROR after one turn."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    scenario = {"turns": [
        {"events": ["busy"], "error": _INTERRUPTED_STREAM},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _make_retry_config())
    retry_text = _assert_retried_in_the_same_session(
        run, fake, "the model's provider interrupted the response stream")
    # §3: the retry prompt carries the reason without Kilo's wrapping quotes
    assert '"the model' not in retry_text and 'stream"' not in retry_text
    assert "agent-a: retry 1/2 in 0s — the model's provider interrupted the response stream" in [
        r.getMessage() for r in caplog.records if r.name == "tools.contest.runner"]
    assert len(_jsonl(sb.out_dir / "agent-a" / "turns.jsonl")) == 2



def _work_ready_late(directory, text):
    """KC-63: the retry's work lands after the leftover idles are in the tap,
    so a wait that read one of them as its own end would see no commit."""
    time.sleep(0.5)
    work_ready(directory, text)


def test_a_retry_after_an_error_waits_for_its_own_idle(tmp_path):
    """KC-63, round 106's mimo-v2-5: a `session.error` followed by two
    `session.idle`. The retry ends on its own idle, records the two it
    skipped, and no `continue` is sent in between. Without the mark the retry
    ended in 0.15 s on the first leftover idle."""
    scenario = {"turns": [
        {"events": ["busy"], "error": dict(_INTERRUPTED_STREAM), "idles_after_error": 2},
        {"on_prompt": _work_ready_late, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _make_retry_config())
    _assert_ready(run, sb.ws("agent-a"))
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert run.turns[0]["idle_status"] == "error"
    assert "stale_events" not in run.turns[0]
    assert run.turns[1]["idle_status"] == "idle"
    assert run.turns[1]["stale_events"] == 2
    assert len(_prompts(fake)) == 2
    turns = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert [t.get("stale_events") for t in turns] == [None, 2]
    assert sum(1 for t in turns if t.get("harvest")) == 1


def test_upstream_unavailable_reprompts_same_session_and_recovers(tmp_path):
    """§1: Kilo's stream-log text is the same class once it reaches
    `session.error`, so it is retried too."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _UPSTREAM_UNAVAILABLE},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _make_retry_config())
    _assert_retried_in_the_same_session(run, fake, "upstream unavailable for model x")


def test_rejected_request_is_retried_after_a_finished_reply(tmp_path, caplog):
    """Acceptance §2a: the session has already had an assistant reply with
    `finish: "tool-calls"` — five steps done before the provider turned the
    request off. Retried with `RETRY_PROMPT` in the same session."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    scenario = {"turns": [
        {"events": ["busy"], "error": _REJECTED_REQUEST},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run_finished_one(tmp_path, scenario)
    _assert_retried_in_the_same_session(run, fake, "the model's provider rejected the request")
    logs = [r.getMessage() for r in caplog.records if r.name == "tools.contest.runner"]
    assert any(l.startswith("agent-a: retry 1/2 in 0s — the model's provider rejected the request")
               for l in logs), logs


def test_rejected_request_is_not_retried_on_the_first_call(tmp_path):
    """Acceptance §2: the same payload in a session with no finished reply is
    still ERROR after one turn — no second prompt, even with retries left."""
    scenario = {"turns": [{"events": ["busy"], "error": _REJECTED_REQUEST}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _make_retry_config())
    assert run.state is AgentState.ERROR
    assert len(_prompts(fake)) == 1
    assert len(run.turns) == 1
    assert "provider rejected the request" in run.last_error


def test_rejected_request_uses_the_kc19_retry_budget(tmp_path):
    """Acceptance §2a: the mid-session refusal spends KC-19's budget, it gets
    none of its own — two refusals with `max_error_retries=1` end ERROR with
    the `after 1 retries:` line."""
    scenario = {"turns": [
        {"events": ["busy"], "error": _REJECTED_REQUEST},
        {"events": ["busy"], "error": _REJECTED_REQUEST},
    ]}
    sb, fake, _h, run, _ = _run_finished_one(tmp_path, scenario,
                                             _make_retry_config(max_error_retries=1))
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 1 retries: session.error:")
    assert [t["kind"] for t in run.turns] == ["initial", "retry"]
    assert len(_prompts(fake)) == 2 and len(_session_posts(fake)) == 1


def test_rejected_request_is_not_retried_when_the_transcript_is_unreadable(tmp_path, monkeypatch):
    """Acceptance §2a: `messages()` raising while the count is decided is no
    finished reply — the refusal is treated as a first-call one, so the run
    ends ERROR instead of raising into the round."""
    def unreadable(self, session):
        raise RuntimeError("GET /message: 500")
    monkeypatch.setattr(KiloBackend, "messages", unreadable)
    scenario = {"turns": [{"events": ["busy"], "error": _REJECTED_REQUEST}]}
    sb, fake, _h, run, _ = _run_finished_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert len(_prompts(fake)) == 1
    assert len(run.turns) == 1


def test_the_transcript_is_read_for_a_rejected_request_only(tmp_path, monkeypatch):
    """§2a costs a `GET /message` only where the answer depends on it: a
    rejected request. Any other error — permanent or already retryable — is
    decided without reading the transcript."""
    reads = []
    real = _runner_module._finished_replies

    def counting(backend, session):
        reads.append(session)
        return real(backend, session)
    monkeypatch.setattr(_runner_module, "_finished_replies", counting)

    not_found = {"name": "APIError", "data": {"message": "Model not found: agent-a:free"}}
    _, _, _, run, _ = _run_finished_one(tmp_path / "a", {"turns": [
        {"events": ["busy"], "error": not_found}]})
    assert run.state is AgentState.ERROR and reads == []

    _, _, _, run, _ = _run_finished_one(tmp_path / "b", {"turns": [
        {"events": ["busy"], "error": _INTERRUPTED_STREAM},
        {"on_prompt": work_ready, "events": ["busy", "idle"]}]})
    assert run.state is AgentState.READY and reads == []

    _, _, _, run, _ = _run_finished_one(tmp_path / "c", {"turns": [
        {"events": ["busy"], "error": _REJECTED_REQUEST},
        {"on_prompt": work_ready, "events": ["busy", "idle"]}]})
    assert run.state is AgentState.READY and len(reads) == 1


# ─────────────────────────────────────────────────────────────────────────────
# KC-54: a context overflow — the work goes on in a fresh session
# ─────────────────────────────────────────────────────────────────────────────

#: The payload round 91's five agents sent back: the provider's `name`, the
#: message under `data`. `_is_overflow` must read it before the KC-19 retry
#: path sees it.
_OVERFLOW = {"name": "ContextOverflowError",
             "data": {"message": "the request exceeds the model's maximum context length"}}
#: The same overflow with the provider's retry flag set. The overflow check
#: still wins, because a prompt into the full session overflows again.
_RETRYABLE_OVERFLOW = {"name": "ContextOverflowError",
                       "data": {"message": "the request exceeds the model's maximum context length",
                                "isRetryable": True, "metadata": {"code": "ECONNRESET"}}}


def test_is_overflow_matches_the_name_and_both_message_spellings():
    assert _is_overflow({"name": "ContextOverflowError", "data": {"message": "..."}})
    assert _is_overflow({"name": "Other",
                         "data": {"message": "the request exceeds the model's maximum context length"}})
    assert _is_overflow("ContextOverflowError")
    assert _is_overflow({"name": "Other", "message": "context_length_exceeded"})
    # a `data.message` that says something else does not hide the top-level one
    assert _is_overflow({"name": "Other", "data": {"message": "bad request"},
                         "message": "context_length_exceeded"})


def test_is_overflow_is_false_for_none_empty_and_a_retryable_network_error():
    assert not _is_overflow(None)
    assert not _is_overflow({})
    assert not _is_overflow(_ECONNRESET)
    assert not _is_overflow("ECONNRESET")
    assert not _is_overflow({"name": "ProviderError", "message": "boom-42"})
    assert not _is_overflow(42)


def _run_overflow_one(tmp_path, scenario, config=None, prepare=None):
    """`_run_one` against :class:`_OverflowFake`, so a scenario may script the
    fresh session the overflow's uncommitted work continues in."""
    sb = Sandbox(tmp_path)
    if prepare is not None:
        prepare(str(sb.ws("agent-a").path))
    config = config or make_config(["agent-a"])
    with _OverflowFake(scenario) as fake:
        h = Harness(sb, fake, config)
        run = h.go()
    return sb, fake, h, run, _aborted(fake)


def test_an_overflow_turn_with_uncommitted_work_continues_in_a_fresh_session(tmp_path):
    """`session.error` names `ContextOverflowError` and the worktree holds an
    uncommitted file: a second `POST /session`, the next prompt goes to the
    *new* session and carries the round prompt plus the dirty lines, the
    overflowed turn keeps the old `session_id`, and `run.session_id` is the
    new one."""
    scenario = {
        "turns": [{"on_prompt": work_edit_no_commit, "events": ["busy"], "error": _OVERFLOW}],
        "turns_after": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
    }
    sb, fake, _h, run, _ = _run_overflow_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    assert len(_session_posts(fake)) == 2
    (old_session, fresh_session) = fake.sessions()
    assert run.session_id == fresh_session.id
    (sid1, first), (sid2, second) = _prompts(fake)
    assert sid1 == old_session.id and sid2 == fresh_session.id
    assert "pkg/thing.py" not in first
    assert "pkg/thing.py" in second and "uncommitted" in second
    assert "runs/agent-a/PROGRESS.csv" in second
    assert run.turns[0]["session_id"] == old_session.id
    assert "session_id" not in run.turns[1]
    (t0, t1) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["session_id"] == old_session.id and t0["idle_status"] == "error"
    assert "session_id" not in t1


def test_an_overflow_turn_with_a_retryable_flag_still_uses_a_fresh_session(tmp_path):
    """The provider flags the overflow retryable: the overflow check comes
    first, so the KC-19 retry path never fires — the second prompt is the round
    prompt into a *new* session, not `RETRY_PROMPT` into the full one."""
    cfg = _make_retry_config(max_continues_per_attempt=1)
    scenario = {
        "turns": [{"on_prompt": work_edit_no_commit, "events": ["busy"],
                   "error": _RETRYABLE_OVERFLOW}],
        "turns_after": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
    }
    sb, fake, _h, run, _ = _run_overflow_one(tmp_path, scenario, cfg)
    _assert_ready(run, sb.ws("agent-a"))
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    (sid1, _first), (sid2, second) = _prompts(fake)
    assert sid1 != sid2 and len(_session_posts(fake)) == 2
    assert "dropped the connection" not in second and "uncommitted" in second


def test_an_overflow_turn_with_a_clean_tree_is_a_stall_not_a_crash(tmp_path, monkeypatch):
    """Nothing uncommitted, nothing committed: the model spent its whole context
    reading and produced nothing — STALLED, not ERROR, with one `POST /session`
    and no harvest."""
    def boom(*args, **kwargs):
        raise AssertionError("_harvest must not run for an overflow with no commit")

    monkeypatch.setattr("tools.contest.runner._harvest", boom)
    scenario = {"turns": [{"events": ["busy"], "error": _OVERFLOW}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _stall_config())
    assert run.state is AgentState.STALLED
    assert run.last_error == "context overflow with no uncommitted work"
    assert run.commit is None and run.attempt == 0
    assert len(_session_posts(fake)) == 1 and len(_prompts(fake)) == 1
    (turn,) = run.turns
    assert turn["idle_status"] == "error" and "harvest" not in turn
    assert "session_id" not in turn


def test_an_overflow_turn_with_a_valid_commit_is_harvested_to_ready(tmp_path):
    """The model committed and claimed, then overflowed: the KC-21 harvest runs
    in place of a retry — READY, not STALLED, and no second session."""
    scenario = {"turns": [{"events": ["busy"], "error": _OVERFLOW}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _stall_config(),
                                    prepare=lambda d: work_ready(d, ""))
    _assert_ready(run, sb.ws("agent-a"))
    assert run.last_error is None
    assert len(_session_posts(fake)) == 1 and len(_prompts(fake)) == 1
    (turn,) = run.turns
    assert turn["idle_status"] == "error"
    assert turn["harvest"]["verdict"] == "READY"


def test_an_overflow_turn_exhausts_the_continue_budget_then_stalls(tmp_path):
    """`max_continues_per_attempt = 1`, two overflows with work still on disk:
    the first opens a fresh session, the second is a STALLED turn, and there
    are exactly two `POST /session` in total — the loop cannot run forever."""
    scenario = {"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy"],
                           "error": _OVERFLOW}]}
    cfg = make_config(["agent-a"], max_continues_per_attempt=1)
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.STALLED
    assert run.last_error == "context overflow"
    assert len(_session_posts(fake)) == 2
    assert len(_prompts(fake)) == 2
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    (old_session, fresh_session) = fake.sessions()
    assert run.session_id == fresh_session.id
    (t0, t1) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["session_id"] == old_session.id and t1["idle_status"] == "error"


def test_an_overflow_with_zero_continues_is_a_stall_without_a_new_session(tmp_path):
    """`max_continues_per_attempt = 0` turns the mechanism off: a dirty overflow
    is STALLED, not a fresh session."""
    cfg = make_config(["agent-a"], max_continues_per_attempt=0)
    scenario = {"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy"],
                           "error": _OVERFLOW}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.STALLED
    assert run.last_error == "context overflow"
    assert len(_session_posts(fake)) == 1 and len(_prompts(fake)) == 1


def test_an_overflow_with_an_unreadable_tree_is_a_clean_stall(tmp_path, monkeypatch, caplog):
    """`_dirty_tree` raising `TreeReadError`: the read error does not raise into
    the run — no fresh session, the "no work" stall, and the reason on the log."""
    caplog.set_level(logging.WARNING, logger="tools.contest.runner")

    def unreadable(ws):
        raise TreeReadError("git status in /wt/agent-a exited 128: not a git repository")

    monkeypatch.setattr("tools.contest.runner._dirty_tree", unreadable)
    scenario = {"turns": [{"events": ["busy"], "error": _OVERFLOW}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, _stall_config())
    assert run.state is AgentState.STALLED
    assert run.last_error == "context overflow with no uncommitted work"
    assert run.commit is None
    assert len(_session_posts(fake)) == 1
    assert _runner_has(caplog, "tree unreadable")


def test_a_non_overflow_session_error_is_error_as_before(tmp_path):
    """`name: "SomeOtherError"`: today's path byte for byte — ERROR with the
    payload, one session, one prompt, no retry even though retries are
    configured (it is not retryable)."""
    cfg = _make_retry_config()
    scenario = {"turns": [{"events": ["busy"],
                           "error": {"name": "SomeOtherError", "message": "boom-98"}}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, scenario, cfg)
    assert run.state is AgentState.ERROR
    assert "boom-98" in run.last_error
    assert not run.last_error.startswith("after ")
    assert len(_session_posts(fake)) == 1 and len(_prompts(fake)) == 1
    assert run.turns[0]["idle_status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# KC-56: a turn cut off at `finish: "length"` goes on, it is not harvested
# ─────────────────────────────────────────────────────────────────────────────

#: round 74's sensenova window: `limit.context` of the model that was cut off at
#: 244 410 + 17 734 = 262 144
_LIMIT = 262_144


def _length(**tokens) -> dict:
    """An assistant message's `info` for a reply cut off at a token limit."""
    tokens.setdefault("input", 0)
    tokens.setdefault("output", 0)
    tokens.setdefault("reasoning", 0)
    tokens.setdefault("cache", {"read": 0, "write": 0})
    return {"finish": "length", "tokens": tokens}


#: the first row of round 74: the output budget, all of it reasoning, no text
_OUTPUT_CUT = _length(input=23_352, reasoning=32_000, cache={"read": 0, "write": 0})
#: a full window: 91 % of `_LIMIT` in `input` alone
_CONTEXT_CUT = _length(input=int(0.91 * _LIMIT))


def _cut_config(context_limit=_LIMIT, **over) -> ContestConfig:
    """`make_config` whose agent-a carries *context_limit*, as intake puts it there."""
    config = make_config(["agent-a"], **over)
    return replace(config, agents=tuple(replace(spec, context_limit=context_limit or None)
                                        for spec in config.agents))


def _run_cut_one(tmp_path, scenario, *, config=None):
    """`_run_one` against :class:`_OverflowFake`, so a scenario may script the
    fresh session a context cut-off opens; the agent's limit is `_LIMIT`."""
    sb = Sandbox(tmp_path)
    config = config or _cut_config()
    with _OverflowFake(scenario) as fake:
        h = Harness(sb, fake, config)
        run = h.go()
    return sb, fake, h, run


class _Messages:
    """A backend stand-in for `_cut_off`: `messages()` returns or raises."""

    def __init__(self, messages=None, error=None):
        self._messages, self._error = messages, error

    def messages(self, session):
        if self._error is not None:
            raise self._error
        return self._messages


def _assistant(info) -> dict:
    return {"info": {"role": "assistant", **info}, "parts": []}


def test_cut_off_reads_the_last_assistant_message():
    """`"output"` below 90 % of the window, `"context"` at or above it, `None`
    for any other finish — and only the *last* assistant message counts."""
    user = {"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]}
    assert _cut_off(_Messages([user, _assistant(_OUTPUT_CUT)]), None, _LIMIT) == "output"
    assert _cut_off(_Messages([_assistant(_CONTEXT_CUT), user]), None, _LIMIT) == "context"
    # round 74's third row: input + reasoning is exactly the window
    full = _length(input=244_410, reasoning=17_734)
    assert _cut_off(_Messages([_assistant(full)]), None, _LIMIT) == "context"
    # cache.read counts toward the window; exactly at the threshold is "context"
    at = _length(input=0, cache={"read": int(CONTEXT_FULL_SHARE * 1000)})
    assert _cut_off(_Messages([_assistant(at)]), None, 1000) == "context"
    below = _length(input=int(CONTEXT_FULL_SHARE * 1000) - 1)
    assert _cut_off(_Messages([_assistant(below)]), None, 1000) == "output"
    stopped = {"finish": "stop", "tokens": _CONTEXT_CUT["tokens"]}
    assert _cut_off(_Messages([_assistant(_OUTPUT_CUT), _assistant(stopped)]), None,
                    _LIMIT) is None


def test_cut_off_is_output_when_the_limit_is_unknown():
    """No `limit.context` for the model: a full window cannot be told from a
    spent output budget, so every cut-off is an output one."""
    for limit in (None, 0):
        assert _cut_off(_Messages([_assistant(_CONTEXT_CUT)]), None, limit) == "output"


def test_cut_off_fails_open():
    """A transcript that cannot be read, is not a list, holds no assistant
    message, or a message whose tokens are junk — never an exception."""
    assert _cut_off(_Messages(error=RuntimeError("GET /message: 500")), None, _LIMIT) is None
    assert _cut_off(_Messages({"not": "a list"}), None, _LIMIT) is None
    assert _cut_off(_Messages([]), None, _LIMIT) is None
    assert _cut_off(_Messages([{"info": {"role": "user"}}, "junk", None]), None, _LIMIT) is None
    junk = {"finish": "length", "tokens": {"input": "many", "cache": 7, "output": True}}
    assert _cut_off(_Messages([_assistant(junk)]), None, _LIMIT) == "output"
    assert _cut_off(_Messages([_assistant({"finish": "length"})]), None, _LIMIT) == "output"


def test_an_output_cut_off_on_a_clean_tree_continues_in_the_same_session(tmp_path, monkeypatch):
    """Acceptance 1: `finish: "length"`, reasoning = the whole output budget, no
    text, a clean tree — a continue into the *same* session with the cut-off
    message, no harvest of the cut-off turn, `cut_off: "output"` in
    turns.jsonl, and the next turn's work is READY."""
    counts = _harvest_calls(monkeypatch)
    scenario = {"turns": [
        {"events": ["busy", "idle"], "message_info": _OUTPUT_CUT},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, h, run = _run_cut_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    assert len(_session_posts(fake)) == 1
    (sid0, _first), (sid1, second) = _prompts(fake)
    assert sid0 == sid1 and second == CUT_OFF_MESSAGE
    assert len(counts) == 1
    (t0, t1) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["cut_off"] == "output" and "harvest" not in t0
    assert "new_session" not in t0 and "cut_off" not in t1
    # the cut-off turn went straight back to PROMPTED, never HARVESTING
    first_harvest = h.transitions.index(AgentState.HARVESTING)
    assert h.transitions[:first_harvest].count(AgentState.PROMPTED) == 2


def test_a_context_cut_off_on_a_clean_tree_opens_a_fresh_session(tmp_path, monkeypatch):
    """Acceptance 2: `input` at 91 % of `limit.context`, a clean tree — a second
    `POST /session`, whose first prompt is the bare `round_prompt` (no dirty
    paragraph); `run.attempt` stays 0 and the swap spends a continue."""
    counts = _harvest_calls(monkeypatch)
    scenario = {
        "turns": [{"events": ["busy", "idle"], "message_info": _CONTEXT_CUT}],
        "turns_after": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
    }
    cfg = _cut_config()
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    ws = sb.ws("agent-a")
    _assert_ready(run, ws)
    assert run.attempt == 0
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    assert len(_session_posts(fake)) == 2
    (old_session, fresh_session) = fake.sessions()
    assert run.session_id == fresh_session.id
    (sid1, _first), (sid2, second) = _prompts(fake)
    assert sid1 == old_session.id and sid2 == fresh_session.id
    assert second == round_prompt("agent-a", sb.ticket_path, ws.base_sha,
                                  tmp_dir=_scratch_arg(cfg))
    assert len(counts) == 1
    (t0, t1) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["cut_off"] == "context" and "harvest" not in t0
    assert t0["session_id"] == old_session.id and t0["new_session"] == fresh_session.id
    assert "session_id" not in t1


def test_a_context_cut_off_on_a_dirty_tree_carries_the_dirty_paragraph(tmp_path):
    """Acceptance 3: the same cut-off with uncommitted work — the new session's
    first prompt is `round_prompt` *with* the dirty paragraph."""
    scenario = {
        "turns": [{"on_prompt": work_edit_no_commit, "events": ["busy", "idle"],
                   "message_info": _CONTEXT_CUT}],
        "turns_after": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
    }
    cfg = _cut_config()
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    ws = sb.ws("agent-a")
    _assert_ready(run, ws)
    (sid1, _first), (sid2, second) = _prompts(fake)
    assert sid1 != sid2
    assert second.startswith(round_prompt("agent-a", sb.ticket_path, ws.base_sha,
                                          tmp_dir=_scratch_arg(cfg)))
    assert "pkg/thing.py" in second and "uncommitted" in second
    assert run.turns[0]["cut_off"] == "context"


def test_a_cut_off_at_85_percent_of_the_window_is_an_output_one(tmp_path):
    """Acceptance 4: 85 % of `limit.context` is under the threshold — the
    same session, the cut-off message, no second `POST /session`."""
    scenario = {"turns": [
        {"events": ["busy", "idle"], "message_info": _length(input=int(0.85 * _LIMIT))},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    assert len(_session_posts(fake)) == 1
    assert _prompts(fake)[1][1] == CUT_OFF_MESSAGE
    assert run.turns[0]["cut_off"] == "output"


def test_a_stopped_reply_on_a_clean_tree_is_harvested_as_before(tmp_path):
    """Acceptance 5: `finish: "stop"` on a clean tree is today's path — harvested
    at once, no continue, no `cut_off`."""
    cfg = _cut_config(max_rework=0)
    stopped = {"finish": "stop", "tokens": _CONTEXT_CUT["tokens"]}
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": stopped}]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    assert run.state is AgentState.GAVE_UP
    (turn,) = run.turns
    assert turn["kind"] == "initial" and turn["harvest"]["verdict"] == "REWORK"
    assert "cut_off" not in turn
    assert len(_session_posts(fake)) == 1 and len(_prompts(fake)) == 1


def test_a_transcript_that_cannot_be_read_is_harvested_as_before(tmp_path, monkeypatch):
    """Acceptance 6: `messages()` raising — fail-open, today's path."""
    def boom(self, session):
        raise RuntimeError("GET /session/x/message: 500")

    monkeypatch.setattr(KiloBackend, "messages", boom)
    cfg = _cut_config(max_rework=0)
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": _OUTPUT_CUT}]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    assert run.state is AgentState.GAVE_UP
    (turn,) = run.turns
    assert turn["harvest"]["verdict"] == "REWORK" and "cut_off" not in turn
    assert len(_prompts(fake)) == 1


def test_an_output_cut_off_with_the_budget_spent_is_harvested(tmp_path, monkeypatch):
    """Acceptance 7: `max_continues_per_attempt = 1`, two output cut-offs in a
    row — the first continues, the second falls through to the harvest."""
    counts = _harvest_calls(monkeypatch)
    cfg = _cut_config(max_continues_per_attempt=1, max_rework=0)
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": _OUTPUT_CUT}] * 2}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    assert run.state is AgentState.GAVE_UP
    assert [t["kind"] for t in run.turns] == ["initial", "continue"]
    assert run.turns[0]["cut_off"] == "output"
    assert "cut_off" not in run.turns[1] and run.turns[1]["harvest"]["verdict"] == "REWORK"
    assert len(counts) == 1 and len(_session_posts(fake)) == 1


def test_a_context_cut_off_spends_the_same_budget_as_a_continue(tmp_path, monkeypatch):
    """The swap counts in `max_continues_per_attempt`: with a budget of 1, the
    fresh session's own cut-off is harvested — two sessions, never three."""
    counts = _harvest_calls(monkeypatch)
    cfg = _cut_config(max_continues_per_attempt=1, max_rework=0)
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": _CONTEXT_CUT}]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    assert run.state is AgentState.GAVE_UP
    assert len(_session_posts(fake)) == 2 and len(counts) == 1
    assert run.turns[0]["cut_off"] == "context" and "harvest" in run.turns[1]


def test_zero_continues_turns_the_cut_off_check_off(tmp_path, monkeypatch):
    """`max_continues_per_attempt = 0`: no transcript is read, the cut-off turn
    is harvested at once."""
    def never(self, session):
        raise AssertionError("messages() must not be read with no continue budget")

    monkeypatch.setattr(KiloBackend, "messages", never)
    cfg = _cut_config(max_continues_per_attempt=0, max_rework=0)
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": _CONTEXT_CUT}]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=cfg)
    assert run.state is AgentState.GAVE_UP
    assert len(_session_posts(fake)) == 1 and "cut_off" not in run.turns[0]


def test_an_unknown_limit_makes_a_full_window_an_output_cut_off(tmp_path):
    """No `context_limit` on the spec — a roster read without an offer: the
    same session and the cut-off message, never a second `POST /session`."""
    scenario = {"turns": [
        {"events": ["busy", "idle"], "message_info": _CONTEXT_CUT},
        {"on_prompt": work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario, config=_cut_config(None))
    _assert_ready(run, sb.ws("agent-a"))
    assert len(_session_posts(fake)) == 1
    assert run.turns[0]["cut_off"] == "output"


def test_a_failed_post_session_on_a_context_cut_off_is_error(tmp_path, monkeypatch):
    """The fresh session is refused: ERROR with the `POST /session` line, the
    cut-off turn is still in turns.jsonl with its old session, and that old
    session is still the one `finally` writes to `<agent>.session.json`."""
    real = KiloBackend.create_session
    calls = []

    def once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise ContestBackendError("POST /session -> 503")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(KiloBackend, "create_session", once)
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": _CONTEXT_CUT}]}
    sb, fake, _h, run = _run_cut_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("POST /session failed:")
    (t0,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["cut_off"] == "context" and t0["session_id"] == fake.sessions()[0].id
    assert "new_session" not in t0
    assert (sb.out_dir / "agent-a.session.json").is_file()


def test_a_refused_overflow_swap_still_records_the_turn_and_the_session(tmp_path, monkeypatch):
    """Guard for the swap KC-56 factored out of KC-54: a dirty overflow whose
    fresh `POST /session` is refused ends ERROR exactly as before — the turn
    that overflowed in turns.jsonl, and the old session's messages in
    `<agent>.session.json` (a swap helper that hands back `None` for the
    session on a refusal loses the second)."""
    real = KiloBackend.create_session
    calls = []

    def once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise ContestBackendError("POST /session -> 503")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(KiloBackend, "create_session", once)
    scenario = {"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy"],
                           "error": _OVERFLOW}]}
    sb, fake, _h, run, _ = _run_overflow_one(tmp_path, scenario)
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("POST /session failed:")
    (t0,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert t0["session_id"] == fake.sessions()[0].id and t0["idle_status"] == "error"
    assert (sb.out_dir / "agent-a.session.json").is_file()


def test_run_round_hands_each_agent_its_models_context_limit(tmp_path):
    """`run_round` reads the limit off each agent's spec: agent-a's cut-off at
    91 % of its own window opens a fresh session."""
    sb = Sandbox(tmp_path)
    config = _cut_config()
    scenario = {
        "turns": [{"events": ["busy", "idle"], "message_info": _CONTEXT_CUT}],
        "turns_after": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
    }
    with _OverflowFake(scenario) as fake:
        state = run_round(config, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir)
    run = _by_name(state)["agent-a"]
    _assert_ready(run, sb.ws("agent-a"))
    assert run.turns[0]["cut_off"] == "context"
    assert len(_session_posts(fake)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# run_round
# ─────────────────────────────────────────────────────────────────────────────

def test_round_two_agents_ready_with_state_json_and_table_rows(tmp_path):
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
        posts = _session_posts(fake)
    runs = _by_name(state)
    for name in ("agent-a", "agent-b"):
        _assert_ready(runs[name], sb.ws(name))
        assert (sb.out_dir / name / "turns.jsonl").is_file()
        assert (sb.out_dir / f"{name}.session.json").is_file()
    assert sorted(p["query"]["directory"] for p in posts) == sorted(str(w.path) for w in sb.workspaces)
    data = _state_json(sb)
    assert (data["round_no"], data["ticket"], data["base_sha"]) == (ROUND, TICKET, sb.base_sha)
    assert RoundState.from_dict(data) == state
    rows = state.table_rows()
    assert [r["name"] for r in rows] == ["agent-a", "agent-b"]
    assert all(r["state"] == "READY" and r["commit"] == runs[r["name"]].commit for r in rows)


def test_unknown_model_for_one_agent_does_not_stop_the_round(tmp_path):
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    scenario = {"bad_models": {"agent-b:free"}, "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
    runs = _by_name(state)
    _assert_ready(runs["agent-a"], sb.ws("agent-a"))
    assert runs["agent-b"].state is AgentState.ERROR and "unknown model" in runs["agent-b"].last_error
    assert RoundState.from_dict(_state_json(sb)).agents[1].state is AgentState.ERROR


def _creates_and_reads(fake) -> tuple:
    """Indexes of the two ``POST /session`` and whether a ``/message`` read (the
    first agent finishing) sits between them."""
    reqs = list(fake.requests)
    creates = [i for i, r in enumerate(reqs) if r["method"] == "POST" and r["path"] == "/session"]
    between = [r["path"] for r in reqs[creates[0]:creates[1]]]
    return between, any(p.endswith("/message") for p in between)


def test_max_parallel_one_runs_two_agents_sequentially(tmp_path):
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy"], "delay": 0.7}]}) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=1))
        between, finished_first = _creates_and_reads(fake)
    for name in ("agent-a", "agent-b"):
        _assert_ready(_by_name(state)[name], sb.ws(name))
    assert finished_first, between


def test_max_parallel_two_overlaps_two_agents(tmp_path):
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy"], "delay": 1.0}]}) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
        between, finished_first = _creates_and_reads(fake)
    for name in ("agent-a", "agent-b"):
        _assert_ready(_by_name(state)[name], sb.ws(name))
    # the second session opened before the first finished — overlap proven from
    # the request order, not from a wall-clock bound that a loaded box breaks.
    assert not finished_first, between


def test_three_agents_rework_in_parallel_and_state_json_is_always_whole(tmp_path):
    """A reader parses state.json continuously while three agents rework at
    once: every read is valid JSON, no agent goes backwards from READY, and
    the per-agent artifacts never mix."""
    agents = ["agent-a", "agent-b", "agent-c"]
    sb = Sandbox(tmp_path, agents)
    scenario = {"turns": [dict(_permission_turn(work_no_test, ["/tmp/*"]), delay=0.2),
                          dict(_permission_turn(work_ready, ["/tmp/*"]), delay=0.2)]}
    stop, bad, ready, reads = threading.Event(), [], set(), [0]

    def reader():
        path = sb.out_dir / "state.json"
        while not stop.is_set():
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    reads[0] += 1
                    for a in data["agents"]:
                        if a["state"] == "READY":
                            ready.add(a["agent"]["name"])
                        elif a["agent"]["name"] in ready:
                            bad.append(f"{a['agent']['name']} left READY for {a['state']}")
                except (ValueError, OSError) as exc:
                    bad.append(f"torn state.json: {exc}")
            time.sleep(0.003)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with _BenchFake(scenario) as fake:
            state = _round(sb, fake, make_config(agents, max_parallel=3, tmp_roots=("/tmp/*",)))
            prompts = _prompts(fake)
    finally:
        stop.set()
        thread.join(2)
    runs = _by_name(state)
    for name in agents:
        _assert_ready(runs[name], sb.ws(name))
        assert runs[name].attempt == 1 and len(runs[name].turns) == 2
        decisions = _jsonl(sb.out_dir / name / "decisions.jsonl")
        assert len(decisions) == 2 and all(d["sessionID"] == runs[name].session_id for d in decisions)
        assert len(_jsonl(sb.out_dir / name / "turns.jsonl")) == 2
        assert runs[name].permissions["asked"] == 2 and runs[name].permissions["allowed"] == 2
    assert len(prompts) == 6 and len({sid for sid, _ in prompts}) == 3
    assert reads[0] > 5 and not bad, bad[:3]


def test_a_silent_agent_stalls_next_to_a_chatty_one(tmp_path):
    """agent-a goes silent while agent-b keeps emitting on the same server;
    the fake broadcasts every event to every tap, so a's clock must count
    only a's session — a takes the idle-event silence path (not the 30 s
    turn_timeout), regardless of what agent-b is doing at the same time.

    FL-1 (round 84, round 7): what this test keeps is the half that cannot
    be starved.

    It used to make agent-b beat for 12 s *across* agent-a's 8 s window, so
    b had to survive a window it was not tripping — Shape 4 (postmortem §8),
    where the window, the runtime and the tolerance for a starved box are
    one number and there is no margin to widen. It failed exactly that way:
    b came back `(STALLED, 'no event for 8s')` while emitting five times a
    second, because what starves is the *delivery* of a beat, not its
    emission.

    So the sharp claim — a neighbour's events do not reset this session's
    silence clock — moved to where it can be settled exactly, on a fake
    clock with no transport at all: `test_contest_kilo_client.py::
    test_a_beat_the_wait_does_not_count_lets_the_silence_clock_run_out`.
    A KC-12 regression fails there deterministically.

    What is left here is the integration half, and all of it is load-proof:
    in a real two-agent round, on one broadcast stream, the silent agent
    stalls on its own clock and the working neighbour is not taken down with
    it. b is still chatty — it just finishes its turn well inside a's window
    instead of racing it, so starvation can only delay a's stall, never
    invert the outcome.
    * the classification check (idle_status/last_error) is what proves a's
      clock ran independently of b's traffic, not a wall-clock bound: a
      KC-12 regression (a's clock counting b's events) would show up as
      agent-a taking the turn_timeout path instead of the idle-event one,
      not as a slow abort — and the abort check looks only at agent-a's own
      session instead of unpacking "the one abort" from a list that a
      genuine second abort (of either agent) can make hold more than one
      entry.
    """
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    stamps: list = []

    with _BenchFake({"turns": [{"events": ["busy"], "idle": False}]}) as fake:
        orig_turn, orig_record = fake._run_turn, fake._record_request

        def run_turn(session, turn, text):
            if session.directory.endswith("agent-b"):
                # The beats start *before* the git work, not after it: b's
                # silence clock is already running when this hook is entered.
                # b chats for at least ~2 s and idles once its work is
                # committed (`idle_after`), inside a's 8 s window on an idle
                # box — what matters here is the neighbour's *traffic*, not
                # how long the neighbour lasts, and b must not idle before its
                # own commit and PROGRESS.csv row exist, or it is not READY.
                work_done = threading.Event()
                fake.pulse(session.id, KEEPALIVE_BEAT_S, NEIGHBOUR_BEATS,
                           then_idle=True, idle_after=work_done)
                try:
                    work_ready(session.directory, text)
                finally:
                    work_done.set()
            return orig_turn(session, turn, text)

        def record(method, path, query, body):
            stamps.append((time.monotonic(), path))
            orig_record(method, path, query, body)

        fake._run_turn, fake._record_request = run_turn, record
        state = _round(sb, fake, make_config(
            ["agent-a", "agent-b"], max_parallel=2, turn_timeout_sec=120,
            idle_event_timeout_sec=KEEPALIVE_WINDOW_S))
    runs = _by_name(state)
    assert runs["agent-a"].state is AgentState.STALLED
    assert runs["agent-a"].last_error == f"no event for {KEEPALIVE_WINDOW_S}s"
    assert runs["agent-a"].turns[-1]["idle_status"] == "stalled"
    _assert_ready(runs["agent-b"], sb.ws("agent-b"))
    a_abort_path = f"/session/{runs['agent-a'].session_id}/abort"
    assert any(s[1] == a_abort_path for s in stamps), "agent-a's session was never aborted"


def _interrupt_once_agent_a_is_saved_ready(sb, pid, timeout=10.0):
    """An `on_prompt` that sends SIGINT to *pid* — after state.json says agent-a is READY.

    agent-a's harvest and agent-b's second prompt run on different threads. Sending the
    signal the moment b is prompted races a's READY, and the state.json the interrupt
    saves is the one the test asserts on: on a loaded machine a was still HARVESTING
    about half the time. state.json is rewritten after every transition, so READY on
    disk means READY in memory. If it never shows up the signal goes out anyway after
    *timeout*, and the test's own assertion reports what it found.
    """
    def on_prompt(directory, text):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                agents = _state_json(sb)["agents"]
            except (OSError, ValueError):
                agents = []
            if any(a["agent"]["name"] == "agent-a" and a["state"] == "READY" for a in agents):
                break
            time.sleep(0.02)
        os.kill(pid, signal.SIGINT)

    return on_prompt


@pytest.mark.usefixtures("sigint_raises_keyboardinterrupt")
def test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes(tmp_path):
    """agent-a lands in one turn; agent-b's rework prompt is where Ctrl-C
    arrives (SIGINT to this process). Then b was aborted, state.json says
    a=READY and b mid-flight, KeyboardInterrupt propagated. Resuming from
    that state.json skips a (no new session) and restarts b in its worktree."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2, turn_timeout_sec=300)
    pid = os.getpid()

    def turn1(directory, text):
        (work_ready if _agent_of(directory) == "agent-a" else work_no_test)(directory, text)

    scenario = {"turns": [{"on_prompt": turn1, "events": ["busy", "idle"]},
                          {"on_prompt": _interrupt_once_agent_a_is_saved_ready(sb, pid),
                           "events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        with pytest.raises(KeyboardInterrupt):
            _round(sb, fake, cfg)
        aborted = {r["path"].split("/")[2] for r in fake.calls(path="/abort")}
        session_b = next(s.id for s in fake.sessions() if s.directory.endswith("agent-b"))
    assert session_b in aborted
    saved = RoundState.from_dict(_state_json(sb))
    a, b = saved.agents
    assert a.state is AgentState.READY and not b.state.terminal

    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake2:
        state = _round(sb, fake2, cfg, resume=saved)
        creates = [p["query"]["directory"] for p in _session_posts(fake2)]
        prompts = _prompts(fake2)
    runs = _by_name(state)
    _assert_ready(runs["agent-a"], sb.ws("agent-a"))
    _assert_ready(runs["agent-b"], sb.ws("agent-b"))
    assert creates == [str(sb.ws("agent-b").path)]
    assert len(prompts) == 1 and "runs/agent-b/PROGRESS.csv" in prompts[0][1]
    assert _commits(str(sb.ws("agent-b").path)) == 1
    assert RoundState.from_dict(_state_json(sb)) == state


def test_resume_adopts_a_ready_worktree_without_a_session(tmp_path):
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2)
    _work(str(sb.ws("agent-b").path), test=True)
    prior = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha, started_at=1.0, agents=[
        AgentRun(agent=cfg.agents[0], workspace=sb.ws("agent-a"), state=AgentState.READY, commit="0" * 40),
        AgentRun(agent=cfg.agents[1], workspace=sb.ws("agent-b"), state=AgentState.WAITING, session_id="ses_gone"),
    ])
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake:
        state = _round(sb, fake, cfg, resume=prior)
        creates = _session_posts(fake)
    runs = _by_name(state)
    assert runs["agent-a"].state is AgentState.READY and runs["agent-a"].commit == "0" * 40
    _assert_ready(runs["agent-b"], sb.ws("agent-b"))
    assert not creates
    assert state.started_at == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# run_tests — KC-16: the pytest roots inside the harvest
# ─────────────────────────────────────────────────────────────────────────────

THING_CHANGED = "def thing():\n    return 42\n"
TEST_BREAKS = "def test_thing():\n    assert False\n"
TEST_PASSES = "def test_thing():\n    assert 1 + 1 == 2\n"
ALL_ROOTS_PASS = "tests:PASS tests_bugfix:absent .smoke_tests:absent .regression_tests:absent"


def _commit(directory: str) -> str:
    """`add -A`, one commit — amended, since a rework keeps the branch at one commit."""
    _git(directory, "add", "-A")
    if _commits(directory) >= 1:
        _git(directory, "commit", "-q", "--amend", "--no-edit")
    else:
        _git(directory, "commit", "-q", "-m", "KC-16: thing")
    return _git(directory, "rev-parse", "HEAD")


def work_breaking_test(directory, text):
    """A change plus a test that fails: READY for the ticket, REWORK for the suite."""
    _write(Path(directory) / "pkg" / "thing.py", THING_CHANGED)
    _write(Path(directory) / "tests" / "test_thing.py", TEST_BREAKS)
    _claim(directory, _commit(directory))


def work_passing_test(directory, text):
    """The same worktree with a test that passes: the rework's second turn."""
    _write(Path(directory) / "tests" / "test_thing.py", TEST_PASSES)
    _claim(directory, _commit(directory))


def test_run_round_run_tests_is_keyword_only_with_a_false_default():
    """KC-16 adds one keyword to `run_round` and KC-62 another (`server_pid`, for
    the heartbeat's neighbour count); nothing else about it moves."""
    import inspect

    params = inspect.signature(run_round).parameters
    assert list(params) == ["config", "round_no", "ticket_path", "workspaces",
                            "make_backend", "out_dir", "resume", "run_tests",
                            "server_pid"]
    for name in ("make_backend", "out_dir", "resume", "run_tests", "server_pid"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    assert params["resume"].default is None
    assert params["run_tests"].default is False
    assert params["server_pid"].default is None
    assert inspect.signature(run_agent).parameters["run_tests"].default is False


def test_run_tests_true_makes_the_failed_suite_a_rework_with_the_pytest_tail(tmp_path):
    """`run_tests=True` judges the tree with the roots: the failing suite is
    REWORK with `tests_failed`, the pytest tail in the rework prompt, and the
    fixed suite READY on the second turn."""
    cfg = make_config(["agent-a"], max_parallel=1)
    scenario = {"turns": [{"on_prompt": work_breaking_test, "events": ["busy", "idle"]},
                          {"on_prompt": work_passing_test, "events": ["busy", "idle"]}]}
    sb = Sandbox(tmp_path)
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                          run_tests=True)
    (run,) = state.agents
    assert run.state is AgentState.READY
    first, second = run.turns
    assert first["harvest"]["verdict"] == "REWORK"
    assert first["harvest"]["reasons"] == ["tests_failed"]
    assert isinstance(first["harvest"]["elapsed"], float)
    assert second["harvest"]["verdict"] == "READY"
    assert second["harvest"]["reasons"] == []
    assert isinstance(second["harvest"]["elapsed"], float)
    assert run.attempt == 1
    (sid, _), (_, rework) = _prompts(fake)
    assert "FAILED tests/test_thing.py::test_thing" in rework
    assert "Attempt 1 of 2" in rework


def test_run_tests_false_is_ready_after_one_turn_on_the_same_tree(tmp_path):
    """The same tree, the roots off: the ticket's self-check is the only judge
    and one turn is enough — the default every earlier test runs on."""
    cfg = make_config(["agent-a"], max_parallel=1)
    scenario = {"turns": [{"on_prompt": work_breaking_test, "events": ["busy", "idle"]}]}
    sb = Sandbox(tmp_path)
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir)
    (run,) = state.agents
    _assert_ready(run, sb.ws("agent-a"))
    assert len(run.turns) == 1
    assert run.turns[0]["harvest"]["verdict"] == "READY"
    assert run.turns[0]["harvest"]["reasons"] == []
    assert isinstance(run.turns[0]["harvest"]["elapsed"], float)


def test_run_tests_true_never_runs_the_roots_twice_at_once(tmp_path, monkeypatch):
    """Two agents harvested at once, `run_tests=True`: the roots run one
    worktree at a time, so the counter never leaves zero until one suite is done."""
    import tools.contest.harvest as harvest_module

    agents = ["agent-a", "agent-b"]
    sb = Sandbox(tmp_path, agents)
    cfg = make_config(agents, max_parallel=2)
    barrier = threading.Barrier(len(agents))
    counter = [0]
    peaks = []

    def roots(cwd, **kwargs):
        counter[0] += 1
        peaks.append(counter[0])
        time.sleep(0.2)
        counter[0] -= 1
        return ALL_ROOTS_PASS, []

    def on_prompt(directory, text):
        # FL-1 (round 84): a rendezvous between two agent threads, not a
        # deadline — 2 s was short enough for a loaded box to break the
        # barrier and turn a scheduling delay into a test failure.
        barrier.wait(60)
        work_ready(directory, text)

    monkeypatch.setattr(harvest_module, "run_tests_detail", roots)
    scenario = {"turns": [{"on_prompt": on_prompt, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                          run_tests=True)
    for name in agents:
        _assert_ready(_by_name(state)[name], sb.ws(name))
    assert len(peaks) == 2 and max(peaks) == 1, peaks


def test_resume_harvests_the_mid_flight_worktree_with_the_roots(tmp_path, monkeypatch):
    """`_plan` takes the same `run_tests`: a resumed tree that breaks the suite is
    not adopted as READY just because it committed and claimed."""
    import tools.contest.harvest as harvest_module

    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2)
    _work(str(sb.ws("agent-b").path), test=True)
    harvested: list[tuple[str, str]] = []

    def roots(cwd, **kwargs):
        # KC-60: the roots run in a detached checkout of the commit, not the tree.
        harvested.append((cwd, _git(cwd, "rev-parse", "HEAD")))
        return ALL_ROOTS_PASS, []

    prior = RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha, started_at=1.0, agents=[
        AgentRun(agent=cfg.agents[0], workspace=sb.ws("agent-a"), state=AgentState.READY, commit="0" * 40),
        AgentRun(agent=cfg.agents[1], workspace=sb.ws("agent-b"), state=AgentState.WAITING, session_id="ses_gone"),
    ])
    monkeypatch.setattr(harvest_module, "run_tests_detail", roots)
    with _BenchFake({"turns": []}) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                          resume=prior, run_tests=True)
    runs = _by_name(state)
    assert runs["agent-b"].state is AgentState.READY
    assert runs["agent-a"].commit == "0" * 40
    assert not _session_posts(fake)
    assert harvested[0][0] != str(sb.ws("agent-b").path)      # not the agent's tree
    assert harvested[0][1] == _branch_sha(sb.ws("agent-b"))   # the commit it scores


# ─────────────────────────────────────────────────────────────────────────────
# KC-18: the round narrates itself — one INFO line per transition, a heartbeat
# ─────────────────────────────────────────────────────────────────────────────

LOGGER = "tools.contest.runner"


def _lines(caplog, prefix: str) -> list:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.INFO and r.getMessage().startswith(prefix)]


def test_every_transition_is_one_info_line_with_the_agents_name_first(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _sb, _fake, h, run, _ = _run_one(tmp_path, scenario)
    assert run.state is AgentState.READY
    lines = _lines(caplog, "agent-a: ")
    assert [l.split()[1] for l in lines] == ["PROMPTED", "WAITING", "HARVESTING", "READY"]
    assert len(lines) == len(h.transitions)
    assert "attempt 0 (initial)" in lines[0]
    assert "tests off" in lines[2]
    assert run.commit[:12] in lines[3]


def test_rework_and_error_lines_carry_the_codes_and_the_payload(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    scenario = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _run_one(tmp_path, scenario)
    (rework,) = _lines(caplog, "agent-a: REWORK")
    assert "attempt 1" in rework and "no_test_file" in rework
    assert any("(rework)" in l for l in _lines(caplog, "agent-a: PROMPTED"))

    caplog.clear()
    scenario = {"turns": [{"events": ["busy"], "error": {"name": "ProviderError", "message": "boom-42"}}]}
    _run_one(tmp_path / "second", scenario)
    (error,) = _lines(caplog, "agent-a: ERROR")
    assert "session.error" in error and "boom-42" in error


def test_heartbeat_names_the_waiting_agent_and_its_time_in_state(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    sb = Sandbox(tmp_path)
    # FL-1 (round 84): the silence window is incidental here — this test is
    # about the heartbeat *log line*, and the turn only has to reach READY.
    # A 5 s window was one more thing for a loaded box to trip over.
    cfg = make_config(["agent-a"], progress_every_sec=0.2, idle_event_timeout_sec=120)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"], "delay": 1.0}]}
    with _BenchFake(scenario) as fake:
        state = _round(sb, fake, cfg)
    assert state.agents[0].state is AgentState.READY
    beats = _lines(caplog, f"round {ROUND} ")
    assert beats, caplog.text
    assert any("agent-a WAITING" in b and "1 live" in b for b in beats)
    assert all(b.endswith(" live") for b in beats)
    assert not [t for t in threading.enumerate() if t.name.startswith("contest-progress")]


def test_progress_every_sec_zero_logs_transitions_but_no_heartbeat(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], progress_every_sec=0)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"], "delay": 0.5}]}
    with _BenchFake(scenario) as fake:
        _round(sb, fake, cfg)
    assert _lines(caplog, "agent-a: READY")
    assert not _lines(caplog, f"round {ROUND} ")
    assert not [t for t in threading.enumerate() if t.name.startswith("contest-progress")]


# ─────────────────────────────────────────────────────────────────────────────
# KC-22 follow-up: what the landed tests leave open — the off switch on --resume,
# the porcelain lines as git prints them, a bounded listing, and the behaviours
# the implementation has but nothing pins down
# ─────────────────────────────────────────────────────────────────────────────

import tools.contest.runner as _runner_module  # noqa: E402
from tools.contest.runner import continue_message  # noqa: E402


def work_commit_and_leave_a_stray_file(directory, text):
    """One commit (no test, so REWORK) and one file left over in the tree."""
    work_no_test(directory, text)
    _write(Path(directory) / "pkg" / "stray.py", "x = 1\n")


def _watch_tree_reads(monkeypatch) -> list:
    """Record every `git status` the runner takes of a worktree; the real one still runs."""
    real, calls = _runner_module._dirty_tree, []

    def watching(ws):
        calls.append(str(ws.path))
        return real(ws)

    monkeypatch.setattr(_runner_module, "_dirty_tree", watching)
    return calls


def _mid_flight_prior(sb, cfg, agent="agent-a") -> RoundState:
    """A round that died with *agent* mid-flight: its session is gone, its worktree is not."""
    spec = next(s for s in cfg.agents if s.name == agent)
    return RoundState(round_no=ROUND, ticket=TICKET, base_sha=sb.base_sha, started_at=1.0, agents=[
        AgentRun(agent=spec, workspace=sb.ws(agent), state=AgentState.WAITING, session_id="ses_gone"),
    ])


def _resumed_first_prompt(sb, cfg) -> str:
    """Resume *sb*'s only agent into whatever its worktree holds; the fresh session's first prompt."""
    with _BenchFake({"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake:
        _round(sb, fake, cfg, resume=_mid_flight_prior(sb, cfg))
        (_sid, first), = _prompts(fake)
    return first


def _plain_prompt(sb, cfg) -> str:
    """The plain `round_prompt` plus the KC-65 note the runner appends to every send.

    "No paragraph" means exactly this: nothing between `round_prompt` and the
    worker note, so a `continue_message` block would fail these equalities.
    """
    return (round_prompt("agent-a", sb.ticket_path, sb.base_sha, tmp_dir=_scratch_arg(cfg))
            + _runner_module.prompt_workers_note(sb.out_dir, cfg))


def test_resume_with_max_continues_zero_sends_the_plain_prompt(tmp_path, monkeypatch):
    """0 disables the whole mechanism: no paragraph, and the tree is not even read —
    only the runner's KC-65 worker note sits behind `round_prompt`."""
    tree_reads = _watch_tree_reads(monkeypatch)
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], max_continues_per_attempt=0)
    work_edit_no_commit(str(sb.ws("agent-a").path), "")
    first = _resumed_first_prompt(sb, cfg)
    assert first == _plain_prompt(sb, cfg)
    assert tree_reads == []


def test_resume_into_a_clean_worktree_keeps_the_prompt_exactly(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    _work(str(sb.ws("agent-a").path), test=False)       # committed, no test: REWORK, tree clean
    assert _resumed_first_prompt(sb, cfg) == _plain_prompt(sb, cfg)


def test_resume_with_a_commit_under_the_dirty_tree_keeps_the_plain_prompt(tmp_path):
    """The paragraph says nothing was committed, so it is only sent when that is true."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    work_commit_and_leave_a_stray_file(str(sb.ws("agent-a").path), "")
    assert _resumed_first_prompt(sb, cfg) == _plain_prompt(sb, cfg)


def test_a_dirty_tree_above_a_commit_is_harvested_not_continued(tmp_path, monkeypatch):
    """Something is handed in: the harvest has a commit to score, so it scores it."""
    harvests = _harvest_calls(monkeypatch)
    scenario = {"turns": [{"on_prompt": work_commit_and_leave_a_stray_file, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _sb, fake, _h, run, _ = _run_one(tmp_path, scenario)
    assert [t["kind"] for t in run.turns][:2] == ["initial", "rework"]
    assert run.turns[0]["harvest"]["verdict"] == "REWORK"
    assert len(harvests) >= 1
    assert "uncommitted" not in _prompts(fake)[1][1]


def test_a_fresh_round_reads_no_tree_and_its_prompt_is_exactly_round_prompt(tmp_path, monkeypatch):
    """The nudge is looked for on a `--resume` and after an idle turn with no commit; a
    fresh run that commits on its first turn costs no `git status` at all."""
    tree_reads = _watch_tree_reads(monkeypatch)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    cfg = make_config(["agent-a"])
    sb, fake, _h, _run, _ = _run_one(tmp_path, scenario, cfg)
    (_sid, text), = _prompts(fake)
    assert text == round_prompt("agent-a", sb.ticket_path, sb.base_sha,
                                tmp_dir=_scratch_arg(cfg))
    assert tree_reads == []


def test_continue_turns_and_the_resume_nudge_leave_the_json_shape_alone(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    work_edit_no_commit(str(sb.ws("agent-a").path), "")
    with _BenchFake({"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]},
                               {"on_prompt": work_ready, "events": ["busy", "idle"]}]}) as fake:
        state = _round(sb, fake, cfg, resume=_mid_flight_prior(sb, cfg))
    saved = _state_json(sb)
    (agent,) = saved["agents"]
    assert set(agent) == {"agent", "workspace", "session_id", "state", "attempt", "turns",
                          "permissions", "questions", "last_error", "resumable", "commit",
                          "cost", "tokens"}
    assert [t["kind"] for t in agent["turns"]] == ["initial", "continue"]
    assert RoundState.from_dict(saved) == state


def test_dirty_tree_names_each_file_and_leaves_runs_out(tmp_path):
    """`runs/` is the runner's ground even where a repo does not gitignore it, and the
    first line is as git prints it — its leading space is part of the status."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _write(repo / "pkg" / "a.py", "a\n")
    _write(repo / "pkg" / "b.py", "b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    ws = Workspace(agent="agent-a", path=repo.resolve(), branch="main",
                   base_sha=_git(repo, "rev-parse", "HEAD"), kind="worktree")

    assert _runner_module._dirty_tree(ws) == ""
    _write(repo / "runs" / "agent-a" / "PROGRESS.csv", "row\n")
    assert _runner_module._dirty_tree(ws) == ""                     # not the work
    _write(repo / "pkg" / "a.py", "changed\n")
    _write(repo / "pkg" / "b.py", "changed\n")
    _write(repo / "tests" / "new" / "test_x.py", "x\n")
    assert _runner_module._dirty_tree(ws).splitlines() == [
        " M pkg/a.py", " M pkg/b.py", "?? tests/new/test_x.py"]


def test_dirty_tree_of_a_path_git_cannot_read_raises(tmp_path):
    """FL-2: a `git status` that exits non-zero is a read error, not a clean tree.

    The old behaviour returned `""` here, and the caller read that as "no
    uncommitted work" — exactly the KC-31/KC-41 path where an unreadable tree
    became zero harvested entries. `TreeReadError` is what lets a caller tell
    the two apart; the runner catches it and degrades to "no nudge".
    """
    ws = Workspace(agent="agent-a", path=tmp_path / "gone", branch="main", base_sha="0" * 40,
                   kind="worktree")
    with pytest.raises(_runner_module.TreeReadError):
        _runner_module._dirty_tree(ws)


def test_continue_message_lists_every_line_of_a_short_tree():
    text = continue_message(" M pkg/thing.py\n?? tests/test_thing.py")
    assert "uncommitted" in text and "Do not start over" in text
    assert " M pkg/thing.py" in text and "?? tests/test_thing.py" in text
    assert "more" not in text


def test_continue_message_is_bounded_for_a_huge_tree():
    """The message is a prompt: a tree with thousands of untracked files is not a list."""
    text = continue_message("\n".join(f"?? gen/f{i}.py" for i in range(5000)))
    assert "gen/f0.py" in text and "gen/f39.py" in text
    assert "gen/f40.py" not in text
    assert "... and 4960 more" in text
    assert len(text.splitlines()) < 60


# ─────────────────────────────────────────────────────────────────────────────
# KC-27: the heartbeat shows phase, files-based progress, and test duration
# ─────────────────────────────────────────────────────────────────────────────

def _make_repo(tmp_path) -> Path:
    """A bare repo with a base commit, like Sandbox but standalone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _write(repo / "pkg" / "a.py", "a\n")
    _write(repo / "pkg" / "b.py", "b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_worktree_files_counts_tracked_and_untracked(tmp_path):
    """`_worktree_files` on a tmp_path sandbox: clean → 0, tracked+untracked → 2,
    committed+edited → 2, .smoke_tests/x not counted, non-git path → 0."""
    repo = _make_repo(tmp_path)
    ws = Workspace(agent="agent-a", path=repo.resolve(), branch="main",
                   base_sha=_git(repo, "rev-parse", "HEAD"), kind="worktree")

    assert _runner_module._worktree_files(ws) == 0

    _write(repo / "pkg" / "a.py", "changed\n")
    _write(repo / "pkg" / "c.py", "c\n")
    assert _runner_module._worktree_files(ws) == 2

    _git(repo, "add", "pkg/a.py")
    _git(repo, "commit", "-q", "-m", "edit a")
    _write(repo / "pkg" / "c.py", "c-edited\n")
    assert _runner_module._worktree_files(ws) == 2

    _write(repo / ".smoke_tests" / "x", "x\n")
    assert _runner_module._worktree_files(ws) == 2

    ws2 = Workspace(agent="agent-b", path=tmp_path / "not_a_repo", branch="main",
                    base_sha="0" * 40, kind="worktree")
    assert _runner_module._worktree_files(ws2) == 0


def test_progress_table():
    """The `_progress` table: files-count against the pack's median."""
    S = AgentState
    assert _runner_module._progress(S.WAITING, 1, 2, False) == 30
    assert _runner_module._progress(S.WAITING, 3, 2, False) == 60
    assert _runner_module._progress(S.WAITING, 0, 1, False) == 0
    assert _runner_module._progress(S.WAITING, 1, 2, True) == 70
    assert _runner_module._progress(S.HARVESTING, 5, 2, False) == 80
    assert _runner_module._progress(S.READY, 5, 2, False) == 100
    assert _runner_module._progress(S.STALLED, 5, 2, False) is None
    assert _runner_module._progress(S.PROMPTED, 5, 2, False) == 0


def _make_heartbeat(agents: list, files_by_name: dict):
    """A `_Heartbeat` with *agents* and mocked `_worktree_files`/`_commits_above`.

    Returns `(heartbeat, module, orig_files, orig_commits)` — restore the
    originals with `rm._worktree_files, rm._commits_above = f, c` in a `finally`.
    """
    rm = _runner_module
    orig_files, orig_commits = rm._worktree_files, rm._commits_above
    rm._worktree_files = lambda ws: files_by_name.get(ws.agent, 0)
    rm._commits_above = lambda ws: 0
    state = RoundState(round_no=ROUND, ticket=TICKET, base_sha="0" * 40,
                       started_at=time.time() - 120, agents=agents)
    hb = rm._Heartbeat(state, {a.agent.name: time.monotonic() - 60 for a in agents}, 0)
    return hb, rm, orig_files, orig_commits


def test_heartbeat_rollback_on_rework(tmp_path, monkeypatch):
    """WAITING with attempt=1 and a commit above base shows ≤60% and ↺1,
    never 70; the same agent with attempt=0 shows 70."""
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    spec = next(s for s in make_config(["agent-a"]).agents if s.name == "agent-a")

    # attempt=0 with a commit: 70%
    run0 = AgentRun(agent=spec, workspace=ws, state=AgentState.WAITING, attempt=0)
    hb0, rm, f, c = _make_heartbeat([run0], {"agent-a": 3})
    try:
        rm._commits_above = lambda ws: 1
        line = hb0.line()
        assert "70%" in line
        assert "↺" not in line
    finally:
        rm._worktree_files, rm._commits_above = f, c

    # attempt=1 with a commit: ≤60%, ↺1
    run1 = AgentRun(agent=spec, workspace=ws, state=AgentState.WAITING, attempt=1)
    hb1, rm, f, c = _make_heartbeat([run1], {"agent-a": 3})
    try:
        rm._commits_above = lambda ws: 1
        line = hb1.line()
        assert "↺1" in line
        assert "70%" not in line
    finally:
        rm._worktree_files, rm._commits_above = f, c


def test_heartbeat_line_with_two_agents_and_ready(tmp_path):
    """Two working agents (1 and 3 files) show 30%/60%, 1f/3f, ten-cell bars;
    a READY agent shows `READY (tests 4m)` when its last harvest has elapsed."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b", "agent-c"])
    specs = make_config(["agent-a", "agent-b", "agent-c"]).agents
    spec_a, spec_b, spec_c = specs[0], specs[1], specs[2]

    run_a = AgentRun(agent=spec_a, workspace=sb.ws("agent-a"), state=AgentState.WAITING)
    run_b = AgentRun(agent=spec_b, workspace=sb.ws("agent-b"), state=AgentState.WAITING)
    run_c = AgentRun(agent=spec_c, workspace=sb.ws("agent-c"), state=AgentState.READY,
                     commit="abc123456789",
                     turns=[{"harvest": {"verdict": "READY", "reasons": [], "elapsed": 240.0}}])

    hb, rm, f, c = _make_heartbeat([run_a, run_b, run_c],
                                    {"agent-a": 1, "agent-b": 3})
    try:
        line = hb.line()
        assert "agent-a WAITING" in line
        assert "30%" in line
        assert "1f" in line
        assert "agent-b WAITING" in line
        assert "60%" in line
        assert "3f" in line
        assert "[###......." in line  # 30% → 3 filled
        assert "[######...." in line  # 60% → 6 filled
        assert "agent-c READY (tests 4m)" in line
    finally:
        rm._worktree_files, rm._commits_above = f, c


def test_harvest_elapsed_in_turns_jsonl_and_state_json(tmp_path, caplog, monkeypatch):
    """With `run_tests=True`: the READY turn's harvest in turns.jsonl and
    state.json has a float `elapsed`, and the INFO line for READY ends with
    `(tests Ns)`.
    """
    caplog.set_level(logging.INFO, logger=LOGGER)
    import tools.contest.harvest as harvest_module
    monkeypatch.setattr(harvest_module, "run_tests_detail",
                        lambda cwd, **kwargs: (ALL_ROOTS_PASS, []))

    cfg = make_config(["agent-a"], max_parallel=1)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb = Sandbox(tmp_path)
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir,
                          run_tests=True)

    turns = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert len(turns) == 1
    assert "elapsed" in turns[0]["harvest"]
    assert isinstance(turns[0]["harvest"]["elapsed"], float)
    # KC-57: `waited` rides next to `elapsed`, 0.0 here because this harvest
    # took the round's pytest slot at once and waited for nobody.
    assert isinstance(turns[0]["harvest"]["waited"], float)
    assert turns[0]["harvest"]["waited"] == 0.0

    state_data = _state_json(sb)
    agent = state_data["agents"][0]
    assert "elapsed" in agent["turns"][0]["harvest"]
    assert isinstance(agent["turns"][0]["harvest"]["elapsed"], float)
    assert "waited" in agent["turns"][0]["harvest"]
    assert isinstance(agent["turns"][0]["harvest"]["waited"], float)

    lines = _lines(caplog, "agent-a: READY")
    assert len(lines) == 1
    assert "(tests " in lines[0] and lines[0].rstrip().endswith(")")


def test_run_tests_off_names_the_harvest_not_the_tests(tmp_path, caplog):
    """KC-27 §1: with `run_tests` off the READY note says `(harvest Ns)` — the
    harvest ran, the roots did not, and the note must not claim they did."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run_one(tmp_path, scenario)
    _assert_ready(run, sb.ws("agent-a"))
    (line,) = _lines(caplog, "agent-a: READY")
    assert "(harvest " in line and "(tests " not in line
    assert line.rstrip().endswith("s)")


def test_a_state_json_without_elapsed_still_loads(tmp_path):
    """KC-27 §5: `state.json`'s shape changes only by `elapsed`; one written
    before it loads through `from_dict` and keeps its two old keys."""
    data = {
        "round_no": 1, "ticket": "t.md", "base_sha": "0" * 40, "started_at": 1.0,
        "agents": [{
            "agent": {"name": "a", "provider_id": "p", "model_id": "m"},
            "workspace": {"agent": "a", "path": str(tmp_path), "branch": "b",
                          "base_sha": "0" * 40, "kind": "worktree"},
            "state": "READY", "attempt": 0,
            "turns": [{"kind": "initial", "harvest": {"verdict": "READY", "reasons": []}}],
            "permissions": {"asked": 0, "allowed": 0, "rejected": 0,
                            "gated": 0, "gate_failed": 0},
            "questions": 0,
        }],
    }
    state = RoundState.from_dict(data)
    assert state.agents[0].state is AgentState.READY
    assert state.agents[0].turns[0]["harvest"] == {"verdict": "READY", "reasons": []}


def _real_worktree(tmp_path, name: str, *, dirty: int = 0, committed: int = 0) -> Workspace:
    """A git worktree with *committed* files changed in a commit above the base
    and *dirty* more changed on disk — the heartbeat reads it through real git."""
    root = tmp_path / name
    root.mkdir()
    for i in range(6):
        _write(root / f"f{i}.py", "x = 0\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    for i in range(committed):
        _write(root / f"f{i}.py", "x = 1\n")
    if committed:
        _git(root, "commit", "-qam", "work")
    for i in range(committed, committed + dirty):
        _write(root / f"f{i}.py", "x = 2\n")
    return Workspace(agent=name, path=root, branch=name, base_sha=base, kind="worktree")


def _real_line(runs: list) -> str:
    state = RoundState(round_no=ROUND, ticket=TICKET, base_sha="0" * 40,
                       started_at=time.time() - 120, agents=runs)
    return _runner_module._Heartbeat(
        state, {r.agent.name: time.monotonic() - 60 for r in runs}, 0).line()


def _real_run(ws: Workspace, state: AgentState, attempt: int = 0, turns=()) -> AgentRun:
    run = AgentRun(agent=AgentSpec(name=ws.agent, provider_id="p", model_id=ws.agent),
                   workspace=ws, state=state, attempt=attempt)
    run.turns.extend(turns)
    return run


def _part(line: str, name: str) -> str:
    return next(p for p in line.split(" · ") if p.split(": ")[-1].startswith(name + " "))


def test_the_heartbeat_reads_real_worktrees_with_no_helper_mocked(tmp_path):
    """KC-27 §3–§4 end to end: `_worktree_files` and `_commits_above` run
    against real git — 1 and 3 files are 30 % and 60 % against a median of 2,
    a READY agent names its tests' time, and a rework rolls a committed agent
    back from 70 % to its files-count with `↺1` in place of `(attempt 1)`."""
    a = _real_run(_real_worktree(tmp_path, "aa", dirty=1), AgentState.WAITING)
    b = _real_run(_real_worktree(tmp_path, "bb", dirty=3), AgentState.WAITING)
    c = _real_run(_real_worktree(tmp_path, "cc"), AgentState.READY, turns=[
        {"harvest": {"verdict": "READY", "reasons": [], "elapsed": 240.0}}])
    line = _real_line([a, b, c])
    assert "aa WAITING 60s [###.......] 30% 1f" in _part(line, "aa"), line
    assert "bb WAITING 60s [######....] 60% 3f" in _part(line, "bb"), line
    assert _part(line, "cc").startswith("cc READY (tests 4m)"), line

    ws = _real_worktree(tmp_path, "rr", committed=1, dirty=1)
    first = _real_line([_real_run(ws, AgentState.WAITING, attempt=0)])
    again = _real_line([_real_run(ws, AgentState.WAITING, attempt=1)])
    assert "70% 2f" in first and "↺" not in first, first
    assert "↺1" in again and "70%" not in again and "(attempt" not in again, again


def test_the_packs_median_is_not_floored(tmp_path):
    """Working agents at 1 and 2 files have a median of 1.5: the one at 1 file
    is 40 %, not the 60 % an integer median of 1 would call "at the pack"."""
    a = _real_run(_real_worktree(tmp_path, "aa", dirty=1), AgentState.WAITING)
    b = _real_run(_real_worktree(tmp_path, "bb", dirty=2), AgentState.WAITING)
    line = _real_line([a, b])
    assert "40% 1f" in _part(line, "aa"), line
    assert "60% 2f" in _part(line, "bb"), line


# ─────────────────────────────────────────────────────────────────────────────
# KC-36: a turn that is still changing files extends its own deadline
# ─────────────────────────────────────────────────────────────────────────────

def _churn_ws(tmp_path: Path, agent: str = "agent-a") -> Workspace:
    """`_make_repo` as a `Workspace`: what `_churn` and the turn's clock read."""
    repo = _make_repo(tmp_path)
    return Workspace(agent=agent, path=repo.resolve(), branch="main",
                     base_sha=_git(repo, "rev-parse", "HEAD"), kind="worktree")


def _grow(directory: Path, seconds: float, every: float = 0.05) -> None:
    """One line appended every *every* seconds for *seconds*: a worktree that
    keeps moving while the turn clock runs. Line-buffered — a sample reads the
    file off the disk, so the writes have to get there."""
    path = directory / "pkg" / "growing.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", buffering=1) as fh:
        i = 0
        stop = time.monotonic() + seconds
        while time.monotonic() < stop:
            fh.write(f"x{i} = {i}\n")
            i += 1
            time.sleep(every)


class _RecordingBackend:
    """A `ContestBackend` that records every `wait_idle` call and answers idle."""

    def __init__(self):
        self.calls = []

    def wait_idle(self, session, timeout, **kwargs):
        self.calls.append({"timeout": timeout, **kwargs})
        return IdleResult(status="idle", elapsed=0.0)


def test_churn_counts_files_and_lines_for_untracked_tracked_and_committed(tmp_path):
    """`_churn` is the union of three reads: an untracked file's own lines come
    off the disk, a tracked edit and a commit come off `--numstat`."""
    ws = _churn_ws(tmp_path)
    repo = ws.path
    assert _runner_module._churn(ws) == (0, 0)

    # untracked: the path from `git status`, the lines from the file itself
    _write(repo / "pkg" / "new.py", "1\n2\n3\n")
    assert _runner_module._churn(ws) == (1, 3)

    # tracked: the edit lands on top of the untracked file
    _write(repo / "pkg" / "a.py", "a\nb\nc\n")
    assert _runner_module._churn(ws) == (2, 5)

    # committed above the base: the same work, read off the branch instead
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    assert _runner_module._churn(ws) == (2, 5)

    # `.smoke_tests/` is a link `sync_test_tiers.py` makes, not a turn: both
    # numbers ignore it, and so does the heartbeat's files count
    _write(repo / ".smoke_tests" / "x", "1\n2\n3\n4\n5\n")
    assert _runner_module._churn(ws) == (2, 5)
    assert _runner_module._worktree_files(ws) == 2


def test_churn_of_a_binary_edit_counts_the_file_not_its_lines(tmp_path):
    """A `-` in a `--numstat` line is a binary file: the path counts, the lines do not."""
    ws = _churn_ws(tmp_path)
    (ws.path / "pkg" / "blob.bin").write_bytes(b"\x00" * 64)
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", "blob")
    (ws.path / "pkg" / "blob.bin").write_bytes(b"\x00" * 128)
    assert _runner_module._churn(ws) == (1, 0)


def test_churn_of_a_missing_worktree_is_zero_without_raising(tmp_path):
    ws = Workspace(agent="agent-a", path=tmp_path / "gone", branch="main",
                   base_sha="0" * 40, kind="worktree")
    assert _runner_module._churn(ws) == (0, 0)


def test_churn_of_a_deleted_worktree_is_zero_without_raising(tmp_path):
    """The directory is gone between the sample and the read: `(0, 0)`, not an error."""
    import shutil
    ws = _churn_ws(tmp_path)
    shutil.rmtree(ws.path)
    assert _runner_module._churn(ws) == (0, 0)


def test_churn_when_git_fails_is_zero_without_raising(tmp_path, monkeypatch):
    """Any git failure is a sample of nothing — never an exception into a round."""
    ws = _churn_ws(tmp_path)
    _write(ws.path / "pkg" / "a.py", "a\nb\n")

    def boom(*args, **kwargs):
        raise OSError("git is gone")

    monkeypatch.setattr(_runner_module, "run_git", boom)
    assert _runner_module._churn(ws) == (0, 0)
    assert _runner_module._worktree_files(ws) == 0


def test_the_turn_deadline_grows_with_the_worktree(tmp_path, caplog):
    """Round 64 replayed at its own numbers: 1800 s nominal, 600 s per grant,
    7200 s cap. A worktree that grew 2 files and 346 lines since the last
    deadline is extended an hour; one flat since 11:56 with only heartbeats on
    the wire is not. Either way the sample is keyed by the run's worktree, never
    by a model id.
    """
    caplog.set_level(logging.INFO, logger=LOGGER)
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=1800, turn_extend_sec=600,
                      turn_max_sec=7200, idle_event_timeout_sec=0)
    spec = cfg.agents[0]

    flat = _runner_module._turn_deadline(AgentRun(agent=spec, workspace=ws), cfg)
    assert flat.on_deadline(1800.0) is None
    assert flat.extensions == []
    assert flat.granted == 0.0
    assert flat.last_sample == (0, 0)

    growing = _runner_module._turn_deadline(AgentRun(agent=spec, workspace=ws), cfg)
    _write(ws.path / "pkg" / "more.py", "x\n" * 344)
    _write(ws.path / "pkg" / "more2.py", "y\nz\n")
    assert growing.on_deadline(1800.0) == 600
    assert growing.extensions == [
        {"at": 1800.0, "files": 2, "lines": 346, "granted": 600}]
    assert growing.granted == 600.0
    (line,) = [l for l in _lines(caplog, "agent-a: WAITING") if " +" in l]
    assert line == "agent-a: WAITING — +10m at 30m (2 files, 346 lines)"


def test_a_flat_sample_is_refused_and_named_in_the_stall(tmp_path):
    """Flat churn is the old path: nothing is granted, and the stall line names
    the sample the deadline refused."""
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=600,
                      turn_max_sec=7200, idle_event_timeout_sec=0)
    clock = _runner_module._turn_deadline(AgentRun(agent=cfg.agents[0], workspace=ws), cfg)
    assert clock.on_deadline(600.0) is None
    assert clock.extensions == []
    assert clock.last_sample == (0, 0)
    assert _runner_module._no_idle_error(cfg, 600.0, clock) == \
        "no idle after 10m (0 files, 0 lines, unchanged for 10m)"


def test_growth_every_time_clips_the_last_grant_to_turn_max_sec(tmp_path):
    """An edit loop is bounded: a 600 s floor, 900 s per grant and a 2400 s cap
    gives 900 then 900, then nothing — and the total is exactly the cap."""
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=900,
                      turn_max_sec=2400, idle_event_timeout_sec=0)
    clock = _runner_module._turn_deadline(AgentRun(agent=cfg.agents[0], workspace=ws), cfg)
    for i in range(2):
        _write(ws.path / "pkg" / f"loop{i}.py", f"i = {i}\n")
        assert clock.on_deadline(float(cfg.turn_timeout_sec + clock.granted)) == 900
    assert clock.extensions == [
        {"at": 600.0, "files": 1, "lines": 1, "granted": 900},
        {"at": 1500.0, "files": 2, "lines": 2, "granted": 900}]
    # the cap is reached: the same growth grants nothing more
    _write(ws.path / "pkg" / "loop2.py", "j = 2\n")
    assert clock.on_deadline(float(cfg.turn_max_sec)) is None
    assert clock.granted + cfg.turn_timeout_sec == cfg.turn_max_sec


def test_wait_turn_passes_on_deadline_only_when_extension_is_armed(tmp_path):
    """The seam the runner uses: `turn_extend_sec = 0` hands nothing over, and
    an armed clock hands its callback over alongside the round's limits."""
    backend = _RecordingBackend()
    cfg0 = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=0,
                       turn_max_sec=7200, idle_event_timeout_sec=0)
    cfg1 = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=600,
                       turn_max_sec=7200, idle_event_timeout_sec=0)
    session = SessionRef(id="ses_one", provider_id="kenary", model_id="hy3:free",
                         directory="/nowhere", agent="agent-a")
    clock = _runner_module._TurnClock(on_deadline=lambda elapsed: 600.0)
    common = dict(on_permission=lambda event: ("once", ""), on_question=lambda event: None)

    _runner_module._wait_turn(backend, session, cfg0, **common)
    _runner_module._wait_turn(backend, session, cfg1, on_deadline=clock.on_deadline, **common)
    assert len(backend.calls) == 2
    assert "on_deadline" not in backend.calls[0]
    assert backend.calls[0]["timeout"] == 600.0
    assert backend.calls[0]["idle_event_timeout"] is None
    assert backend.calls[1]["on_deadline"] is clock.on_deadline



def test_mark_is_optional_on_a_backend(tmp_path):
    """KC-63: a backend without `mark` (this one) still runs a turn, and
    `since` goes over only when there is a mark to pass."""
    backend = _RecordingBackend()
    assert not hasattr(backend, "mark")
    cfg = make_config(["agent-a"], turn_timeout_sec=600, idle_event_timeout_sec=0)
    session = SessionRef(id="ses_one", provider_id="kenary", model_id="hy3:free",
                         directory="/nowhere", agent="agent-a")
    common = dict(on_permission=lambda event: ("once", ""), on_question=lambda event: None)

    assert _runner_module._wait_turn(backend, session, cfg, **common).status == "idle"
    _runner_module._wait_turn(backend, session, cfg, since=None, **common)
    _runner_module._wait_turn(backend, session, cfg, since=7, **common)
    assert "since" not in backend.calls[0]
    assert "since" not in backend.calls[1]
    assert backend.calls[2]["since"] == 7


def test_turn_extend_sec_zero_never_passes_on_deadline(tmp_path):
    """End to end, `turn_extend_sec = 0` is today's path: `wait_idle` never
    sees an `on_deadline` kwarg and the stall line is the one every earlier
    turn compares byte for byte."""
    calls = []
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=0,
                      turn_max_sec=7200, idle_event_timeout_sec=0)
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, cfg)
        real = h.backend.wait_idle

        def spying(session, timeout, **kwargs):
            calls.append({"timeout": timeout, **kwargs})
            return real(session, timeout, **kwargs)

        h.backend.wait_idle = spying
        run = h.go()
        aborted = _aborted(fake)
    assert calls, "no turn was waited on"
    assert all("on_deadline" not in call for call in calls), calls
    assert all(call["timeout"] == 1.0 for call in calls), calls
    assert run.state is AgentState.STALLED and aborted
    assert run.turns[0]["idle_status"] == "timeout"
    assert run.last_error == "no idle after 1s"
    assert "extensions" not in run.turns[0]


def test_a_flat_worktree_gets_no_extension_and_names_what_it_refused(tmp_path):
    """A turn whose worktree never moves: no `extensions` key, `STALLED`, and the
    sample in the error — round 64's glm-4-7-flash."""
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=60,
                      turn_max_sec=720, idle_event_timeout_sec=0)
    _sb, _fake, _h, run, aborted = _run_one(
        tmp_path, {"turns": [{"events": ["busy"], "idle": False}]}, cfg)
    assert run.state is AgentState.STALLED and aborted
    assert run.turns[0]["idle_status"] == "timeout"
    assert "extensions" not in run.turns[0]
    assert run.last_error.startswith("no idle after ")
    assert "(0 files, 0 lines, unchanged for" in run.last_error


def test_a_growing_worktree_is_extended_once(tmp_path, caplog):
    """A worktree whose line count grows between samples earns one extension,
    which lands in the turn as `extensions` and on stderr as a KC-18 line."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=1,
                      turn_max_sec=2, idle_event_timeout_sec=0)
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    scenario["turns"][0]["on_prompt"] = lambda d, t: _grow(Path(d), 3.0)
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)
    assert run.state is AgentState.STALLED and aborted
    assert run.turns[0]["idle_status"] == "timeout"
    (ext,) = run.turns[0]["extensions"]
    assert ext["files"] == 1 and ext["lines"] > 0 and ext["granted"] == 1, ext
    assert ext["at"] == pytest.approx(1.0, abs=0.6)
    # the stall line names the last sample the deadline refused: the same
    # file, grown by the time the extended deadline came due
    assert "(1 files," in run.last_error and "unchanged for" in run.last_error, \
        run.last_error
    (line,) = [l for l in _lines(caplog, "agent-a: WAITING") if " +" in l]
    assert line.startswith("agent-a: WAITING — +1s at "), line
    assert line.endswith(f"({ext['files']} files, {ext['lines']} lines)"), line


def test_growth_every_time_stops_at_turn_max_sec(tmp_path):
    """The last grant is clipped so the total is exactly `turn_max_sec`, and the
    turn still ends STALLED rather than running on."""
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=2,
                      turn_max_sec=4, idle_event_timeout_sec=0)
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    scenario["turns"][0]["on_prompt"] = lambda d, t: _grow(Path(d), 5.0, every=0.01)
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)
    assert run.state is AgentState.STALLED and aborted
    ext = run.turns[0]["extensions"]
    assert [e["granted"] for e in ext] == [2, 1], ext
    assert cfg.turn_timeout_sec + sum(e["granted"] for e in ext) == cfg.turn_max_sec
    assert (run.turns[0]["idle_at"] - run.turns[0]["sent_at"]) >= cfg.turn_max_sec - 0.5


# ─────────────────────────────────────────────────────────────────────────────
# KC-48: an ended agent leaves no process in its worktree
# ─────────────────────────────────────────────────────────────────────────────

#: A grace far inside the 5 s default: a TERM that is honoured is honoured in
#: milliseconds, so no test here waits out the grace.
_GRACE_SEC = 0.2

# Both payloads print READY *after* their handler is installed, so a test can
# wait for the child to be armed before the reap under test fires. Without the
# gate a TERM that lands during Python's own start-up is honoured, and a child
# meant to ignore SIGTERM dies by it.
_READY = "sys.stderr.write('READY\\n'); sys.stderr.flush()\n"
_KILL = "import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" \
        + _READY + "time.sleep(600)\n"
_TERM = "import sys, time\n" + _READY + "time.sleep(600)\n"


#: The holder a stray runs under: it starts the payload with its cwd where the
#: test wants it, relays READY with the payload's pid, and prints its exit code.
#: The holder's own cwd is `/`, outside every tree, so it is never a candidate;
#: the payload is this process's *grandchild* — the round 86 shape, where the
#: suites were no child of the runner — because the reap spares the runner's own
#: children (the backend's agent loop, the harvest's judge).
_HOLDER = """
import subprocess, sys
p = subprocess.Popen([sys.executable, "-c", sys.argv[2]], cwd=sys.argv[1],
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
if p.stderr.readline().strip() == "READY":
    print("READY", p.pid, flush=True)
print("RC", p.wait(), flush=True)
"""


class _Leaked:
    """A stray the reap must clean up, and never one that leaks out.

    `wait()` gives the stray's exit code for the TERM/KILL assertions; `kill()` in
    a `finally` covers the paths where the reap under test did not reach it — a
    failing test must not leave a sleeper on the box, which is the defect KC-48
    is about."""

    def __init__(self, cwd, code):
        self.holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(cwd), code],
                                       cwd="/", stdout=subprocess.PIPE, text=True)
        self.pid = None
        self.rc = None

    def ready(self, timeout=10.0):
        """Block until the stray has installed its handlers. Raises if it never did."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hit, _, _ = select.select([self.holder.stdout], [], [],
                                      max(0.0, end - time.monotonic()))
            if hit:
                word, _, pid = self.holder.stdout.readline().partition(" ")
                if word == "READY":
                    self.pid = int(pid)
                    return
        raise AssertionError("the stray never reported READY")

    def wait(self, timeout=10.0):
        end = time.monotonic() + timeout
        while self.rc is None and time.monotonic() < end:
            hit, _, _ = select.select([self.holder.stdout], [], [],
                                      max(0.0, end - time.monotonic()))
            if hit:
                word, _, rc = self.holder.stdout.readline().partition(" ")
                if word == "RC":
                    self.rc = int(rc)
        return self.rc

    def alive(self):
        """Whether the stray still runs — read off `/proc`, not the holder's
        pipe, so a KILL a moment ago already reads as gone."""
        try:
            with open(f"/proc/{self.pid}/stat", encoding="utf-8") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except (OSError, IndexError):
            return False

    def kill(self):
        if self.pid is not None and self.rc is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
        if self.pid is None:        # never armed: the holder is all there is to stop
            self.holder.kill()
        self.holder.wait(10)
        self.holder.stdout.close()


def _leaked(cwd, *, ignore_term=False):
    return _Leaked(cwd, _KILL if ignore_term else _TERM)


def test_a_stalled_agent_leaves_no_process_in_its_worktree(tmp_path, monkeypatch, caplog):
    """KC-48 §1-2: a silence stall ends the run and clears its worktree — the
    child that honours SIGTERM dies by TERM, the one that ignores it by KILL,
    the one in `sub/dir` too, the siblings and outsiders untouched, and the
    three killed children are on `reaped` in state.json. The test chdirs into
    the tree first, so the runner's self-exclusion is what keeps it alive."""
    monkeypatch.setattr("tools.contest.runner.REAP_GRACE_SEC", _GRACE_SEC)
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    # a second worktree next to the first: the round's normal shape
    sibling = ws.path.parent / "agent-b"
    _git(sb.repo, "worktree", "add", "-q", "-b", f"contest/{ROUND}/agent-b",
         str(sibling), sb.base_sha)
    (ws.path / "sub" / "dir").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(ws.path)          # the test's own cwd is inside the tree
    caplog.set_level(logging.INFO, logger="tools.contest.runner")

    honours, ignores, nested = _leaked(ws.path), _leaked(ws.path, ignore_term=True), \
        _leaked(ws.path / "sub" / "dir")
    keep = [_leaked(outside, ignore_term=True), _leaked(sibling, ignore_term=True)]
    for proc in [honours, ignores, nested, *keep]:
        proc.ready()          # the handler is installed before the reap fires
    try:
        cfg = _stall_config(idle_event_timeout_sec=1)
        with _BenchFake({"turns": [{"events": [], "idle": False}]}) as fake:
            state = _round(sb, fake, cfg)
        agent = _by_name(state)["agent-a"]
        assert agent.state is AgentState.STALLED, agent.last_error
        assert honours.wait() == -signal.SIGTERM
        assert ignores.wait() == -signal.SIGKILL
        assert nested.wait() == -signal.SIGTERM
        # read before the `finally` below tidies `keep` up
        kept_alive = [proc.alive() for proc in keep]
    finally:
        for proc in keep + [honours, ignores, nested]:
            proc.kill()
    assert all(kept_alive), kept_alive
    reaped = agent.reaped or []
    pids = [item["pid"] for item in reaped]
    assert set(pids) == {honours.pid, ignores.pid, nested.pid}, reaped
    cmds = {item["pid"]: item["cmd"] for item in reaped}
    assert all(len(cmd) <= 120 for cmd in cmds.values())
    assert "signal.SIG_IGN" in cmds[ignores.pid]
    assert os.getpid() not in pids               # the runner is never a candidate
    assert not any(p.pid in pids for p in keep), reaped

    reaped_json = {a["agent"]["name"]: a.get("reaped") for a in _state_json(sb)["agents"]}
    assert [i["pid"] for i in reaped_json["agent-a"]] == pids
    assert "agent-b" not in reaped_json
    assert _runner_has(caplog, "agent-a: reaped 3 processes left in the worktree")


def test_an_unreadable_proc_root_is_one_warning_and_an_empty_reap(tmp_path, monkeypatch, caplog):
    """KC-48 §3: no `/proc` is a best-effort miss, not an exception into a run —
    one WARNING per reap, an empty result, and the tree's processes are never
    signalled, so the agent still reaches its terminal state."""
    caplog.set_level(logging.WARNING, logger="tools.contest.runner")
    missing = str(tmp_path / "no-proc")
    monkeypatch.setattr("tools.contest.runner._PROC_ROOT", missing)
    bystander = _leaked(tmp_path, ignore_term=True)
    try:
        bystander.ready()
        assert _reap_worktree(tmp_path, name="agent-a", grace=_GRACE_SEC) == []
        assert bystander.alive()                  # nothing was signalled at all
    finally:
        bystander.kill()
    warns = [r for r in caplog.records if r.name == "tools.contest.runner"
             and r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    assert "no reap of" in warns[0].getMessage() and missing in warns[0].getMessage()

    sb = Sandbox(tmp_path)
    cfg = _stall_config(idle_event_timeout_sec=1)
    with _BenchFake({"turns": [{"events": [], "idle": False}]}) as fake:
        state = _round(sb, fake, cfg)
    agent = _by_name(state)["agent-a"]
    assert agent.state is AgentState.STALLED, agent.last_error
    assert agent.reaped is None
    assert "reaped" not in _state_json(sb)["agents"][0]


@pytest.mark.usefixtures("sigint_raises_keyboardinterrupt")
def test_the_rounds_end_reaps_every_worktree_on_ctrl_c(tmp_path, monkeypatch):
    """KC-48 §3: the round's own sweep runs on Ctrl-C too, in the main thread,
    over every worktree — the agent that has already ENDED and the one still
    mid-flight, which stays mid-flight in state.json for --resume.

    agent-b's second prompt is where Ctrl-C lands. It first waits for
    agent-a's `session.json`, which `run_agent` writes in the same `finally`
    right after agent-a's own reap, so the sleeper the hook then leaves in
    agent-a's tree can only be reached by the round's sweep; the one it leaves
    in its own tree is what a mid-flight agent's suite looks like at Ctrl-C."""
    monkeypatch.setattr("tools.contest.runner.REAP_GRACE_SEC", _GRACE_SEC)
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    ws_a, ws_b = sb.ws("agent-a"), sb.ws("agent-b")
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2, turn_timeout_sec=300)
    pid = os.getpid()
    calls, leaked = [], []
    real, main = _reap_worktree, threading.main_thread()

    def recording(worktree, *, name, grace=None):
        out = real(worktree, name=name, grace=grace)
        calls.append((name, str(worktree), threading.current_thread(), out))
        return out

    def leak_and_interrupt(directory, text):
        # agent-a's own reap is done: its session.json is written in the same
        # `finally`, right after it, so it is out before this hook runs on
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (sb.out_dir / "agent-a.session.json").is_file():
            time.sleep(0.02)
        assert (sb.out_dir / "agent-a.session.json").is_file(), \
            "agent-a never finished, so there is nothing left for the sweep to reap"
        for tree in (ws_a.path, ws_b.path):
            stray = _leaked(tree, ignore_term=True)
            leaked.append(stray)
            stray.ready()     # armed before Ctrl-C, so only the grace can kill it
        os.kill(pid, signal.SIGINT)

    scenario = {"turns": [
        {"on_prompt": lambda d, t: work_ready(d, t) if _agent_of(d) == "agent-a"
                               else work_no_test(d, t),
         "events": ["busy", "idle"]},
        {"on_prompt": leak_and_interrupt, "events": ["busy"], "idle": False},
    ]}
    monkeypatch.setattr("tools.contest.runner._reap_worktree", recording)
    try:
        with _BenchFake(scenario) as fake:
            with pytest.raises(KeyboardInterrupt):
                _round(sb, fake, cfg)
        rcs = [proc.wait() for proc in leaked]
    finally:
        for proc in leaked:
            proc.kill()

    assert rcs == [-signal.SIGKILL, -signal.SIGKILL], rcs
    stray_a, stray_b = leaked
    # the sweep ran in the main thread over both trees: agent-a's own reap had
    # already returned, and agent-b never reached one of its own, so the round's
    # exit is the only reap that could still reach either sleeper.
    for tree, stray in ((ws_a, stray_a), (ws_b, stray_b)):
        sweeps = [c for c in calls if c[2] is main and c[1] == str(tree.path)]
        assert sweeps, calls
        assert any(stray.pid in [i["pid"] for i in c[3]] for c in sweeps), calls
    # agent-b stays mid-flight for --resume, and carries what it left running
    saved = {a["agent"]["name"]: a for a in _state_json(sb)["agents"]}
    assert not AgentState(saved["agent-b"]["state"]).terminal
    assert stray_b.pid in [i["pid"] for i in saved["agent-b"]["reaped"]]


def test_a_stalled_turn_with_a_commit_is_reaped_before_its_harvest(tmp_path, monkeypatch):
    """KC-48 §1: a STALLED turn with a commit under it is harvested (KC-21) —
    and the harvest reads the tree and runs its suites only after the stray is
    gone, not next to it. The run still lands READY on its commit.

    The commit and the stray are made in `prepare`, before the round (FL-1): a
    hook that commits while the silence clock runs is a race on the box's load."""
    monkeypatch.setattr("tools.contest.runner.REAP_GRACE_SEC", _GRACE_SEC)
    leaked, seen = [], []
    real = _runner_module._harvest

    def harvest(*args, **kwargs):
        seen.append(leaked[0].alive())
        return real(*args, **kwargs)

    def commit_and_leak(worktree):
        _work(worktree, test=True)
        stray = _leaked(worktree, ignore_term=True)
        leaked.append(stray)
        stray.ready()

    monkeypatch.setattr(_runner_module, "_harvest", harvest)
    try:
        _sb, _fake, _h, run, _ = _run_one(tmp_path, {"turns": [{"events": [], "idle": False}]},
                                          _stall_config(), prepare=commit_and_leak)
        rc = leaked[0].wait()
    finally:
        for proc in leaked:
            proc.kill()
    assert run.state is AgentState.READY, run.last_error
    assert seen == [False]
    assert rc == -signal.SIGKILL
    assert [i["pid"] for i in run.reaped] == [leaked[0].pid]


def test_the_runners_own_child_in_the_worktree_is_not_the_agents_leftover(tmp_path, monkeypatch):
    """KC-48: `OpenRouterBackend` runs its agent loop as this process's child with
    `cwd=` the worktree, and `close()` ends it after the reap. It is not work the
    agent left running, so the reap neither signals nor records it — only a
    stray one level further down is."""
    monkeypatch.setattr("tools.contest.runner.REAP_GRACE_SEC", _GRACE_SEC)
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                             cwd=str(ws.path))
    stray = _leaked(ws.path)
    try:
        stray.ready()
        with _BenchFake({"turns": [{"events": [], "idle": False}]}) as fake:
            state = _round(sb, fake, _stall_config())
        rc = stray.wait()
        child_alive = child.poll() is None
    finally:
        stray.kill()
        child.kill()
        child.wait()
    assert child_alive
    assert rc == -signal.SIGTERM
    assert [i["pid"] for i in _by_name(state)["agent-a"].reaped] == [stray.pid]


def test_two_runs_on_one_model_id_extend_on_their_own_churn(tmp_path):
    """KC-36 §6: `hy3-var1` and `hy3-var2` share a model id and have their own
    worktrees. The one still writing is extended and runs on; the one that went
    flat stalls at the nominal clock."""
    from dataclasses import replace
    base = make_config(["hy3-var1", "hy3-var2"], max_parallel=2, turn_timeout_sec=1,
                       turn_extend_sec=1, turn_max_sec=4, idle_event_timeout_sec=0)
    cfg = replace(base, agents=tuple(
        AgentSpec(name=a.name, provider_id="kenary", model_id="hy3:free")
        for a in base.agents))
    assert {a.model for a in cfg.agents} == {"kenary/hy3:free"}

    sb = Sandbox(tmp_path, ["hy3-var1", "hy3-var2"])
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}

    def on_prompt(directory, text):
        if "hy3-var1" in directory:
            _grow(Path(directory), 5.0, every=0.01)
        else:
            time.sleep(5)

    scenario["turns"][0]["on_prompt"] = on_prompt
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir),
                          out_dir=sb.out_dir)

    flat, growing = _by_name(state)["hy3-var2"], _by_name(state)["hy3-var1"]
    assert flat.state is AgentState.STALLED
    assert "extensions" not in flat.turns[0]
    assert growing.state is AgentState.STALLED
    ext = growing.turns[0]["extensions"]
    assert ext and all(e["granted"] == 1 for e in ext), ext
    assert cfg.turn_timeout_sec + sum(e["granted"] for e in ext) == cfg.turn_max_sec
    span = lambda run: run.turns[0]["idle_at"] - run.turns[0]["sent_at"]
    assert span(growing) > span(flat) + 1.0


# ─────────────────────────────────────────────────────────────────────────────
# KC-65 — the agents' pytest worker count
# ─────────────────────────────────────────────────────────────────────────────

def _eight_cores(monkeypatch) -> None:
    """`core_count` reads `os.cpu_count`; the rule is stated for an 8-core box."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)


def _worker_state(tmp_path, states, create=True, **over):
    """One run per state at the state named: what `refresh` counts as live.

    The workspaces are paths only — `refresh` counts states and writes one file,
    so no git worktree stands behind them.
    """
    out = tmp_path / "out"
    if create:
        out.mkdir(parents=True, exist_ok=True)
    names = [f"agent-{i}" for i in range(len(states))]
    # KC-68: the crowd's rule alone — the suite slots split the box their own way
    over.setdefault("agent_suite_slots", 0)
    cfg = make_config(names, **over)
    runs = [
        AgentRun(agent=spec,
                 workspace=Workspace(agent=spec.name, path=Path(f"/tmp/{spec.name}"),
                                     branch=f"contest/{ROUND}/{spec.name}",
                                     base_sha="b", kind="worktree"),
                 state=AgentState(state))
        for spec, state in zip(cfg.agents, states)
    ]
    return out, cfg, RoundState(round_no=ROUND, ticket=TICKET, base_sha="b",
                                started_at=1.0, agents=runs)


def _note(workers: int) -> str:
    """The KC-65 lines the runner appends, as a test expects them."""
    return ("\n\nThe box is shared: right now `-n auto` gives your pytest "
            f"{workers} workers.\n"
            "Do not pass a larger `-n`; a run is slower when many agents test at once.\n")


def test_pytest_workers_follows_the_live_agents(monkeypatch):
    """live 1 -> the whole box, 2..4 -> pytest_workers_few, 5+ -> pytest_workers_min,
    always at most the cores."""
    _eight_cores(monkeypatch)
    cfg = make_config(["agent-a"], agent_suite_slots=0)
    for live, want in ((0, 8), (1, 8), (2, 4), (3, 4), (4, 4), (5, 2), (9, 2)):
        assert _runner_module.agent_pytest_workers(live, 8, cfg) == want
    assert _runner_module.agent_pytest_workers(3, 2, cfg) == 2, "capped at the cores"
    assert _runner_module.agent_pytest_workers(1, 2, cfg) == 2, "two cores, not four"


def test_a_fixed_pytest_workers_per_agent_is_not_capped(monkeypatch):
    """`pytest_workers_per_agent = 3` -> 3 whatever the cores."""
    _eight_cores(monkeypatch)
    cfg = make_config(["agent-a"], pytest_workers_per_agent=3)
    for live, cores in ((1, 8), (9, 8), (1, 2)):
        assert _runner_module.agent_pytest_workers(live, cores, cfg) == 3


def test_suite_slots_split_the_box_between_the_roots_that_can_run(monkeypatch):
    """KC-68: round 69 — eight live agents, two slots, 8 cores: 4 each, not the crowd's
    2, so the two roots that can run at once fill the box. One slot is the whole box;
    fewer live agents than slots split it by the agents; the crowd's count is a floor."""
    _eight_cores(monkeypatch)
    for slots, live, want in ((2, 8, 4), (1, 8, 8), (2, 1, 8), (4, 2, 4),
                              (3, 8, 2), (4, 4, 4), (16, 9, 2)):
        cfg = make_config(["agent-a"], agent_suite_slots=slots)
        assert _runner_module.agent_pytest_workers(live, 8, cfg) == want, (slots, live)
    cfg = make_config(["agent-a"], agent_suite_slots=1)
    assert _runner_module.agent_pytest_workers(8, 2, cfg) == 2, "capped at the cores"
    cfg = make_config(["agent-a"], agent_suite_slots=2, pytest_workers_per_agent=3)
    assert _runner_module.agent_pytest_workers(8, 8, cfg) == 3, "a fixed count still wins"


def test_the_worker_file_follows_the_slots_as_the_round_moves(tmp_path, monkeypatch):
    """KC-68: with two slots the file holds 4 for eight live agents and the whole box
    once one is left — the refresh reads the slots the same way the start does."""
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 8, agent_suite_slots=2)
    memo: dict = {}
    _runner_module.refresh_pytest_workers(out, cfg, state, memo)
    assert _runner_module.read_pytest_workers(out / _runner_module.WORKERS_FILE) == 4
    for run in state.agents[1:]:
        run.state = AgentState.READY
    _runner_module.refresh_pytest_workers(out, cfg, state, memo)
    assert _runner_module.read_pytest_workers(out / _runner_module.WORKERS_FILE) == 8


def test_round_live_agents_counts_the_runs_not_the_command_line(tmp_path):
    """On `--resume` the count is the round's agents minus the terminal ones, and it
    may outnumber the names on the command line."""
    _out, cfg, state = _worker_state(
        tmp_path, ["WAITING", "READY", "READY", "CREATED", "READY", "GAVE_UP"])
    assert _runner_module.round_live_agents(cfg, None) == 6
    assert _runner_module.round_live_agents(cfg, state) == 2


def test_the_worker_file_follows_the_round(tmp_path, monkeypatch):
    """6 live -> 2, two go READY -> 4, three go READY -> 4, one left -> 8. A queued
    CREATED agent holds no slot; a HARVESTING one does."""
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 6)
    path = out / _runner_module.WORKERS_FILE

    def count(states) -> int:
        for run, st in zip(state.agents, states):
            run.state = AgentState(st)
        _runner_module.refresh_pytest_workers(out, cfg, state, {})
        return int(path.read_text(encoding="utf-8"))

    assert count(["WAITING"] * 6) == 2
    assert count(["READY", "READY"] + ["WAITING"] * 2 + ["CREATED"] * 2) == 4
    assert count(["READY"] * 3 + ["WAITING"] * 3) == 4
    assert count(["READY"] * 5 + ["WAITING"]) == 8
    assert count(["CREATED"] * 6) == 8, "nothing holds a slot: the box is free"
    assert count(["WAITING"] * 3 + ["CREATED"] * 3) == 4, "CREATED is not live"
    assert count(["HARVESTING"] * 5 + ["WAITING"]) == 2, "HARVESTING holds a slot"


def test_refresh_writes_only_when_the_count_moves(tmp_path, monkeypatch):
    """A save that sees the same count does no I/O: the file already holds it,
    so the next save leaves it alone even though it starts with no memo of its
    own."""
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 6)
    writes = []

    def writing(target, value):
        writes.append(value)
        Path(target).write_text(f"{value}\n", encoding="utf-8")

    monkeypatch.setattr(_runner_module, "write_pytest_workers", writing)
    _runner_module.refresh_pytest_workers(out, cfg, state, {})
    _runner_module.refresh_pytest_workers(out, cfg, state, {})
    assert writes == [2]


def test_ten_agents_on_eight_cores_ask_for_two_and_never_more(tmp_path, monkeypatch):
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 10)
    _runner_module.refresh_pytest_workers(out, cfg, state, {})
    assert (out / _runner_module.WORKERS_FILE).read_text(encoding="utf-8") == "2\n"


def test_a_fixed_worker_count_is_written_once_and_the_round_never_moves_it(
        tmp_path, monkeypatch):
    """The CLI writes a fixed count; `run_round` must not touch it, so the operator's
    number stays the number."""
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 6,
                                    pytest_workers_per_agent=3)
    writes = []
    monkeypatch.setattr(_runner_module, "write_pytest_workers",
                        lambda target, value: writes.append(value))
    _runner_module.refresh_pytest_workers(out, cfg, state, {})
    assert writes == []
    assert not (out / _runner_module.WORKERS_FILE).exists()


def test_an_unwritable_worker_file_is_one_log_line(tmp_path, monkeypatch, caplog):
    """No directory, no workers file, one warning: the agents' pytest then takes
    xdist's own answer."""
    _eight_cores(monkeypatch)
    out, cfg, state = _worker_state(tmp_path, ["WAITING"] * 6, create=False)
    with caplog.at_level(logging.WARNING, logger="tools.contest.runner"):
        _runner_module.refresh_pytest_workers(out, cfg, state, {})
    assert not out.exists()
    lines = [m for m in caplog.messages if "pytest-workers" in m]
    assert len(lines) == 1
    assert lines[0].startswith("could not write")


def test_a_round_whose_worker_file_will_not_write_still_runs(tmp_path, monkeypatch, caplog):
    """A write that fails is one log line and one fewer env var, never a stopped round."""
    _eight_cores(monkeypatch)

    def refusing(target, value):
        raise OSError("no such file or directory")

    monkeypatch.setattr(_runner_module, "write_pytest_workers", refusing)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    with _BenchFake(scenario) as fake:
        with caplog.at_level(logging.WARNING, logger="tools.contest.runner"):
            state = _round(sb, fake, cfg)
        assert not fake.turn_errors
    _assert_ready(_by_name(state)["agent-a"], sb.ws("agent-a"))
    lines = [m for m in caplog.messages if "pytest-workers" in m]
    assert len(lines) == 1, "one warning, and the round went on"
    (_sid, text), = _prompts(fake)
    assert "The box is shared" not in text, "no file, no guess"


def test_every_prompt_names_the_count_of_that_moment(tmp_path, monkeypatch):
    """The note is read from the file at send time, not cached when the round
    starts: the first prompt names the crowd's count, the round rewrites the
    file with the whole box before the `continue`, and the `continue` names the
    whole box. The line is the last one, so a `continue_message` paragraph stays
    where it is."""
    _eight_cores(monkeypatch)
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], agent_suite_slots=0)

    def count_for(live: int) -> int:
        """The runner's own rule, through the runner's own writer."""
        value = _runner_module.agent_pytest_workers(live, 8, cfg)
        _runner_module.write_pytest_workers(sb.out_dir / _runner_module.WORKERS_FILE, value)
        return value

    few = count_for(2)                      # two live: pytest_workers_few
    assert few == 4

    seen: list = []
    whole = [0]

    def on_prompt(directory, text):
        """The first prompt: edit and go idle dirty, so a `continue` follows;
        the round rewrites the file with the whole box before it is sent."""
        seen.append(text)
        work_edit_no_commit(directory, text)
        whole[0] = count_for(1)               # one live left: the whole box

    def on_second(directory, text):
        seen.append(text)
        work_ready(directory, text)

    scenario = {"turns": [{"on_prompt": on_prompt, "events": ["busy", "idle"]},
                          {"on_prompt": on_second, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
        assert not fake.turn_errors
    _assert_ready(run, sb.ws("agent-a"))
    first, second = seen
    assert first.endswith(_note(few))
    assert second.endswith(_note(whole[0]))
    assert whole[0] > few, "fewer agents: more workers each"
    assert second.count("The box is shared") == 1, "the line is the last one"


def test_no_worker_line_when_the_file_cannot_be_read(tmp_path):
    """No `pytest-workers` file, no line and no guess: the prompt is byte-for-byte
    `round_prompt`."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
    _assert_ready(run, sb.ws("agent-a"))
    (_sid, text), = _prompts(fake)
    assert text == round_prompt("agent-a", sb.ticket_path, sb.base_sha,
                                tmp_dir=_scratch_arg(cfg))


def test_no_worker_line_when_a_fixed_count_equals_every_core(tmp_path, monkeypatch):
    """`pytest_workers_per_agent` is the box itself: the note would only repeat the
    operator's own number."""
    _eight_cores(monkeypatch)
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], pytest_workers_per_agent=8)
    _runner_module.write_pytest_workers(sb.out_dir / _runner_module.WORKERS_FILE, 8)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
    _assert_ready(run, sb.ws("agent-a"))
    (_sid, text), = _prompts(fake)
    assert "The box is shared" not in text


def test_the_agents_tmpdir_is_scratch_the_policy_allows(tmp_path):
    """The round moved every agent's shell onto a dir the policy never saw, so its
    `/*` glob joins `tmp_roots`: a `tmp_path` fixture is a mechanical `once`, not a gate
    call. Without the dir the same path is the gate's to answer."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], tmp_roots=())
    agent_tmp = tmp_path / "scratch" / "contest-45"
    scenario = {"turns": [_permission_turn(work_ready, [str(agent_tmp) + "/*"])]}

    allowed = make_policy(cfg, "reject")
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg, policy=allowed, agent_tmp=agent_tmp).go()
        (replied,) = fake.events_of("permission.replied")
    _assert_ready(run, sb.ws("agent-a"))
    assert replied["properties"]["reply"] == "once"
    assert allowed._completion_fn.calls == 0, "geometry, not the gate"
    line = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")[-1]
    assert line["layer"] == "mechanical"
    assert line["reason"] == "inside worktree/tmp_roots"

    denied = make_policy(cfg, "reject")
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, cfg, policy=denied).go()
        (replied,) = fake.events_of("permission.replied")
    _assert_ready(run, sb.ws("agent-a"))
    assert replied["properties"]["reply"] == "reject"
    assert denied._completion_fn.calls == 1, "no dir, so the gate answers"


# ─────────────────────────────────────────────────────────────────────────────
# KC-66: the gate's 429 is the agent's time, granted back to the turn
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimitedGate:
    """A gate transport that 429s, waiting between its tries.

    It runs the transport's own loop — ``error_retries`` tries, ``_sleep_fn``
    between them — so ``gate_attempts`` stays the sleeps plus one, exactly as on
    the wire. *limited* is how many 429s the limiter sends before it lifts.
    """

    def __init__(self, verdict, limited):
        self.verdict = verdict
        self.limited = limited
        self.calls = 0

    def __call__(self, url, headers, payload, timeout, **kw):
        retries = kw.get("error_retries", 0)
        wait = kw.get("error_retry_wait_sec", 0.0)
        sleep_fn = kw.get("_sleep_fn")
        for attempt in range(retries + 1):
            self.calls += 1
            if self.calls > self.limited:
                return json.dumps({"verdict": self.verdict, "reason": "stub"})
            if attempt == retries:
                break
            if sleep_fn is not None:
                sleep_fn(wait)
        raise RuntimeError(f"HTTP 429 from {url}: free-model rate limit reached")


def test_a_gate_that_waits_gets_its_seconds_back_into_the_turn(tmp_path):
    """Two 429s of 30 s, then an allow: the action goes through, the turn is
    longer by the two minutes the gate waited, and the tries and the added time
    are in the decision log and in the turn."""
    cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",),
                      gate_retries=4, gate_retry_wait_sec=30.0)
    gate = _RateLimitedGate("allow", 2)
    slept: list = []
    policy = Policy(cfg, completion_fn=gate, clock=time.monotonic,
                    sleep=lambda seconds: slept.append(seconds))
    sb, fake, _h, run, _ = _run_one(
        tmp_path, {"turns": [_permission_turn(work_ready, ["/var/lib/*"])]},
        cfg, policy)

    _assert_ready(run, sb.ws("agent-a"))
    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "once"
    assert gate.calls == 3
    assert slept == [30.0, 30.0]
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["gate_attempts"] == 3
    assert line["gate_added_sec"] == 120
    assert run.turns[0]["gate_attempts"] == 3
    assert run.turns[0]["gate_added_sec"] == 120
    (turn,) = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert turn["gate_added_sec"] == 120 and turn["gate_attempts"] == 3


def test_a_gate_that_429s_every_try_is_rejected_after_five_tries(tmp_path):
    """The KC-66 budget runs out: five tries, four 30 s waits, a gate-failed
    reject — and the turn is still longer by the three minutes the gate spent
    waiting."""
    cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",),
                      gate_retries=4, gate_retry_wait_sec=30.0)
    slept: list = []
    policy = Policy(cfg, completion_fn=_RateLimitedGate("allow", 99),
                    clock=time.monotonic,
                    sleep=lambda seconds: slept.append(seconds))
    sb, fake, _h, run, _ = _run_one(
        tmp_path, {"turns": [_permission_turn(work_ready, ["/var/lib/*"])]},
        cfg, policy)

    _assert_ready(run, sb.ws("agent-a"))
    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "reject"
    assert policy._completion_fn.calls == 5
    assert slept == [30.0] * 4
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "gate-failed"
    assert line["gate_attempts"] == 5
    assert line["gate_added_sec"] == 180
    assert run.turns[0]["gate_added_sec"] == 180
    assert run.turns[0]["gate_attempts"] == 5


def test_a_gate_that_never_429s_adds_nothing_to_the_turn(tmp_path):
    """No wait, no tries, no time back — and no `gate_*` keys on the turn."""
    cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",),
                      gate_retries=4, gate_retry_wait_sec=30.0)
    policy = make_policy(cfg, "allow")
    sb, fake, _h, run, _ = _run_one(
        tmp_path, {"turns": [_permission_turn(work_ready, ["/var/lib/*"])]},
        cfg, policy)

    _assert_ready(run, sb.ws("agent-a"))
    assert policy._completion_fn.calls == 1
    (line,) = _jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert "gate_attempts" not in line and "gate_added_sec" not in line
    assert "gate_added_sec" not in run.turns[0]
    assert "gate_attempts" not in run.turns[0]


def test_the_gate_grant_leaves_the_churn_room_alone(tmp_path):
    """The gate's recovery is not progress: a turn that spent three minutes on a
    busy gate key still has its whole churn budget left, and the two halves sit
    side by side on the clock instead of one eating the other."""
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=600,
                      turn_max_sec=900, idle_event_timeout_sec=0)
    clock = _runner_module._turn_deadline(
        AgentRun(agent=cfg.agents[0], workspace=ws), cfg)

    assert clock.grant_gate(180.0, 5) == 180.0
    assert clock.gate_added == 180.0 and clock.gate_attempts == 5
    assert clock.granted == 0.0, "recovery is not progress"

    # the churn still finds the whole 300 s the cap leaves over the floor
    _write(ws.path / "pkg" / "grew.py", "x\n")
    assert clock.on_deadline(float(cfg.turn_timeout_sec + 1.0)) == 300.0
    assert clock.granted == 300.0
    # the cap is reached: the same growth grants nothing more
    _write(ws.path / "pkg" / "grew2.py", "y\n")
    assert clock.on_deadline(float(cfg.turn_max_sec + 1.0)) is None

    # and nothing malformed buys time
    for seconds, tries in (("a lot", 3), (0.0, 3), (60.0, 0), (60.0, "five")):
        assert clock.grant_gate(seconds, tries) == 0.0
    assert clock.gate_added == 180.0 and clock.gate_attempts == 5


# ─────────────────────────────────────────────────────────────────────────────
# KC-58: an agent's own full suite waits for a round-wide slot
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suite_slots_are_clean():
    """The round's suite slots are module state shared by every test here.

    `reset` clears the queue and stops the poller thread, so a holder or a
    waiter left behind by one test cannot bleed into the next through the
    shared `_SUITE_SLOTS`. It runs before each test too, so a test that froze
    the clock and left a poller behind cannot hand it to its neighbour.
    """
    _runner_module._SUITE_SLOTS.reset()
    yield
    _runner_module._SUITE_SLOTS.reset()


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    """Poll *predicate* until it is true or the deadline passes — no event to wire."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _SuiteSleeper:
    """A process that reads to the scan as a `pytest` in its worktree, running nothing.

    `[python, -c, "<sleep>", "pytest"]` — the trailing argument is `sys.argv[1]`
    for a `-c` script, which python ignores — so `/proc` shows a live `pytest`
    whose cwd is the worktree and no test suite runs. That trailing argument is
    the whole point: it is what makes the process KC-58's signal.
    """

    CODE = "import time\ntime.sleep(600)\n"

    def __init__(self, worktree):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", self.CODE, "pytest"], cwd=str(worktree),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pid = self.proc.pid

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(5)
        except OSError:
            pass


def _suite_scenario(command: str, *, delay: float = 0.0, idle: bool = True,
                     on_prompt=None) -> dict:
    """One turn that asks for a whole pytest root, as Kilo sends it.

    `delay` keeps the turn alive after the reply, so the asker still holds its
    slot while a test arranges the next one. `on_prompt` is the prompt hook —
    the git work that makes the harvest accept the turn instead of reworking
    it, which is what a READY assertion needs.
    """
    turn = {"events": ["busy"],
            "permission": {"permission": "bash", "metadata": {"command": command}},
            "idle": idle}
    if on_prompt is not None:
        turn["on_prompt"] = on_prompt
    if delay:
        turn["delay"] = delay
    return {"turns": [turn]}


def test_only_a_bash_ask_can_hold_a_suite_slot():
    """`external_directory` and `doom_loop` are about where a command reaches, not
    how long it runs: only a `bash` ask whose command is a whole root holds one.
    A whole root waits for the slot; naming a subset is answered at once."""
    for whole_root in ("pytest tests", "python3 -m pytest tests -n 4", "pytest -n 4",
                       "cd . && python3 -m pytest tests -n 4"):
        assert _runner_module._is_full_suite_ask(
            {"permission": "bash", "metadata": {"command": whole_root}}), whole_root
    for targeted in ("pytest tests/test_runner.py -q", "pytest tests/test_x.py::test_y",
                     "pytest -k foo", "pytest tests -m slow"):
        assert not _runner_module._is_full_suite_ask(
            {"permission": "bash", "metadata": {"command": targeted}}), targeted
    assert not _runner_module._is_full_suite_ask(
        {"permission": "external_directory", "metadata": {"command": "pytest tests"}})
    assert not _runner_module._is_full_suite_ask({"permission": "bash", "metadata": {}})
    assert not _runner_module._is_full_suite_ask({})
    assert not _runner_module._is_full_suite_ask(None)


def test_the_suite_settings_degrade_to_their_defaults():
    """A config without the key, or one that cannot be read, arms the ticket's
    defaults rather than raising into a round."""
    assert _runner_module._suite_slots_armed(None) == 1
    assert _runner_module._suite_slots_armed(make_config(["agent-a"])) == 1
    assert _runner_module._suite_slots_armed(
        make_config(["agent-a"], agent_suite_slots=0)) == 0
    assert _runner_module._suite_slots_armed(
        make_config(["agent-a"], agent_suite_slots=3)) == 3

    class BadSlots:
        agent_suite_slots = "soon"

    assert _runner_module._suite_slots_armed(BadSlots()) == 1

    assert _runner_module._suite_ceiling(None) == 900.0
    assert _runner_module._suite_ceiling(make_config(["agent-a"])) == 900.0
    assert _runner_module._suite_ceiling(
        make_config(["agent-a"], agent_suite_max_sec=150)) == 150.0
    assert _runner_module._suite_ceiling(
        make_config(["agent-a"], agent_suite_max_sec=0)) == 0.0


def test_two_agents_share_one_suite_slot(tmp_path, monkeypatch):
    """KC-58 acceptance: two agents each ask for `python3 -m pytest tests -n 4`
    with `agent_suite_slots = 1`. The first answers at once and keeps the slot
    while its pytest runs; the second's reply goes out only after the first
    one's processes are gone. The `/proc` scan is patched and no suite runs.
    """
    names = ("agent-a", "agent-b")
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, agent_suite_slots=1, idle_event_timeout_sec=0)
    path_a = _runner_module._path_of(sb.ws(names[0]).path)
    holding = {path_a: {4242}}          # agent-a's suite is up in its worktree
    monkeypatch.setattr(_runner_module, "_suite_pids",
                        lambda worktree, proc_root=None:
                        holding.get(_runner_module._path_of(worktree), set()))
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    slots = _runner_module._SUITE_SLOTS

    with _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, delay=20.0,
                                     on_prompt=work_ready)) as fa, \
         _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, delay=20.0,
                                     on_prompt=work_ready)) as fb:
        ha = Harness(sb, fa, cfg, agent=names[0])
        hb = Harness(sb, fb, cfg, agent=names[1])
        results = {}

        def go(agent, h):
            results[agent] = h.go()

        ta = threading.Thread(target=go, args=(names[0], ha), daemon=True)
        ta.start()
        assert _wait_for(lambda: fa.calls(method="POST", prefix="/permission/")), \
            "the first agent never got its reply"
        replied_a = time.monotonic()
        assert slots.status(names[0])[0] == "running", slots.status(names[0])

        tb = threading.Thread(target=go, args=(names[1], hb), daemon=True)
        tb.start()
        # the second stands behind the first one's suite, one ahead of it
        assert _wait_for(lambda: slots.status(names[1]) is not None)
        queued = slots.status(names[1])
        assert queued[0] == "queued" and queued[2] == 1, queued
        assert not fb.calls(method="POST", prefix="/permission/"), \
            "the second reply went out while the first one's suite was still up"

        holding[path_a] = set()          # the first one's processes are gone
        cleared = time.monotonic()
        assert _wait_for(lambda: fb.calls(method="POST", prefix="/permission/")), \
            "the second reply never went out"
        replied_b = time.monotonic()
        for thread in (ta, tb):          # the fakes are still up: the streams end
            thread.join(60)              # with them, and a stream gone is ERROR
        assert not ta.is_alive() and not tb.is_alive()
        assert replied_b >= cleared > replied_a
        assert results[names[0]].state is AgentState.READY, results[names[0]].last_error
        assert results[names[1]].state is AgentState.READY, results[names[1]].last_error


def test_agent_suite_slots_zero_answers_every_ask_immediately(tmp_path, monkeypatch):
    """KC-58 acceptance: `agent_suite_slots = 0` is today's path. No slot is
    taken and nothing is queued — both replies go out at once, even while a
    `pytest` is up in the worktree, where a queue would have held them.
    """
    names = ("agent-a", "agent-b")
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, agent_suite_slots=0, idle_event_timeout_sec=0)
    monkeypatch.setattr(_runner_module, "_suite_pids", lambda worktree, proc_root=None: {4242})
    asked = []
    real_acquire = _runner_module._SuiteSlots.acquire

    def refusing(name, worktree, ceiling=None, stop=None, scoped=False):
        asked.append(name)
        return real_acquire(name, worktree, ceiling=ceiling, stop=stop, scoped=scoped)

    monkeypatch.setattr(_runner_module._SuiteSlots, "acquire", refusing)

    with _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, on_prompt=work_ready)) as fa, \
         _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, on_prompt=work_ready)) as fb:
        ha, hb = Harness(sb, fa, cfg, agent=names[0]), Harness(sb, fb, cfg, agent=names[1])
        ta = threading.Thread(target=ha.go, daemon=True)
        tb = threading.Thread(target=hb.go, daemon=True)
        ta.start()
        tb.start()
        assert _wait_for(lambda: fa.calls(method="POST", prefix="/permission/"))
        assert _wait_for(lambda: fb.calls(method="POST", prefix="/permission/"))
        ta.join(60)
        tb.join(60)
        assert ha.run.state is AgentState.READY, ha.run.last_error
        assert hb.run.state is AgentState.READY, hb.run.last_error
    assert not asked, "0 slots must not ask for one"
    assert not _runner_module._SUITE_SLOTS._entries, "nothing may be queued"


def test_a_stalled_holder_gives_its_slot_to_the_next_waiter(tmp_path, monkeypatch):
    """KC-58 acceptance: a session that stalls while holding a slot releases it
    and the next queued agent is answered. The scan keeps seeing the holder's
    pytest, so only the agent ending can free the slot — the release on the way
    out, not the poll.
    """
    names = ("agent-a", "agent-b")
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, agent_suite_slots=1, idle_event_timeout_sec=1)
    path_a = _runner_module._path_of(sb.ws(names[0]).path)
    monkeypatch.setattr(_runner_module, "_suite_pids",
                        lambda worktree, proc_root=None:
                        {4242} if _runner_module._path_of(worktree) == path_a else set())
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    slots = _runner_module._SUITE_SLOTS

    with _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, idle=False)) as fa, \
         _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, idle=False)) as fb:
        ha, hb = Harness(sb, fa, cfg, agent=names[0]), Harness(sb, fb, cfg, agent=names[1])
        results = {}

        def go(agent, h):
            results[agent] = h.go()

        ta = threading.Thread(target=go, args=(names[0], ha), daemon=True)
        ta.start()
        assert _wait_for(lambda: slots.status(names[0]) is not None)
        assert slots.status(names[0])[0] == "running"
        tb = threading.Thread(target=go, args=(names[1], hb), daemon=True)
        tb.start()
        assert _wait_for(lambda: slots.status(names[1]) is not None)
        assert slots.status(names[1])[0] == "queued"
        assert not fb.calls(method="POST", prefix="/permission/")

        assert _wait_for(lambda: not ta.is_alive(), 60)     # agent-a stalls
        ta.join(60)
        assert slots.status(names[0]) is None, "the slot must not survive its agent"
        assert _wait_for(lambda: fb.calls(method="POST", prefix="/permission/"), 60), \
            "the next queued agent was never answered"
        tb.join(60)
    assert results[names[0]].state is AgentState.STALLED, results[names[0]].last_error
    assert results[names[1]].state is AgentState.STALLED, results[names[1]].last_error


def test_a_background_suite_holds_its_slot_until_its_process_exits(tmp_path, monkeypatch):
    """KC-58 acceptance: a suite started in the background holds the slot while
    its pytest process runs in the worktree and releases it when the process
    exits. No `release` is called — the poll is what finds the process gone.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_runner_module, "time", clock)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    ws = _churn_ws(tmp_path)
    sleeper = _SuiteSleeper(ws.path)
    slots = _runner_module._SUITE_SLOTS
    try:
        assert _runner_module._suite_pids(ws.path) == {sleeper.pid}, "the scan must see it"
        assert slots.acquire("agent-a", ws.path) == 0.0
        entered = {}

        def hold():
            entered["waited"] = slots.acquire("agent-b", ws.path)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        assert slots.status("agent-b")[0] == "queued"
        clock.advance(30.0)            # the background suite takes thirty seconds
        assert thread.is_alive(), "the process is still up, so the slot must still be held"
        sleeper.kill()                 # ... until the process exits
        assert _wait_for(lambda: not thread.is_alive(), 60)
        thread.join(60)
        assert entered["waited"] == pytest.approx(30.0, abs=1.0)
        # the hold is gone but the entry is not: only an explicit release drops it
        assert slots.status("agent-a") == ("done", 30.0, 0)
        # agent-b holds the slot now; whether the poll has already noticed that
        # no pytest is left is a timing question, not something this test owns
        second = slots.status("agent-b")
        assert second[0] in ("running", "done") and second[2] == 0, second
    finally:
        sleeper.kill()
        slots.release("agent-a")
        slots.release("agent-b")


def test_a_holder_past_its_ceiling_stops_blocking_without_a_kill(tmp_path, monkeypatch):
    """KC-58 acceptance: past `agent_suite_max_sec` the holder stops blocking —
    the next waiter goes in beside it — and the holder's process is not
    signalled. The ceiling is a queue rule, not a kill.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_runner_module, "time", clock)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    ws = _churn_ws(tmp_path)
    sleeper = _SuiteSleeper(ws.path)
    slots = _runner_module._SUITE_SLOTS
    try:
        assert slots.acquire("agent-a", ws.path, ceiling=100.0) == 0.0
        entered = {}

        def wait():
            entered["waited"] = slots.acquire("agent-b", ws.path, ceiling=100.0)

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        assert slots.status("agent-b")[0] == "queued"
        clock.advance(90.0)
        # one short of the ceiling: the queue still blocks
        assert not _wait_for(lambda: slots.status("agent-b")[0] != "queued", 0.3)
        assert slots.status("agent-a") == ("running", 90.0, 0)
        # past it: the waiter goes in beside the holder, which keeps running
        clock.advance(20.0)
        assert _wait_for(lambda: not thread.is_alive(), 60)
        thread.join(60)
        assert entered["waited"] == pytest.approx(110.0, abs=1.0)
        assert slots.status("agent-a")[0] == "over"
        assert slots.status("agent-a")[1] == pytest.approx(110.0, abs=1.0)
        assert sleeper.alive, "the ceiling must not signal the holder's pytest"
    finally:
        sleeper.kill()
        slots.release("agent-a")
        slots.release("agent-b")


class _RecordingSuiteSlots:
    """The round's slots, wrapped to count who asked for one."""

    def __init__(self):
        self.acquired: list = []
        self.released: list = []
        self.limit = 1
        self.holders: dict = {}

    def configure(self, slots):
        self.limit = int(slots)
        return self.limit

    def acquire(self, name, worktree, ceiling=None, stop=None, scoped=False):
        self.acquired.append((name, ceiling, scoped))
        self.holders[name] = ("running", 0.0, 0)
        return 0.0

    def release(self, name):
        self.released.append(name)
        self.holders.pop(name, None)

    def status(self, name):
        return self.holders.get(name)

    def ahead(self, name):
        return 0


def test_the_harvest_takes_a_suite_slot_too_and_gives_it_back(tmp_path, monkeypatch):
    """KC-58: the judge's roots take one of the round's slots, so an agent's own
    `pytest tests -n 4` never runs beside them, and the harvest's own budget is
    its ceiling. `agent_suite_slots = 0` skips that queue for good."""
    import tools.contest.harvest as harvest_module

    sb = Sandbox(tmp_path, ["agent-a"])
    holder = _RecordingSuiteSlots()
    monkeypatch.setattr(_runner_module, "_SUITE_SLOTS", holder)
    got = {}

    def roots(ws, ticket_path, *, run_tests=False, budget_sec=0.0, waited=0.0, ahead=0):
        got.update(run_tests=run_tests, budget=budget_sec, ahead=ahead)
        return harvest_module.Harvest(verdict="READY", reasons=(), commit=None, facts={},
                                      elapsed=1.0, waited=waited, ahead=ahead)

    monkeypatch.setattr(_runner_module, "harvest", roots)

    cfg = make_config(["agent-a"], agent_suite_slots=1, harvest_budget_sec=750)
    assert _runner_module._harvest(
        sb.ws("agent-a"), sb.ticket_path, True, cfg).verdict == "READY"
    # its own key, scoped to the call: an agent's own slot is never touched
    assert holder.acquired == [("agent-a:harvest", 750.0, True)], holder.acquired
    assert holder.released == ["agent-a:harvest"], holder.released
    assert got == {"run_tests": True, "budget": 750.0, "ahead": 0}

    # 0 slots: no queue to stand in, and the roots still run — today's path
    monkeypatch.setattr(_runner_module, "_SUITE_SLOTS", _runner_module._SuiteSlots())
    _runner_module._SUITE_SLOTS.configure(0)
    got.clear()
    cfg0 = make_config(["agent-a"], agent_suite_slots=0, harvest_budget_sec=750)
    assert _runner_module._harvest(
        sb.ws("agent-a"), sb.ticket_path, True, cfg0).verdict == "READY"
    assert got == {"run_tests": True, "budget": 750.0, "ahead": 0}


def test_the_heartbeat_names_the_suite_queue_and_its_ceiling(tmp_path, monkeypatch):
    """KC-58: a WAITING agent reads `queued` with its wait and how many are ahead
    of it, the holder reads `suite`, and one past its ceiling says so — the queue
    that was invisible to the round is on the line."""
    names = ("agent-a", "agent-b", "agent-c")
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names)
    runs = [AgentRun(agent=cfg.agents[i], workspace=sb.ws(names[i]), state=AgentState.WAITING)
            for i in range(3)]
    hb, rm, files, commits = _make_heartbeat(runs, {n: 3 for n in names})
    try:
        monkeypatch.setattr(rm, "_SUITE_SLOTS", _RecordingSuiteSlots())
        rm._SUITE_SLOTS.holders.update({
            "agent-a": ("queued", 720.0, 2),
            "agent-b": ("running", 600.0, 0),
            "agent-c": ("over", 960.0, 0),
        })
        line = hb.line()
    finally:
        rm._worktree_files, rm._commits_above = files, commits
    assert "agent-a WAITING" in line and "(suite queued 12m, 2 ahead)" in line, line
    assert "(suite 10m)" in line, line
    assert "(suite 16m, over the ceiling)" in line, line


def test_parts_say_working_sees_a_running_bash_and_a_recent_edit():
    """KC-58 §2: a `bash` part that has not come back is work, and so is an
    `edit`/`write` completed inside the window — a `bash` that finished and an
    `edit` older than the window are neither."""
    now = time.time()
    window = 600.0
    parts = {
        "running": {"type": "tool", "tool": "bash",
                    "state": {"status": "running", "input": {"command": TEST_SUITE_COMMAND}}},
        "pending": {"type": "tool", "tool": "bash", "state": {"status": "pending"}},
        "done_bash": {"type": "tool", "tool": "bash",
                      "state": {"status": "completed", "input": {"command": TEST_SUITE_COMMAND}}},
        "recent_edit": {"type": "tool", "tool": "edit",
                        "state": {"status": "completed", "time": {"end": now - 4}}},
        "old_edit": {"type": "tool", "tool": "edit",
                     "state": {"status": "completed", "time": {"end": now - 7200}}},
        "read": {"type": "tool", "tool": "read",
                 "state": {"status": "completed", "time": {"end": now - 1}}},
    }
    f = _runner_module._parts_say_working
    assert f((parts["running"],), window, now)
    assert f((parts["pending"],), window, now)
    assert f((parts["recent_edit"],), window, now)
    assert not f((parts["done_bash"],), window, now)
    assert not f((parts["old_edit"],), window, now)
    assert not f((parts["read"],), window, now)
    assert not f((), window, now)
    assert not f(("not a dict", None, {"tool": "bash"}), window, now)
    # the window is measured, so a zero window admits no edit
    assert not f((parts["recent_edit"],), 0.0, now)
    # ... and a bash that is still running is work whatever the window is
    assert f((parts["running"],), 0.0, now)


def test_a_deadline_with_a_running_bash_is_extended_with_flat_churn(tmp_path):
    """KC-58 §2 acceptance: a deadline reached with a `bash` part running extends
    the turn with unchanged churn, and `turn_max_sec` stays the hard ceiling."""
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=1800, turn_extend_sec=600,
                      turn_max_sec=3000, idle_event_timeout_sec=0)
    run = AgentRun(agent=cfg.agents[0], workspace=ws)
    open_bash = {"type": "tool", "tool": "bash",
                 "state": {"status": "running", "input": {"command": TEST_SUITE_COMMAND}}}
    working = lambda: _runner_module._parts_say_working(   # noqa: E731
        (open_bash,), float(cfg.turn_extend_sec), time.time())

    flat = _runner_module._turn_deadline(run, cfg)
    assert flat.on_deadline(1800.0) is None, "flat churn with no work is still refused"

    clock = _runner_module._turn_deadline(run, cfg, working)
    assert clock.on_deadline(1800.0) == 600
    assert clock.on_deadline(2400.0) == 600
    assert clock.extensions == [
        {"at": 1800.0, "files": 0, "lines": 0, "granted": 600, "working": True},
        {"at": 2400.0, "files": 0, "lines": 0, "granted": 600, "working": True}]
    assert clock.granted + cfg.turn_timeout_sec == cfg.turn_max_sec
    assert clock.on_deadline(float(cfg.turn_max_sec)) is None, "the cap is the hard ceiling"


def test_round_106_churn_down_with_an_edit_is_extended(tmp_path):
    """Round 106: 10 files / 1073 lines at one deadline, 1072 at the next — the
    agent removed one line while fixing a test — with an `edit` 4 s earlier.
    Churn that goes down is work, and `unchanged for` counts from the sample
    that differed, not from the last grant.
    """
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=3600, turn_extend_sec=600,
                      turn_max_sec=14400, idle_event_timeout_sec=0)
    run = AgentRun(agent=cfg.agents[0], workspace=ws)
    _write(ws.path / "pkg" / "big.py", "x = 1\n" * 1073)
    working = lambda: _runner_module._parts_say_working(   # noqa: E731
        ({"type": "tool", "tool": "edit",
          "state": {"status": "completed", "time": {"end": time.time() - 4}}},),
        float(cfg.turn_extend_sec), time.time())
    clock = _runner_module._turn_deadline(run, cfg, working)

    assert clock.on_deadline(3600.0) == 600          # 1 file / 1073 lines: growth
    _write(ws.path / "pkg" / "big.py", "x = 1\n" * 1072)
    assert clock.on_deadline(4200.0) == 600          # 1072: churn down, edit recent
    assert [e["lines"] for e in clock.extensions] == [1073, 1072]
    # "unchanged for" counts from 4200, the sample that differed, not from 3600
    assert _runner_module._no_idle_error(cfg, 4800.0, clock) == \
        "no idle after 80m (1 files, 1072 lines, unchanged for 10m)"

    # and without the edit the same drop is refused
    flat = _runner_module._turn_deadline(run, cfg)
    assert flat.on_deadline(float(cfg.turn_max_sec)) is None


def test_unchanged_for_counts_from_the_last_sample_that_differed(tmp_path):
    """Round 106, the other half: `last_change_at` moves on a sample that differs
    in either number, granted or not. A refused sample still resets it, so a turn
    that stopped growing after ten minutes does not read as unchanged for
    twenty-five."""
    ws = _churn_ws(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=600, turn_extend_sec=600,
                      turn_max_sec=14400, idle_event_timeout_sec=0)
    clock = _runner_module._turn_deadline(AgentRun(agent=cfg.agents[0], workspace=ws), cfg)
    _write(ws.path / "pkg" / "a.py", "x\n" * 100)
    # the sample counts the diff against the base file, so 100 lines written
    # is 101 changed: 100 added and the base line deleted. The numbers here are
    # churn, not file sizes.
    assert clock.on_deadline(600.0) == 600              # granted: 1 file / 101 lines
    _write(ws.path / "pkg" / "a.py", "x\n" * 99)        # churn down to 100
    assert clock.on_deadline(1200.0) is None            # refused: no growth, no edit
    assert _runner_module._no_idle_error(cfg, 1800.0, clock) == \
        "no idle after 30m (1 files, 100 lines, unchanged for 10m)"


def test_a_fresh_holder_keeps_its_slot_until_its_pytest_comes_up(tmp_path, monkeypatch):
    """The slot is granted before the reply goes out, so the first polls run
    before Kilo has spawned the shell. They must not read "no pytest" as "done":
    the holder keeps the slot until the scan has seen its process, and only the
    process going away after that frees it."""
    clock = _FakeClock()
    monkeypatch.setattr(_runner_module, "time", clock)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    ws = _churn_ws(tmp_path)
    holding: set = set()
    monkeypatch.setattr(_runner_module, "_suite_pids",
                        lambda worktree, proc_root=None: set(holding))
    slots = _runner_module._SUITE_SLOTS
    try:
        assert slots.acquire("agent-a", ws.path) == 0.0
        thread = threading.Thread(target=slots.acquire, args=("agent-b", ws.path), daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        # a dozen polls with nothing up yet, inside the grace: still held
        assert not _wait_for(lambda: not thread.is_alive(), 0.3)
        assert slots.status("agent-a")[0] == "running"
        holding.add(4242)               # the suite comes up ...
        assert not _wait_for(lambda: not thread.is_alive(), 0.3)
        holding.clear()                 # ... and exits: now the slot moves on
        assert _wait_for(lambda: not thread.is_alive(), 30)
        assert slots.status("agent-a")[0] == "done"
    finally:
        slots.release("agent-a")
        slots.release("agent-b")


def test_a_holder_whose_pytest_never_comes_up_frees_after_the_grace(tmp_path, monkeypatch):
    """A command that never starts a pytest (it failed at once) gives the slot
    back once the start grace is over — it cannot sit on it until its ceiling."""
    clock = _FakeClock()
    monkeypatch.setattr(_runner_module, "time", clock)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    monkeypatch.setattr(_runner_module, "_suite_pids", lambda worktree, proc_root=None: set())
    ws = _churn_ws(tmp_path)
    slots = _runner_module._SUITE_SLOTS
    try:
        assert slots.acquire("agent-a", ws.path, ceiling=900.0) == 0.0
        got = {}
        thread = threading.Thread(
            target=lambda: got.update(waited=slots.acquire("agent-b", ws.path)), daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        clock.advance(_runner_module._SUITE_START_GRACE_SEC - 1)
        assert not _wait_for(lambda: not thread.is_alive(), 0.3)
        clock.advance(2)
        assert _wait_for(lambda: not thread.is_alive(), 30)
        assert got["waited"] == pytest.approx(_runner_module._SUITE_START_GRACE_SEC + 1, abs=1)
    finally:
        slots.release("agent-a")
        slots.release("agent-b")


def test_a_waiter_whose_agent_stops_leaves_the_queue(tmp_path, monkeypatch):
    """A waiter's `stop` (its agent stalled or the round was interrupted) takes
    it out of the queue at once: no slot, no entry, and the holder untouched."""
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    monkeypatch.setattr(_runner_module, "_suite_pids",
                        lambda worktree, proc_root=None: {4242})
    ws = _churn_ws(tmp_path)
    slots = _runner_module._SUITE_SLOTS
    stopped: list = []
    try:
        slots.acquire("agent-a", ws.path)
        got = {}
        thread = threading.Thread(target=lambda: got.update(
            waited=slots.acquire("agent-b", ws.path, stop=lambda: bool(stopped))), daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        stopped.append("stalled")
        assert _wait_for(lambda: not thread.is_alive(), 30)
        assert got["waited"] == 0.0
        assert slots.status("agent-b") is None
        assert slots.status("agent-a")[0] == "running"
    finally:
        slots.release("agent-a")


def test_the_harvest_hold_is_not_read_off_proc(tmp_path, monkeypatch):
    """The harvest's slot is scoped to its call: the judge's pytest has not
    started when the slot is granted, and a poll that sees none must not hand
    the slot on — only the harvest's own `release` does."""
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    monkeypatch.setattr(_runner_module, "_suite_pids", lambda worktree, proc_root=None: set())
    monkeypatch.setattr(_runner_module, "_SUITE_START_GRACE_SEC", 0.0)
    ws = _churn_ws(tmp_path)
    slots = _runner_module._SUITE_SLOTS
    try:
        slots.acquire("agent-a:harvest", ws.path, ceiling=900.0, scoped=True)
        thread = threading.Thread(target=slots.acquire, args=("agent-b", ws.path), daemon=True)
        thread.start()
        assert _wait_for(lambda: slots.status("agent-b") is not None)
        assert not _wait_for(lambda: not thread.is_alive(), 0.3)
        assert slots.status("agent-a:harvest")[0] == "running"
        slots.release("agent-a:harvest")
        assert _wait_for(lambda: not thread.is_alive(), 30)
    finally:
        slots.release("agent-a:harvest")
        slots.release("agent-b")


@pytest.mark.parametrize("cmd, suite", [
    ("/usr/bin/python3 -m pytest tests -n 4", True),
    ("/venv/bin/pytest tests", True),
    ("python3 -m py.test", True),
    ("python3 -c import time pytest", True),
    ("grep -rn pytest_ini tests", False),
    ("vim tests/test_pytest_ini_quiet_summary.py", False),
    ("", False),
])
def test_the_scan_reads_pytest_as_a_word(cmd, suite):
    """A cmdline is a suite when a word *is* pytest, never when one only holds it."""
    assert _runner_module._is_pytest_cmdline(cmd) is suite


def test_the_suite_wait_is_granted_back_and_the_silence_clock_runs_from_the_reply(
        tmp_path, monkeypatch):
    """A whole-root ask that queues longer than the silence window: the wait
    blocked the loop that runs both clocks, so the agent pays for neither. The
    silence clock runs from the reply, not from the ask, and the turn records
    the wait as `suite_wait_sec` — the seconds its deadline was moved by."""
    names = ("agent-a",)
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, agent_suite_slots=1, idle_event_timeout_sec=1)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    monkeypatch.setattr(_runner_module, "_suite_pids", lambda worktree, proc_root=None: set())
    slots = _runner_module._SUITE_SLOTS
    # someone else's suite holds the only slot, released by hand below
    slots.acquire("other:harvest", tmp_path, scoped=True)
    try:
        with _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, on_prompt=work_ready)) as fake:
            h = Harness(sb, fake, cfg, agent=names[0])
            result = {}
            thread = threading.Thread(target=lambda: result.update(run=h.go()), daemon=True)
            thread.start()
            assert _wait_for(lambda: (slots.status(names[0]) or ("",))[0] == "queued")
            time.sleep(2.2)             # twice the silence window, in the queue
            assert not fake.calls(method="POST", prefix="/permission/")
            slots.release("other:harvest")
            thread.join(60)
            assert not thread.is_alive()
        run = result["run"]
        assert run.state is AgentState.READY, run.last_error
        turns = [json.loads(line) for line in
                 (sb.out_dir / names[0] / "turns.jsonl").read_text().splitlines()]
        # the turn idled on its own — not a silence stall that the harvest
        # happened to rescue because the work was already committed
        assert turns[0]["idle_status"] == "idle", turns[0]
        assert turns[0]["suite_wait_sec"] >= 2, turns[0]
    finally:
        slots.release("other:harvest")


def test_grant_suite_is_kept_out_of_the_churn_room():
    """`grant_suite` moves the deadline by the wait and nothing else: the churn
    room (`granted`) and the gate's half are untouched, and junk is 0.0."""
    clock = _runner_module._TurnClock(on_deadline=None)
    assert clock.grant_suite(120.5) == 120.5
    assert clock.grant_suite("soon") == 0.0
    assert clock.grant_suite(-3) == 0.0
    assert clock.suite_waited == 120.5
    assert clock.granted == 0.0 and clock.gate_added == 0.0


def test_the_suite_queue_does_not_count_against_agent_max_sec(tmp_path, monkeypatch):
    """An agent stuck in the suite queue is not at fault: `agent_max_sec` is
    paused while its permission waits for a slot and resumed with what it had
    left. Here the queue alone outlasts the whole limit, and the agent still
    gets its reply, idles on its own and ends READY — not `time up`."""
    names = ("agent-a",)
    sb = Sandbox(tmp_path, names)
    cfg = make_config(names, agent_suite_slots=1, agent_max_sec=2, idle_event_timeout_sec=0)
    monkeypatch.setattr(_runner_module, "_SUITE_POLL_SEC", 0.02)
    monkeypatch.setattr(_runner_module, "_suite_pids", lambda worktree, proc_root=None: set())
    slots = _runner_module._SUITE_SLOTS
    slots.acquire("other:harvest", tmp_path, scoped=True)
    try:
        with _BenchFake(_suite_scenario(TEST_SUITE_COMMAND, on_prompt=work_ready)) as fake:
            h = Harness(sb, fake, cfg, agent=names[0])
            result = {}
            thread = threading.Thread(target=lambda: result.update(run=h.go()), daemon=True)
            thread.start()
            assert _wait_for(lambda: (slots.status(names[0]) or ("",))[0] == "queued")
            time.sleep(3.0)             # longer than the agent's whole limit
            assert thread.is_alive(), "the queue must not end the agent"
            slots.release("other:harvest")
            thread.join(60)
            assert not thread.is_alive()
        run = result["run"]
        assert run.state is AgentState.READY, run.last_error
        assert "time up" not in (run.last_error or "")
        turns = [json.loads(line) for line in
                 (sb.out_dir / names[0] / "turns.jsonl").read_text().splitlines()]
        assert turns[0]["idle_status"] == "idle", turns[0]
        assert turns[0]["suite_wait_sec"] >= 3, turns[0]
    finally:
        slots.release("other:harvest")
