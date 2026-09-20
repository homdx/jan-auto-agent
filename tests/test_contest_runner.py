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
from tools.contest.kilo_client import EventTap, KiloClient, KiloServer  # noqa: E402
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


def _work(directory: str, *, test: bool) -> str:
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
    _claim(directory, sha)
    return sha


def work_ready(directory, text):
    _work(directory, test=True)


def work_no_test(directory, text):
    _work(directory, test=False)


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
        return super()._run_turn(session, {k: v for k, v in turn.items() if k != "questions"}, text)

    def pulse(self, session_id: str, every: float, times: int, then_idle: bool = False) -> None:
        """Emit a ``session.status busy`` every *every* seconds, on a thread."""
        def run():
            for _ in range(times):
                time.sleep(every)
                self._emit({"type": "session.status",
                            "properties": {"sessionID": session_id, "status": "busy"}})
            if then_idle:
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
    kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=30,
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
        self.client = KiloClient(self.server, str(self.ws.path))
        self.tap = EventTap(fake.url, str(self.ws.path), str(sb.out_dir / agent / "events.jsonl")).start()
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
            return run_agent(self.run, client=self.client, tap=self.tap, policy=self.policy,
                             config=self.config, ticket_path=self.sb.ticket_path,
                             out_dir=self.sb.out_dir, on_transition=record)
        finally:
            self.tap.stop()
            self.tap.join(2)


def _run_one(tmp_path, scenario, config=None, policy=None):
    sb = Sandbox(tmp_path)
    config = config or make_config(["agent-a"])
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, config, policy=policy)
        run = h.go()
        aborted = _aborted(fake)
    return sb, fake, h, run, aborted


def _round(sb, fake, config, resume=None) -> RoundState:
    return run_round(config, ROUND, sb.ticket_path, list(sb.workspaces),
                     server=KiloServer.attach(fake.url), out_dir=sb.out_dir, resume=resume)


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
    assert turn["harvest"] == {"verdict": "READY", "reasons": []}
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
    assert run.turns[0]["harvest"] == {"verdict": "REWORK", "reasons": ["no_test_file"]}
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


def test_three_questions_in_one_turn_stall_and_abort(tmp_path):
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy"], "questions": 3, "delay": 0.5}]}
    started = time.monotonic()
    sb, fake, _h, run, aborted = _run_one(tmp_path, scenario)
    assert run.state is AgentState.STALLED and aborted
    assert run.questions == 3 and "questions" in run.last_error
    assert len(fake.calls(prefix="/question/")) >= 2
    assert time.monotonic() - started < 10
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


def test_idle_event_timeout_stalls_a_silent_session_within_3s(tmp_path):
    cfg = make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1)
    started = time.monotonic()
    _sb, _fake, _h, run, aborted = _run_one(tmp_path, {"turns": [{"events": [], "idle": False}]}, cfg)
    elapsed = time.monotonic() - started
    assert run.state is AgentState.STALLED and aborted
    assert elapsed < 3.0
    assert run.last_error == "no event for 1s"
    assert run.turns[0]["idle_status"] == "stalled"


def test_events_of_the_session_keep_a_turn_alive(tmp_path):
    """idle_event_timeout_sec=1, a status event every 0.4 s for 2.4 s, then idle:
    silence is measured from the last event, so no abort."""
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1)
    scenario = {"turns": [{"events": ["busy"], "delay": 2.6}]}
    with _BenchFake(scenario) as fake:
        scenario["turns"][0]["on_prompt"] = lambda d, t: (fake.pulse(fake.sessions()[-1].id, 0.4, 6),
                                                          work_ready(d, t))
        run = Harness(sb, fake, cfg).go()
        aborted = _aborted(fake)
    _assert_ready(run, sb.ws("agent-a"))
    assert not aborted


def test_turn_timeout_stalls_a_session_that_never_idles(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=1, idle_event_timeout_sec=60)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        scenario["turns"][0]["on_prompt"] = lambda d, t: fake.pulse(fake.sessions()[-1].id, 0.3, 20)
        started = time.monotonic()
        run = Harness(sb, fake, cfg).go()
        elapsed = time.monotonic() - started
        aborted = _aborted(fake)
    assert run.state is AgentState.STALLED and aborted and elapsed < 6
    assert run.turns[0]["idle_status"] == "timeout"
    assert run.last_error == "no idle after 1s"


def test_server_going_away_mid_turn_is_error(tmp_path):
    sb = Sandbox(tmp_path)
    cfg = make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=60)
    fake = _BenchFake({"turns": [{"events": ["busy"], "idle": False}]}).start()
    try:
        threading.Timer(0.8, fake.stop).start()
        started = time.monotonic()
        run = Harness(sb, fake, cfg).go()
        elapsed = time.monotonic() - started
    finally:
        fake.stop()
    assert run.state is AgentState.ERROR and elapsed < 10
    assert run.turns[0]["idle_status"] == "closed"


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
        started = time.monotonic()
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
        elapsed = time.monotonic() - started
        between, finished_first = _creates_and_reads(fake)
    for name in ("agent-a", "agent-b"):
        _assert_ready(_by_name(state)[name], sb.ws(name))
    assert not finished_first, between
    assert elapsed < 2.6


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
    only a's session — a is aborted within seconds, not after b is done."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    stamps: list = []

    with _BenchFake({"turns": [{"events": ["busy"], "idle": False}]}) as fake:
        orig_turn, orig_record = fake._run_turn, fake._record_request

        def run_turn(session, turn, text):
            if session.directory.endswith("agent-b"):
                work_ready(session.directory, text)
                fake.pulse(session.id, 0.3, 20, then_idle=True)
            return orig_turn(session, turn, text)

        def record(method, path, query, body):
            stamps.append((time.monotonic(), path))
            orig_record(method, path, query, body)

        fake._run_turn, fake._record_request = run_turn, record
        started = time.monotonic()
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2,
                                             turn_timeout_sec=30, idle_event_timeout_sec=1))
    runs = _by_name(state)
    assert runs["agent-a"].state is AgentState.STALLED
    _assert_ready(runs["agent-b"], sb.ws("agent-b"))
    (abort_at, _), = [s for s in stamps if s[1].endswith("/abort")]
    assert abort_at - started < 4.0


def test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes(tmp_path):
    """agent-a lands in one turn; agent-b's rework prompt is where Ctrl-C
    arrives (SIGINT to this process). Then b was aborted, state.json says
    a=READY and b mid-flight, KeyboardInterrupt propagated. Resuming from
    that state.json skips a (no new session) and restarts b in its worktree."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2, turn_timeout_sec=20)
    pid = os.getpid()

    def turn1(directory, text):
        (work_ready if _agent_of(directory) == "agent-a" else work_no_test)(directory, text)

    scenario = {"turns": [{"on_prompt": turn1, "events": ["busy", "idle"]},
                          {"on_prompt": lambda d, t: os.kill(pid, signal.SIGINT),
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
                            "server", "out_dir", "resume", "run_tests"]
    for name in ("server", "out_dir", "resume", "run_tests"):
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
                          server=KiloServer.attach(fake.url), out_dir=sb.out_dir,
                          run_tests=True)
    (run,) = state.agents
    assert run.state is AgentState.READY
    first, second = run.turns
    assert first["harvest"] == {"verdict": "REWORK", "reasons": ["tests_failed"]}
    assert second["harvest"] == {"verdict": "READY", "reasons": []}
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
                          server=KiloServer.attach(fake.url), out_dir=sb.out_dir)
    (run,) = state.agents
    _assert_ready(run, sb.ws("agent-a"))
    assert len(run.turns) == 1 and run.turns[0]["harvest"] == {"verdict": "READY", "reasons": []}


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
        barrier.wait(2)
        work_ready(directory, text)

    monkeypatch.setattr(harvest_module, "run_tests_detail", roots)
    scenario = {"turns": [{"on_prompt": on_prompt, "events": ["busy", "idle"]},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with _BenchFake(scenario) as fake:
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          server=KiloServer.attach(fake.url), out_dir=sb.out_dir,
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
                          server=KiloServer.attach(fake.url), out_dir=sb.out_dir,
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
    cfg = make_config(["agent-a"], progress_every_sec=0.2, idle_event_timeout_sec=5)
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
