#!/usr/bin/env python3
"""KC-6 black-box bench: one scenario per invocation, run with cwd=<entry worktree>.

    python3 run_one_kc6.py <scenario> <base_worktree>
    python3 run_one_kc6.py --list

Prints PASS / FAIL <reason> / ERROR <traceback tail> as the last line. Every
scenario builds its own sandbox (a git repo with one worktree per agent) in a
temp dir and its own scripted `tests/_kilo_fake.py` server, so nothing touches
the entry worktree and nothing reaches a real `kilo` or a provider.

Only the ticket's contract is used: `AgentState`, `AgentRun`, `RoundState`,
`round_prompt`, `run_agent`, `run_round`, the artifacts under `out_dir`, and
the `tests/_kilo_fake.py` request/event log. `state.json` is read back as JSON
and turned into a `RoundState` through the public constructors, because the
ticket names no loader.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENTRY = Path.cwd().resolve()
sys.path.insert(0, str(ENTRY))
BASE = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 and not sys.argv[1].startswith("--") else None

ROUND = 45
TICKET_NAME = "45-kc6-bench.md"
TICKET_BODY = """# KC-6 bench ticket — the sandbox one

**Status:** open — round 45.
**Severity:** HIGH
**File:** `pkg/thing.py`
**Symbol:** `thing`
**Round:** 45
**Also touches:** `tests/test_thing.py` (new)

body
"""
BRIDGE_OK = '''"""stub"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        out = raw.strip()
        return out[:10]
'''
NO_TEST_SENTENCE = "shipped no test file"


class Fail(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# the sandbox: one repo, one worktree per agent
# ─────────────────────────────────────────────────────────────────────────────

class Sandbox:
    def __init__(self, root: Path, agents=("agent-a",)):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        self.tasks = root / "epic-tasks"
        write(self.tasks / TICKET_NAME, TICKET_BODY)
        r = self.repo
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "bench@example.invalid")
        git(r, "config", "user.name", "bench")
        write(r / "tools" / "auto" / "collect_bridge.py", BRIDGE_OK)
        write(r / "pkg" / "__init__.py", "")
        write(r / "pkg" / "thing.py", "def thing():\n    return 1\n")
        write(r / ".gitignore", "runs/\n__pycache__/\n")
        write(r / "tests" / "__init__.py", "")
        write(r / "tests" / "test_base.py", "def test_base():\n    assert True\n")
        shutil.copytree(self.tasks, r / "epic-tasks")
        (r / "scripts").mkdir()
        shutil.copy(BASE / "scripts" / "append_task.py", r / "scripts" / "append_task.py")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "base")
        self.base_sha = git(r, "rev-parse", "HEAD")
        self.out_dir = root / "out"
        self.out_dir.mkdir()
        self.workspaces = [self.add_agent(a) for a in agents]

    def add_agent(self, agent: str):
        from tools.contest.workspace import Workspace
        branch = f"contest/{ROUND}/{agent}"
        path = self.root / "wt" / agent
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), self.base_sha)
        return Workspace(agent=agent, path=path.resolve(), branch=branch,
                         base_sha=self.base_sha, kind="worktree")

    @property
    def ticket_path(self) -> Path:
        return self.tasks / TICKET_NAME

    def ws(self, agent: str):
        return next(w for w in self.workspaces if w.agent == agent)


def agent_of(directory: str) -> str:
    return Path(directory).name


def commits_since_base(directory: str) -> int:
    base = git(directory, "merge-base", "main", "HEAD")
    return len([l for l in git(directory, "log", "--oneline", f"{base}..HEAD").splitlines() if l])


def append_task(directory: str, commit: str, outcome: str = "DONE"):
    agent = agent_of(directory)
    cmd = [sys.executable, str(Path(directory) / "scripts" / "append_task.py"),
           "--progress", str(Path(directory) / "runs" / agent / "PROGRESS.csv"),
           "--ticket", TICKET_NAME, "--outcome", outcome, "--allow-duplicate",
           "--commit", commit, "--note", "bench"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=directory)
    if r.returncode:
        raise RuntimeError(f"append_task failed: {r.stderr}")


def work(directory: str, *, test: bool):
    """The agent's work for one turn: a change (+ test), one commit, the claim.

    A second call in the same worktree amends, as the rework message asks.
    """
    d = Path(directory)
    write(d / "pkg" / "thing.py", f"def thing():\n    return 42  # {time.time()}\n")
    if test:
        write(d / "tests" / "test_thing.py",
              "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
    git(directory, "add", "-A")
    if commits_since_base(directory) >= 1:
        git(directory, "commit", "-q", "--amend", "--no-edit")
    else:
        git(directory, "commit", "-q", "-m", "KC-6 bench: thing")
    sha = git(directory, "rev-parse", "HEAD")
    append_task(directory, sha)
    return sha


def work_ready(directory, text):
    work(directory, test=True)


def work_no_test(directory, text):
    work(directory, test=False)


# ─────────────────────────────────────────────────────────────────────────────
# the fake server, with the two knobs the ticket's scenarios need
# ─────────────────────────────────────────────────────────────────────────────

def load_fake():
    """`tests/_kilo_fake.py` of the *base* tree — the bench's, not the entry's."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_bench_kilo_fake", BASE / "tests" / "_kilo_fake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_fake_mod = None


def bench_fake(scenario, **kw):
    """A FakeKiloServer subclass: `bad_models` answer 400 on POST /session,
    a turn's `"questions": N` asks N questions in a row."""
    global _fake_mod
    if _fake_mod is None:
        _fake_mod = load_fake()
    fm = _fake_mod
    from http.server import ThreadingHTTPServer

    class Handler(fm._Handler):
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

    class Fake(fm.FakeKiloServer):
        bad_models: set = set()

        def start(self):
            if self._httpd is not None:
                return self
            httpd = ThreadingHTTPServer((self.host, self._port), Handler)
            httpd._fake = self
            httpd.daemon_threads = True
            self._httpd = httpd
            self._base = f"http://{self.host}:{httpd.server_address[1]}"
            t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self._thread = t
            t.start()
            return self

        def _run_turn(self, session, turn, text):
            n = int(turn.get("questions") or 0)
            for i in range(n):
                qid, event = self._question_event(session, {"question": f"q{i}?"})
                self._emit(event)
                box = self._pending.get(qid)
                if box is None or not box["event"].wait(self.reply_timeout):
                    self.unanswered.append(qid)
                    return
            return super()._run_turn(session, {k: v for k, v in turn.items() if k != "questions"}, text)

    fake = Fake(scenario, **kw)
    fake.bad_models = set(scenario.get("bad_models") or ())
    return fake


def wait_subscribers(fake, n, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fake.subscribers >= n:
            return True
        time.sleep(0.01)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# contract helpers
# ─────────────────────────────────────────────────────────────────────────────

def runner():
    import tools.contest.runner as r
    return r


def make_config(agents, **over):
    from tools.contest.roster import AgentSpec, ContestConfig
    from tools.auto.llm_profile import LlmSettings
    specs = tuple(AgentSpec(name=a, provider_id="kenary", model_id=f"{a}:free") for a in agents)
    settings = LlmSettings(base_url="https://gate-bench/v1", api_key="k", model="bench/gate",
                           api_format="openai", response_format=True, temperature=0.0, max_tokens=256)
    kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=30,
              idle_event_timeout_sec=60, max_questions_per_turn=3, tmp_roots=("/tmp/*",),
              gate_max_calls_per_session=20, gate_settings=settings, out_dir="contest-out")
    kw.update(over)
    return ContestConfig(**kw)


class StubGate:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def __call__(self, url, headers, payload, timeout, **kw):
        self.calls += 1
        return json.dumps({"verdict": self.verdict, "reason": "bench gate"})


def make_policy(config, verdict="reject"):
    from tools.contest.policy import Policy
    return Policy(config, completion_fn=StubGate(verdict))


def state_name(state) -> str:
    v = getattr(state, "value", state)
    v = getattr(v, "name", v)
    return str(v).split(".")[-1].upper()


def new_run(spec, ws):
    """`AgentRun(agent=, workspace=)` — the ticket lists defaults for nothing else,
    so accept an entry that wants every field spelled out."""
    R = runner()
    try:
        return R.AgentRun(agent=spec, workspace=ws)
    except TypeError:
        return R.AgentRun(agent=spec, workspace=ws, session_id=None, state=R.AgentState.CREATED,
                          attempt=0, turns=[], permissions={}, questions=0, last_error=None,
                          commit=None, cost=None, tokens=None)


class AgentHarness:
    """Everything `run_agent` needs for one agent against one fake, black-box."""

    def __init__(self, sb: Sandbox, fake, config, agent="agent-a", policy=None):
        from tools.contest.kilo_client import EventTap, KiloClient, KiloServer
        self.sb, self.fake, self.config = sb, fake, config
        self.ws = sb.ws(agent)
        self.spec = next(s for s in config.agents if s.name == agent)
        self.server = KiloServer.attach(fake.url)
        self.client = KiloClient(self.server, str(self.ws.path))
        self.tap = EventTap(fake.url, str(self.ws.path), str(sb.out_dir / agent / "events.jsonl")).start()
        before = fake.subscribers
        wait_subscribers(fake, before + 1)
        self.policy = policy or make_policy(config)
        self.transitions = []
        self.run = new_run(self.spec, self.ws)

    def go(self, on_transition=None):
        R = runner()
        def record(run):
            self.transitions.append(state_name(run.state))
            if on_transition:
                on_transition(run)
        return R.run_agent(self.run, client=self.client, tap=self.tap, policy=self.policy,
                           config=self.config, ticket_path=self.sb.ticket_path,
                           out_dir=self.sb.out_dir, on_transition=record)

    def close(self):
        try:
            self.tap.stop()
            self.tap.join(2)
        except Exception:
            pass


def turns_jsonl(sb, agent):
    p = sb.out_dir / agent / "turns.jsonl"
    if not p.is_file():
        return None
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def decisions_jsonl(sb, agent):
    p = sb.out_dir / agent / "decisions.jsonl"
    if not p.is_file():
        return None
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def prompts(fake):
    """(session id, text) of every prompt_async, in order."""
    out = []
    for r in fake.calls("POST"):
        if r["path"].endswith("/prompt_async"):
            sid = r["path"].split("/")[2]
            text = "".join(p.get("text", "") for p in (r["body"] or {}).get("parts", []))
            out.append((sid, text))
    return out


def session_posts(fake):
    return [r for r in fake.calls("POST", "/session") if r["path"] == "/session"]


def head_sha(ws):
    return git(ws.path, "rev-parse", "HEAD")


def assert_ready(run, ws, where=""):
    check(state_name(run.state) == "READY", f"{where}state {state_name(run.state)}, want READY (last_error={run.last_error!r})")
    sha = head_sha(ws)
    check(run.commit and sha.startswith(str(run.commit)) or str(run.commit).startswith(sha[:7]),
          f"{where}commit {run.commit!r} != worktree HEAD {sha[:12]}")


# ─────────────────────────────────────────────────────────────────────────────
# scenarios
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def s01_symbols_and_states(tmp):
    R = runner()
    for name in ("AgentRun", "AgentState", "run_agent", "run_round", "round_prompt", "RoundState"):
        check(hasattr(R, name), f"missing symbol {name}")
    S = R.AgentState
    want = ["CREATED", "PROMPTED", "WAITING", "HARVESTING", "REWORK", "READY", "GAVE_UP", "STALLED", "ERROR"]
    have = [m.name for m in S]
    check(have == want, f"AgentState members {have}")
    check(all(state_name(S[n]) == n for n in want), "AgentState values are not the names")
    check(isinstance(S.READY, str) or getattr(S.READY, "value", None) == "READY", "AgentState is not a string enum")
    fields = {f.name for f in dataclasses.fields(R.AgentRun)}
    want_f = {"agent", "workspace", "session_id", "state", "attempt", "turns", "permissions",
              "questions", "last_error", "commit", "cost", "tokens"}
    check(want_f <= fields, f"AgentRun lacks fields {sorted(want_f - fields)}")
    check(not R.AgentRun.__dataclass_params__.frozen, "AgentRun is frozen")
    rf = {f.name for f in dataclasses.fields(R.RoundState)} if dataclasses.is_dataclass(R.RoundState) else set()
    if rf:
        check({"round_no", "ticket", "base_sha", "started_at", "agents"} <= rf, f"RoundState fields {sorted(rf)}")
    check(callable(getattr(R.RoundState, "table_rows", None)), "RoundState.table_rows missing")


@scenario
def s02_round_prompt(tmp):
    R = runner()
    sb = Sandbox(tmp)
    text = R.round_prompt("zeta-9", sb.ticket_path, "abc1234def")
    check("python3 scripts/next_task.py --tasks epic-tasks/ --progress runs/zeta-9/PROGRESS.csv" in text,
          "next_task.py line with the agent name missing")
    check("python3 scripts/append_task.py --progress runs/zeta-9/PROGRESS.csv" in text,
          "append_task.py line with the agent name missing")
    check("<YOUR NAME>" not in text, "<YOUR NAME> left in the prompt")
    check("abc1234def" in text, "base sha missing")
    low = text.lower()
    check("reviewer" in low and "final" in low, "permission/reviewer sentence missing")
    check("CollectBridge._shrink" in text and "One local commit" in text, "runbook body missing")
    check("> " not in text.split("\n")[0], "blockquote markers kept")
    # a second agent gets its own name, nothing else changes
    t2 = R.round_prompt("eta-2", sb.ticket_path, "abc1234def")
    check(t2.replace("eta-2", "zeta-9") == text, "prompt differs beyond the name")


@scenario
def s03_happy_path(tmp):
    sb = Sandbox(tmp)
    scen = {"session": {"cost": 0.42, "tokens": {"input": 10, "output": 5, "total": 15}},
            "turns": [{"on_prompt": work_ready, "events": ["busy", "file.edited", "idle"],
                       "assistant": "done"}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
    ws = sb.ws("agent-a")
    assert_ready(run, ws)
    check(run.attempt == 0, f"attempt {run.attempt}")
    check(len(run.turns) == 1, f"turns {len(run.turns)}")
    t = run.turns[0]
    check(t.get("kind") == "initial", f"turn kind {t.get('kind')!r}")
    check(t.get("idle_status") == "idle", f"idle_status {t.get('idle_status')!r}")
    check("sent_at" in t and "idle_at" in t, "turn lacks sent_at/idle_at")
    check(any(str(k).startswith("harvest") for k in t) and "READY" in json.dumps(t), f"turn lacks the harvest verdict: {t!r}")
    check(run.session_id and fake.sessions() and run.session_id == fake.sessions()[0].id, "session_id not the fake's")
    check(run.cost == 0.42 and (run.tokens or {}).get("total") == 15, f"cost/tokens {run.cost} {run.tokens}")
    lines = turns_jsonl(sb, "agent-a")
    check(lines is not None and len(lines) == 1, f"turns.jsonl lines {lines and len(lines)}")
    check((sb.out_dir / "agent-a.session.json").is_file(), "agent-a.session.json missing")
    msgs = json.loads((sb.out_dir / "agent-a.session.json").read_text())
    check(isinstance(msgs, list) and len(msgs) == 2, f"session.json has {msgs!r:.80}")
    check(h.transitions[-1] == "READY" and "HARVESTING" in h.transitions and "WAITING" in h.transitions,
          f"transitions {h.transitions}")
    # the one session was created with the contest title and the rules
    post = session_posts(fake)[0]
    check(post["body"]["title"] == f"contest/{ROUND}/agent-a", f"title {post['body']['title']!r}")
    check(post["body"]["model"] == {"providerID": "kenary", "id": "agent-a:free"}, f"model {post['body']['model']}")
    check(len(post["body"]["permission"]) >= 3, "session rules not sent")
    check(post["query"].get("directory") == str(ws.path), f"session directory {post['query']}")
    sid, text = prompts(fake)[0]
    check("runs/agent-a/PROGRESS.csv" in text and sb.base_sha in text, "initial prompt is not round_prompt")


@scenario
def s04_rework_then_ready(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                      {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
    assert_ready(run, sb.ws("agent-a"))
    check(run.attempt == 1, f"attempt {run.attempt}")
    check(len(run.turns) == 2, f"turns {len(run.turns)}")
    check([t.get("kind") for t in run.turns] == ["initial", "rework"], f"kinds {[t.get('kind') for t in run.turns]}")
    check("no_test_file" in json.dumps(run.turns[0]), "turn 1 lacks the no_test_file reason code")
    ps = prompts(fake)
    check(len(ps) == 2, f"{len(ps)} prompts")
    check(ps[0][0] == ps[1][0], "rework went to a different session")
    check(len(session_posts(fake)) == 1, f"{len(session_posts(fake))} sessions created")
    check(NO_TEST_SENTENCE in ps[1][1], "rework prompt lacks the no_test_file sentence")
    check("Attempt 1 of 2" in ps[1][1], f"rework prompt attempt counter: {ps[1][1][:80]!r}")
    check("REWORK" in h.transitions and h.transitions.count("PROMPTED") == 2, f"transitions {h.transitions}")
    lines = turns_jsonl(sb, "agent-a")
    check(lines and len(lines) == 2, f"turns.jsonl lines {lines and len(lines)}")


@scenario
def s05_give_up_keeps_commit(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                      {"on_prompt": work_no_test, "events": ["busy", "idle"]},
                      {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"], max_rework=1))
        try:
            run = h.go()
        finally:
            h.close()
    check(state_name(run.state) == "GAVE_UP", f"state {state_name(run.state)}")
    check(len(prompts(fake)) == 2, f"{len(prompts(fake))} prompts, want 2")
    sha = head_sha(sb.ws("agent-a"))
    check(run.commit and sha.startswith(str(run.commit)), f"commit not kept: {run.commit!r} vs {sha[:12]}")
    check(run.attempt == 1, f"attempt {run.attempt}")
    check("Attempt 1 of 1" in prompts(fake)[1][1], "rework counter")


def _permission_turn(on_prompt, patterns):
    return {"on_prompt": on_prompt, "events": ["busy", "idle"],
            "permission": {"permission": "external_directory", "patterns": patterns,
                           "metadata": {"command": "rm -v " + patterns[0].rstrip("*") + "x",
                                        "directories": [patterns[0].rstrip("/*")]}},
            "tool_parts": [{"tool": "bash", "status": "completed",
                            "input": {"command": "ls"}, "output": "ok"}]}


@scenario
def s06_permission_mechanical_once(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [_permission_turn(work_ready, ["/tmp/*"])]}
    with bench_fake(scen) as fake:
        cfg = make_config(["agent-a"], tmp_roots=("/tmp/*",))
        h = AgentHarness(sb, fake, cfg, policy=make_policy(cfg, "reject"))
        try:
            run = h.go()
        finally:
            h.close()
    assert_ready(run, sb.ws("agent-a"))
    replied = fake.events_of("permission.replied")
    check(len(replied) == 1 and replied[0]["properties"]["reply"] == "once", f"replied {replied}")
    d = decisions_jsonl(sb, "agent-a")
    check(d is not None and len(d) == 1, f"decisions.jsonl lines {d and len(d)}")
    check(d[0].get("layer") == "mechanical" and d[0].get("reply") == "once", f"decision {d[0]}")
    check(d[0].get("sessionID") == run.session_id, "decision sessionID")
    p = run.permissions or {}
    check(p.get("asked") == 1 and p.get("allowed") == 1 and not p.get("rejected"), f"counters {p}")
    check(not fake.unanswered, "a permission went unanswered")


@scenario
def s07_permission_gate_reject(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [_permission_turn(work_ready, ["/var/lib/*"])]}
    with bench_fake(scen) as fake:
        cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",))
        pol = make_policy(cfg, "reject")
        h = AgentHarness(sb, fake, cfg, policy=pol)
        try:
            run = h.go()
        finally:
            h.close()
    assert_ready(run, sb.ws("agent-a"))
    replied = fake.events_of("permission.replied")
    check(len(replied) == 1 and replied[0]["properties"]["reply"] == "reject", f"replied {replied}")
    d = decisions_jsonl(sb, "agent-a")
    check(d and len(d) == 1 and d[0].get("layer") == "gate" and d[0].get("reply") == "reject", f"decision {d}")
    check(pol._completion_fn.calls == 1, f"gate called {pol._completion_fn.calls} times")
    p = run.permissions or {}
    check(p.get("asked") == 1 and p.get("rejected") == 1 and p.get("gated") == 1, f"counters {p}")


@scenario
def s08_gate_budget_from_counters(tmp):
    """gate_max_calls_per_session=1: the second outside-worktree permission is
    a budget reject, the counters say gate_failed/rejected, no crash."""
    sb = Sandbox(tmp)
    t = _permission_turn(work_no_test, ["/var/lib/*"])
    t2 = _permission_turn(work_ready, ["/var/lib/*"])
    scen = {"turns": [t, t2]}
    with bench_fake(scen) as fake:
        cfg = make_config(["agent-a"], tmp_roots=("/nowhere/*",), gate_max_calls_per_session=1)
        pol = make_policy(cfg, "allow")
        h = AgentHarness(sb, fake, cfg, policy=pol)
        try:
            run = h.go()
        finally:
            h.close()
    assert_ready(run, sb.ws("agent-a"))
    d = decisions_jsonl(sb, "agent-a") or []
    check(len(d) == 2, f"decisions {len(d)}")
    check(d[0]["layer"] == "gate" and d[0]["reply"] == "once", f"first {d[0]}")
    check(d[1]["layer"] == "budget" and d[1]["reply"] == "reject", f"second {d[1]} — gate_budget_left not wired to the counters")
    check(pol._completion_fn.calls == 1, f"gate called {pol._completion_fn.calls} times, budget was 1")
    p = run.permissions or {}
    check(p.get("asked") == 2 and p.get("allowed") == 1 and p.get("rejected") == 1, f"counters {p}")


@scenario
def s09_three_questions_stall(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy"], "questions": 3, "delay": 0.5}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            t0 = time.monotonic()
            run = h.go()
            el = time.monotonic() - t0
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
        rejected = len(fake.calls(prefix="/question/"))
    check(state_name(run.state) == "STALLED", f"state {state_name(run.state)} after {el:.1f}s")
    check(aborted, "abort was not requested")
    check(run.questions == 3, f"questions counted {run.questions}")
    check(rejected >= 2, f"{rejected} questions rejected")
    check(el < 10, f"took {el:.1f}s")


@scenario
def s10_two_questions_is_not_a_stall(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy"], "questions": 2}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
    assert_ready(run, sb.ws("agent-a"))
    check(not aborted, "abort sent for two questions")
    check(run.questions == 2, f"questions {run.questions}")


@scenario
def s11_questions_reset_per_turn(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"on_prompt": work_no_test, "events": ["busy"], "questions": 2},
                      {"on_prompt": work_ready, "events": ["busy"], "questions": 2}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
    assert_ready(run, sb.ws("agent-a"))
    check(not aborted, "abort sent — the question counter did not reset between turns")
    check(run.questions == 4, f"questions total {run.questions}")


@scenario
def s12_session_error(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"events": ["busy"], "error": {"name": "ProviderError", "message": "boom-42"}}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
    check(state_name(run.state) == "ERROR", f"state {state_name(run.state)}")
    check(run.last_error and "boom-42" in str(run.last_error), f"last_error {run.last_error!r}")
    check(len(run.turns) == 1 and run.turns[0].get("idle_status") == "error", f"turns {run.turns}")
    check((sb.out_dir / "agent-a.session.json").is_file(), "session.json not written on ERROR")


@scenario
def s13_unknown_model_error_with_body(tmp):
    sb = Sandbox(tmp)
    scen = {"bad_models": {"agent-a:free"}, "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"]))
        try:
            run = h.go()
        finally:
            h.close()
    check(state_name(run.state) == "ERROR", f"state {state_name(run.state)}")
    check(run.last_error and "unknown model" in str(run.last_error), f"last_error {run.last_error!r}")
    check(not prompts(fake), "prompted after a failed create")
    check("ERROR" in h.transitions, f"transitions {h.transitions}")


@scenario
def s14_silence_stalls_within_3s(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"events": [], "idle": False}]}
    with bench_fake(scen) as fake:
        h = AgentHarness(sb, fake, make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1))
        try:
            t0 = time.monotonic()
            run = h.go()
            el = time.monotonic() - t0
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
    check(state_name(run.state) == "STALLED", f"state {state_name(run.state)} after {el:.1f}s")
    check(el < 3.0, f"STALLED after {el:.1f}s, want < 3s (turn_timeout was 30)")
    check(aborted, "abort was not requested")
    check(len(run.turns) == 1, f"turns {len(run.turns)}")


@scenario
def s15_busy_events_keep_a_turn_alive(tmp):
    """idle_event_timeout_sec=1 but a status event every 0.4s for 2.5s, then
    idle → READY, no abort: silence is measured from the *last* event."""
    sb = Sandbox(tmp)

    def slow_but_alive(fake):
        def hook(directory, text):
            def pulse():
                sid = fake.sessions()[-1].id
                for _ in range(6):
                    time.sleep(0.4)
                    fake._emit({"type": "session.status", "properties": {"sessionID": sid, "status": "busy"}})
            threading.Thread(target=pulse, daemon=True).start()
            work_ready(directory, text)
        return hook

    scen = {"turns": [{"events": ["busy"], "delay": 2.6}]}
    with bench_fake(scen) as fake:
        scen["turns"][0]["on_prompt"] = slow_but_alive(fake)
        h = AgentHarness(sb, fake, make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1))
        try:
            run = h.go()
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
    assert_ready(run, sb.ws("agent-a"))
    check(not aborted, "aborted a turn that kept sending events")


@scenario
def s16_turn_timeout_stalls(tmp):
    sb = Sandbox(tmp)

    def chatty(fake):
        def hook(directory, text):
            def pulse():
                sid = fake.sessions()[-1].id
                for _ in range(20):
                    time.sleep(0.3)
                    fake._emit({"type": "session.status", "properties": {"sessionID": sid, "status": "busy"}})
            threading.Thread(target=pulse, daemon=True).start()
        return hook

    scen = {"turns": [{"events": ["busy"], "idle": False}]}
    with bench_fake(scen) as fake:
        scen["turns"][0]["on_prompt"] = chatty(fake)
        h = AgentHarness(sb, fake, make_config(["agent-a"], turn_timeout_sec=1, idle_event_timeout_sec=60))
        try:
            t0 = time.monotonic()
            run = h.go()
            el = time.monotonic() - t0
        finally:
            h.close()
        aborted = bool(fake.calls(path="/abort"))
    check(state_name(run.state) == "STALLED", f"state {state_name(run.state)} after {el:.1f}s")
    check(aborted, "abort not sent on turn timeout")
    check(el < 6, f"took {el:.1f}s")
    check(run.turns and run.turns[0].get("idle_status") == "timeout", f"turn {run.turns}")


@scenario
def s17_server_goes_away_is_error(tmp):
    sb = Sandbox(tmp)
    scen = {"turns": [{"events": ["busy"], "idle": False}]}
    fake = bench_fake(scen).start()
    h = AgentHarness(sb, fake, make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=60))
    try:
        threading.Timer(0.8, fake.stop).start()
        t0 = time.monotonic()
        run = h.go()
        el = time.monotonic() - t0
    finally:
        h.close()
        fake.stop()
    check(state_name(run.state) == "ERROR", f"state {state_name(run.state)} after {el:.1f}s (server closed)")
    check(el < 10, f"took {el:.1f}s")
    check(run.turns and run.turns[0].get("idle_status") == "closed", f"turn {run.turns}")


# ── run_round ────────────────────────────────────────────────────────────────

def _round(sb, fake, config, resume=None):
    from tools.contest.kilo_client import KiloServer
    R = runner()
    server = KiloServer.attach(fake.url)
    return R.run_round(config, ROUND, sb.ticket_path, list(sb.workspaces), server=server,
                       out_dir=sb.out_dir, resume=resume)


def by_name(state):
    return {r.agent.name: r for r in state.agents}


def read_state_json(sb):
    p = sb.out_dir / "state.json"
    check(p.is_file(), "state.json missing")
    return json.loads(p.read_text(encoding="utf-8"))


def json_agent(data, name):
    for a in data.get("agents") or []:
        blob = json.dumps(a)
        if f'"{name}"' in blob:
            return a
    return None


def json_state_of(a) -> str:
    s = a.get("state")
    return state_name(s) if isinstance(s, str) else str(s)


@scenario
def s20_round_two_agents_ready_state_json(tmp):
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
    runs = by_name(state)
    for a in ("agent-a", "agent-b"):
        assert_ready(runs[a], sb.ws(a), a + ": ")
    check(len(session_posts(fake)) == 2, f"{len(session_posts(fake))} sessions")
    dirs = sorted(p["query"].get("directory") for p in session_posts(fake))
    check(dirs == sorted(str(w.path) for w in sb.workspaces), f"session directories {dirs}")
    data = read_state_json(sb)
    check(data.get("round_no") == ROUND and data.get("base_sha") == sb.base_sha, f"state.json head {str(data)[:120]}")
    check(TICKET_NAME in json.dumps(data.get("ticket")), f"ticket {data.get('ticket')!r}")
    for a in ("agent-a", "agent-b"):
        ja = json_agent(data, a)
        check(ja is not None, f"{a} missing from state.json")
        check(json_state_of(ja) == "READY", f"{a} state.json state {ja.get('state')!r}")
        check(runs[a].commit and runs[a].commit in json.dumps(ja), f"{a} commit not in state.json")
        check((sb.out_dir / a / "turns.jsonl").is_file(), f"{a}/turns.jsonl missing")
        check((sb.out_dir / f"{a}.session.json").is_file(), f"{a}.session.json missing")
    rows = state.table_rows()
    check(len(rows) == 2, f"{len(rows)} rows")
    for row in rows:
        vals = [str(v) for v in (row.values() if isinstance(row, dict) else row)]
        check(any(v in ("agent-a", "agent-b") for v in vals), f"row lacks the name: {row}")
        check(any(v == "READY" for v in vals), f"row lacks the state: {row}")
        check(any("kenary/" in v or v.endswith(":free") for v in vals), f"row lacks the model: {row}")


def _timed_turn(delay, on_prompt=work_ready):
    return {"on_prompt": on_prompt, "events": ["busy"], "delay": delay}


@scenario
def s21_max_parallel_one_is_sequential(tmp):
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    spans = {}

    def hook(directory, text):
        spans.setdefault(agent_of(directory), []).append(time.monotonic())
        work_ready(directory, text)

    scen = {"turns": [_timed_turn(0.7, hook)]}
    with bench_fake(scen) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=1))
        reqs = list(fake.requests)
    runs = by_name(state)
    for a in ("agent-a", "agent-b"):
        assert_ready(runs[a], sb.ws(a), a + ": ")
    # the second session is created only after the first session's prompt went idle
    order = [(r["method"], r["path"]) for r in reqs]
    first_create = [i for i, (m, p) in enumerate(order) if (m, p) == ("POST", "/session")]
    check(len(first_create) == 2, f"{len(first_create)} session creates")
    sids = [r["path"].split("/")[2] for r in reqs if r["path"].endswith("/prompt_async")]
    idx_second_create = first_create[1]
    # between the two creates, the first session must have been prompted and read back (harvest done)
    between = [r["path"] for r in reqs[first_create[0]:idx_second_create]]
    check(any(p.endswith("/prompt_async") for p in between), f"no prompt before the second create: {between}")
    check(any(p.endswith("/message") for p in between),
          f"second session created before the first finished (no /message read in between): {between}")


@scenario
def s22_max_parallel_two_overlaps(tmp):
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    scen = {"turns": [_timed_turn(1.0)]}
    with bench_fake(scen) as fake:
        t0 = time.monotonic()
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
        el = time.monotonic() - t0
        reqs = list(fake.requests)
    runs = by_name(state)
    for a in ("agent-a", "agent-b"):
        assert_ready(runs[a], sb.ws(a), a + ": ")
    creates = [i for i, r in enumerate(reqs) if r["path"] == "/session" and r["method"] == "POST"]
    between = [r["path"] for r in reqs[creates[0]:creates[1]]]
    check(not any(p.endswith("/message") for p in between), f"the two agents did not overlap: {between}")
    check(el < 2.6, f"round took {el:.1f}s with two 1.0s turns in parallel")


@scenario
def s23_unknown_model_does_not_stop_the_round(tmp):
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    scen = {"bad_models": {"agent-b:free"}, "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2))
    runs = by_name(state)
    assert_ready(runs["agent-a"], sb.ws("agent-a"), "agent-a: ")
    check(state_name(runs["agent-b"].state) == "ERROR", f"agent-b {state_name(runs['agent-b'].state)}")
    check("unknown model" in str(runs["agent-b"].last_error), f"agent-b last_error {runs['agent-b'].last_error!r}")
    data = read_state_json(sb)
    check(json_state_of(json_agent(data, "agent-b")) == "ERROR", "state.json lacks agent-b ERROR")


@scenario
def s24_three_agents_rework_in_parallel_state_json_always_valid(tmp):
    """Three agents, max_parallel=3, every one reworks once; a reader thread
    parses state.json continuously — it must always be valid JSON and no agent
    may ever go backwards from READY. Per-agent artifacts must not mix."""
    agents = ["agent-a", "agent-b", "agent-c"]
    sb = Sandbox(tmp, agents)
    scen = {"turns": [_permission_turn(work_no_test, ["/tmp/*"]),
                      _permission_turn(work_ready, ["/tmp/*"])]}
    scen["turns"][0]["delay"] = 0.2
    scen["turns"][1]["delay"] = 0.2
    stop = threading.Event()
    bad = []
    seen_ready = set()
    reads = [0]

    def reader():
        p = sb.out_dir / "state.json"
        while not stop.is_set():
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    reads[0] += 1
                    for a in agents:
                        ja = json_agent(data, a)
                        if ja is None:
                            continue
                        st = json_state_of(ja)
                        if st == "READY":
                            seen_ready.add(a)
                        elif a in seen_ready:
                            bad.append(f"{a} went READY -> {st}")
                except (ValueError, OSError) as e:
                    bad.append(f"state.json unreadable: {type(e).__name__}: {e}")
            time.sleep(0.003)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        with bench_fake(scen) as fake:
            state = _round(sb, fake, make_config(agents, max_parallel=3, tmp_roots=("/tmp/*",)))
    finally:
        stop.set()
        th.join(2)
    runs = by_name(state)
    for a in agents:
        assert_ready(runs[a], sb.ws(a), a + ": ")
        check(runs[a].attempt == 1, f"{a} attempt {runs[a].attempt}")
        check(len(runs[a].turns) == 2, f"{a} turns {len(runs[a].turns)}")
        d = decisions_jsonl(sb, a) or []
        check(len(d) == 2, f"{a} decisions {len(d)}")
        check(all(x.get("sessionID") == runs[a].session_id for x in d), f"{a} decisions carry another session")
        t = turns_jsonl(sb, a) or []
        check(len(t) == 2, f"{a} turns.jsonl {len(t)}")
        p = runs[a].permissions or {}
        check(p.get("asked") == 2 and p.get("allowed") == 2, f"{a} counters {p}")
    check(reads[0] > 5, f"reader saw state.json only {reads[0]} times")
    check(not bad, f"{len(bad)} bad reads, first: {bad[:1]}")
    ps = prompts(fake)
    check(len(ps) == 6 and len({s for s, _ in ps}) == 3, f"{len(ps)} prompts over {len({s for s, _ in ps})} sessions")
    data = read_state_json(sb)
    check(all(json_state_of(json_agent(data, a)) == "READY" for a in agents), "final state.json not all READY")


@scenario
def s25_permissions_do_not_cross_agents(tmp):
    """Two agents in parallel, one under tmp_roots (once) and one outside with the
    gate saying reject: each decisions.jsonl has one line, with its own answer."""
    sb = Sandbox(tmp, ["agent-a", "agent-b"])

    def hook(directory, text):
        work_ready(directory, text)

    # the fake scripts one turn list for every session; agent-b's permission is
    # rewritten per session below
    scen = {"turns": [_permission_turn(hook, ["/tmp/*"])]}
    with bench_fake(scen) as fake:
        # agent-b's session gets the outside pattern: patch the permission per session
        orig = fake._permission_event

        def per_session(session, spec):
            if session.directory.endswith("agent-b"):
                spec = dict(spec, patterns=["/var/lib/*"],
                            metadata={"command": "rm -v /var/lib/x", "directories": ["/var/lib"]})
            return orig(session, spec)

        fake._permission_event = per_session
        cfg = make_config(["agent-a", "agent-b"], max_parallel=2, tmp_roots=("/tmp/*",))
        state = _round(sb, fake, cfg)
        replied = {e["properties"]["sessionID"]: e["properties"]["reply"] for e in fake.events_of("permission.replied")}
    runs = by_name(state)
    for a in ("agent-a", "agent-b"):
        assert_ready(runs[a], sb.ws(a), a + ": ")
    check(replied.get(runs["agent-a"].session_id) == "once", f"agent-a reply {replied}")
    check(replied.get(runs["agent-b"].session_id) == "reject", f"agent-b reply {replied}")
    da, db = decisions_jsonl(sb, "agent-a") or [], decisions_jsonl(sb, "agent-b") or []
    check(len(da) == 1 and da[0]["layer"] == "mechanical", f"agent-a decisions {da}")
    check(len(db) == 1 and db[0]["layer"] in ("gate", "gate-failed"), f"agent-b decisions {db}")
    pa, pb = runs["agent-a"].permissions, runs["agent-b"].permissions
    check(pa.get("allowed") == 1 and not pa.get("rejected"), f"agent-a counters {pa}")
    check(pb.get("rejected") == 1 and not pb.get("allowed"), f"agent-b counters {pb}")


def _rebuild_round_state(sb, data, config):
    """A RoundState from state.json through the public constructors only."""
    R = runner()
    S = R.AgentState
    agents = []
    for ws in sb.workspaces:
        ja = json_agent(data, ws.agent) or {}
        spec = next(s for s in config.agents if s.name == ws.agent)
        run = new_run(spec, ws)
        st = json_state_of(ja) if ja else "CREATED"
        run.state = S[st]
        run.session_id = ja.get("session_id")
        run.attempt = int(ja.get("attempt") or 0)
        run.turns = list(ja.get("turns") or [])
        run.commit = ja.get("commit")
        run.last_error = ja.get("last_error")
        agents.append(run)
    return R.RoundState(round_no=data.get("round_no", ROUND), ticket=data.get("ticket", TICKET_NAME),
                        base_sha=data.get("base_sha", sb.base_sha),
                        started_at=data.get("started_at", time.time()), agents=agents)


@scenario
def s26_ctrl_c_then_resume(tmp):
    """agent-a: READY in one turn. agent-b: turn 1 lacks a test; when the rework
    prompt arrives the operator hits Ctrl-C (SIGINT to this process). Then:
    abort was sent to b, state.json has a=READY, and the exception propagated.
    Resume with a RoundState built from state.json: a is skipped (no new
    session), b restarts from CREATED in the same worktree and reaches READY."""
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    pid = os.getpid()
    main_ident = threading.get_ident()

    def turn1(directory, text):
        if agent_of(directory) == "agent-a":
            work_ready(directory, text)
        else:
            work_no_test(directory, text)

    def turn2(directory, text):
        # only agent-b gets a second turn; Ctrl-C the round
        os.kill(pid, signal.SIGINT)

    scen = {"turns": [{"on_prompt": turn1, "events": ["busy", "idle"]},
                      {"on_prompt": turn2, "events": ["busy"], "idle": False}]}
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2, turn_timeout_sec=20, idle_event_timeout_sec=60)
    interrupted = False
    with bench_fake(scen) as fake:
        try:
            _round(sb, fake, cfg)
        except KeyboardInterrupt:
            interrupted = True
        aborted_sessions = {r["path"].split("/")[2] for r in fake.calls(path="/abort")}
        sessions = {s.directory: s.id for s in fake.sessions()}
    check(interrupted, "KeyboardInterrupt did not propagate out of run_round")
    data = read_state_json(sb)
    check(json_state_of(json_agent(data, "agent-a")) == "READY", f"agent-a not READY in state.json after Ctrl-C: {json_agent(data, 'agent-a')}")
    stb = json_state_of(json_agent(data, "agent-b"))
    check(stb not in ("READY", "GAVE_UP", "STALLED", "ERROR"), f"agent-b terminal ({stb}) after Ctrl-C")
    sid_b = sessions.get(str(sb.ws("agent-b").path))
    check(sid_b in aborted_sessions, f"agent-b's session {sid_b} was not aborted on Ctrl-C ({aborted_sessions})")

    resume = _rebuild_round_state(sb, data, cfg)
    scen2 = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen2) as fake2:
        state = _round(sb, fake2, cfg, resume=resume)
        creates = [p["query"].get("directory") for p in session_posts(fake2)]
    runs = by_name(state)
    assert_ready(runs["agent-a"], sb.ws("agent-a"), "resumed agent-a: ")
    assert_ready(runs["agent-b"], sb.ws("agent-b"), "resumed agent-b: ")
    check(str(sb.ws("agent-a").path) not in creates, "agent-a got a new session on resume")
    check(creates == [str(sb.ws("agent-b").path)], f"resume sessions {creates}")
    check(commits_since_base(str(sb.ws("agent-b").path)) == 1, "agent-b's worktree not amended into one commit")
    data2 = read_state_json(sb)
    check(json_state_of(json_agent(data2, "agent-b")) == "READY", "state.json after resume lacks agent-b READY")


@scenario
def s27_resume_ready_tree_needs_no_session(tmp):
    """A previous attempt left agent-b's worktree READY (commit + test + claim)
    but the state says it was mid-flight: resume harvests first and goes
    straight to READY without creating a session."""
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2)
    work_ready(str(sb.ws("agent-b").path), "")
    R = runner()
    data = {"round_no": ROUND, "ticket": TICKET_NAME, "base_sha": sb.base_sha, "started_at": time.time(),
            "agents": [{"agent": "agent-a", "state": "READY", "commit": "0000000", "attempt": 0},
                       {"agent": "agent-b", "state": "WAITING", "session_id": "ses_gone", "attempt": 0}]}
    resume = _rebuild_round_state(sb, data, cfg)
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        state = _round(sb, fake, cfg, resume=resume)
        creates = [p["query"].get("directory") for p in session_posts(fake)]
    runs = by_name(state)
    check(state_name(runs["agent-a"].state) == "READY", "agent-a not kept READY")
    assert_ready(runs["agent-b"], sb.ws("agent-b"), "agent-b: ")
    check(not creates, f"sessions created on resume: {creates}")


@scenario
def s28_resume_restarts_in_same_worktree_and_amends(tmp):
    """agent-b's worktree has one commit without a test from the killed attempt;
    resume restarts it from CREATED: a fresh session, the *initial* prompt, and
    the fake's turn amends → READY with one commit."""
    sb = Sandbox(tmp, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], max_parallel=2)
    work_no_test(str(sb.ws("agent-b").path), "")
    data = {"round_no": ROUND, "ticket": TICKET_NAME, "base_sha": sb.base_sha, "started_at": time.time(),
            "agents": [{"agent": "agent-a", "state": "READY", "commit": "0000000", "attempt": 0},
                       {"agent": "agent-b", "state": "WAITING", "session_id": "ses_gone", "attempt": 1}]}
    resume = _rebuild_round_state(sb, data, cfg)
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        state = _round(sb, fake, cfg, resume=resume)
        creates = [p["query"].get("directory") for p in session_posts(fake)]
        ps = prompts(fake)
    runs = by_name(state)
    assert_ready(runs["agent-b"], sb.ws("agent-b"), "agent-b: ")
    check(creates == [str(sb.ws("agent-b").path)], f"sessions {creates}")
    check(len(ps) == 1 and "runs/agent-b/PROGRESS.csv" in ps[0][1], "resume did not send the initial prompt")
    check(commits_since_base(str(sb.ws("agent-b").path)) == 1, "not one commit after resume")


@scenario
def s29_round_state_json_roundtrip_of_runner(tmp):
    """Whatever the entry writes to state.json, a RoundState rebuilt from the
    ticket's public constructors and handed back as `resume` is accepted."""
    sb = Sandbox(tmp, ["agent-a"])
    cfg = make_config(["agent-a"])
    scen = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    with bench_fake(scen) as fake:
        _round(sb, fake, cfg)
    data = read_state_json(sb)
    resume = _rebuild_round_state(sb, data, cfg)
    with bench_fake(scen) as fake2:
        state = _round(sb, fake2, cfg, resume=resume)
        creates = session_posts(fake2)
    check(not creates, "a READY agent was re-run on resume")
    check(state_name(by_name(state)["agent-a"].state) == "READY", "READY not kept")
    rows = state.table_rows()
    check(len(rows) == 1, f"{len(rows)} rows")


@scenario
def s30_stall_with_a_chatty_neighbour(tmp):
    """agent-a goes silent while agent-b keeps emitting on the same server:
    a's STALLED must not be masked by b's traffic. (The fake broadcasts every
    event to every tap; a silence clock that counts *any* event on the tap
    instead of this session's never fires here.)"""
    sb = Sandbox(tmp, ["agent-a", "agent-b"])

    def hook(directory, text):
        if agent_of(directory) == "agent-b":
            work_ready(directory, text)

    scen = {"turns": [{"on_prompt": hook, "events": ["busy"], "idle": False}]}
    with bench_fake(scen) as fake:
        # agent-b's session pulses every 0.3 s for 6 s then idles; agent-a is silent
        orig = fake._run_turn

        def run_turn(session, turn, text):
            if session.directory.endswith("agent-b"):
                def pulse():
                    for _ in range(20):
                        time.sleep(0.3)
                        fake._emit({"type": "session.status", "properties": {"sessionID": session.id, "status": "busy"}})
                    fake._emit({"type": "session.idle", "properties": {"sessionID": session.id}})
                threading.Thread(target=pulse, daemon=True).start()
                turn = dict(turn, events=["busy"], idle=False)
            return orig(session, turn, text)

        fake._run_turn = run_turn
        stamps = []
        orig_record = fake._record_request

        def stamped(method, path, query, body):
            stamps.append((time.monotonic(), method, path))
            orig_record(method, path, query, body)

        fake._record_request = stamped
        t0 = time.monotonic()
        state = _round(sb, fake, make_config(["agent-a", "agent-b"], max_parallel=2,
                                             turn_timeout_sec=30, idle_event_timeout_sec=1))
        el = time.monotonic() - t0
    runs = by_name(state)
    check(state_name(runs["agent-a"].state) == "STALLED", f"agent-a {state_name(runs['agent-a'].state)} after {el:.1f}s")
    assert_ready(runs["agent-b"], sb.ws("agent-b"), "agent-b: ")
    aborts = [t for t, m, p in stamps if p.endswith("/abort")]
    check(aborts, "no abort")
    check(aborts[0] - t0 < 4.0, f"agent-a aborted only after {aborts[0] - t0:.1f}s — its silence clock was reset by agent-b's events")


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if sys.argv[1] == "--list":
        print(" ".join(SCENARIOS))
        return 0
    name = sys.argv[1]
    fn = SCENARIOS[name]
    tmp = Path(tempfile.mkdtemp(prefix=f"kc6-{name}-"))
    try:
        fn(tmp)
        print("PASS")
        return 0
    except Fail as e:
        print(f"FAIL {e}")
        return 1
    except BaseException:  # noqa: BLE001
        tb = traceback.format_exc().strip().splitlines()
        print("ERROR " + " | ".join(tb[-3:])[:400])
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
