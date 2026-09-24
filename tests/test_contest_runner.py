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
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
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
from tools.contest.backend import KiloBackend  # noqa: E402
from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import AgentSpec, ContestConfig  # noqa: E402
from tools.contest.runner import (  # noqa: E402
    AgentRun,
    AgentState,
    RoundState,
    round_prompt,
    run_agent,
    run_round,
)
from tools.contest.workspace import Workspace  # noqa: E402

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
    kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=300,
              idle_event_timeout_sec=60, max_questions_per_turn=3, tmp_roots=("/tmp/*",),
              gate_max_calls_per_session=20, gate_settings=gate)
    kw.update(over)
    return ContestConfig(**kw)


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

    def __init__(self, sb: Sandbox, fake, config, agent="agent-a", policy=None):
        self.sb, self.fake, self.config = sb, fake, config
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
                             out_dir=self.sb.out_dir, on_transition=record)
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


def test_a_terminal_harvest_runs_the_roots_under_the_rounds_lock(tmp_path, monkeypatch):
    """`run_tests=True`: the stall's harvest is the round's harvest — the pytest
    roots run in that worktree, through the same `_harvest` that serialises them
    round-wide, and the stall still settles the run READY."""
    import tools.contest.harvest as harvest_module

    sb = Sandbox(tmp_path)
    seen: list = []

    def roots(cwd):
        seen.append(cwd)
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
    assert seen == [str(sb.ws("agent-a").path)]
    assert run.turns[0]["harvest"]["verdict"] == "READY"
    assert run.turns[0]["harvest"]["reasons"] == []
    assert isinstance(run.turns[0]["harvest"]["elapsed"], float)


# ─────────────────────────────────────────────────────────────────────────────
# KC-22: a turn that ends idle with uncommitted work gets a continue, not a rework
# ─────────────────────────────────────────────────────────────────────────────

def _harvest_calls(monkeypatch):
    """Monkeypatch `_harvest` with a counting wrapper; return the counter list."""
    import tools.contest.runner as runner_mod

    seen = []
    real = runner_mod._harvest

    def wrap(ws, ticket_path, run_tests):
        seen.append(1)
        return real(ws, ticket_path, run_tests)

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
    """KC-16 adds one keyword to `run_round`; nothing else about it moves."""
    import inspect

    params = inspect.signature(run_round).parameters
    assert list(params) == ["config", "round_no", "ticket_path", "workspaces",
                            "make_backend", "out_dir", "resume", "run_tests"]
    for name in ("make_backend", "out_dir", "resume", "run_tests"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    assert params["resume"].default is None
    assert params["run_tests"].default is False
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

    def roots(cwd):
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
    harvested = []

    def roots(cwd):
        harvested.append(cwd)
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
    assert harvested == [str(sb.ws("agent-b").path)]


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


def test_resume_with_max_continues_zero_sends_the_plain_prompt(tmp_path, monkeypatch):
    """0 disables the whole mechanism, today's behaviour byte for byte — a `--resume`
    into a dirty worktree included: no paragraph, and the tree is not even read."""
    tree_reads = _watch_tree_reads(monkeypatch)
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], max_continues_per_attempt=0)
    work_edit_no_commit(str(sb.ws("agent-a").path), "")
    first = _resumed_first_prompt(sb, cfg)
    assert first == round_prompt("agent-a", sb.ticket_path, sb.base_sha)
    assert tree_reads == []


def test_resume_into_a_clean_worktree_keeps_the_prompt_exactly(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    _work(str(sb.ws("agent-a").path), test=False)       # committed, no test: REWORK, tree clean
    assert _resumed_first_prompt(sb, cfg) == round_prompt("agent-a", sb.ticket_path, sb.base_sha)


def test_resume_with_a_commit_under_the_dirty_tree_keeps_the_plain_prompt(tmp_path):
    """The paragraph says nothing was committed, so it is only sent when that is true."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"])
    work_commit_and_leave_a_stray_file(str(sb.ws("agent-a").path), "")
    assert _resumed_first_prompt(sb, cfg) == round_prompt("agent-a", sb.ticket_path, sb.base_sha)


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
    sb, fake, _h, _run, _ = _run_one(tmp_path, scenario)
    (_sid, text), = _prompts(fake)
    assert text == round_prompt("agent-a", sb.ticket_path, sb.base_sha)
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
                          "permissions", "questions", "last_error", "commit", "cost", "tokens"}
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
                        lambda cwd: (ALL_ROOTS_PASS, []))

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

    state_data = _state_json(sb)
    agent = state_data["agents"][0]
    assert "elapsed" in agent["turns"][0]["harvest"]
    assert isinstance(agent["turns"][0]["harvest"]["elapsed"], float)

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
