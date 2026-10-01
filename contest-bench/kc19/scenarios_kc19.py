"""KC-19 black-box scenarios: `tools.contest.runner` of the entry under test,
driven through `run_agent` against the base's `tests/_kilo_fake.py` (no entry
touched the fake — `ingest_kc19.sh` prints its diff) and a sandbox repo in
the shape `tests/test_contest_runner.py` builds. KC19_REPO points at the
worktree; nothing from the entry's own tests is imported — the sandbox,
`_BenchFake`, `Harness` and `make_config` below are the base's, copied.

Every scenario is one scripted turn sequence and one expected outcome: the
live ECONNRESET reset followed by a good turn; the backoff; the bound; zero
retries; the non-retryable shapes that must stay ERROR; the message rule;
the base's `ProviderError boom-42`; Ctrl-C inside the backoff; the retry not
eating a rework; the roster keys.
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

REPO = Path(os.environ["KC19_REPO"]).resolve()
for _p in (str(REPO / "tests"), str(REPO)):
    sys.path.insert(0, _p)
for _name in [m for m in sys.modules if m == "tools" or m.startswith("tools.") or m == "_kilo_fake"]:
    del sys.modules[_name]

import _kilo_fake  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest import roster as R  # noqa: E402
from tools.contest import runner as RUN  # noqa: E402
from tools.contest.kilo_client import EventTap, KiloClient, KiloServer  # noqa: E402
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import CONTEST_KEYS, AgentSpec, ContestConfig, load_roster  # noqa: E402
from tools.contest.runner import AgentRun, AgentState, RoundState, run_agent, run_round  # noqa: E402

assert Path(RUN.__file__).resolve().is_relative_to(REPO)
assert Path(_kilo_fake.__file__).resolve().is_relative_to(REPO)

ROUND = 45
TICKET = "45-kc6-probe.md"
TICKET_BODY = """# KC-6 — probe

**Status:** open
**File:** `pkg/thing.py`
**Also touches:** `tests/test_thing.py` (new)

body
"""
BRIDGE = '''"""stub"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        return raw.strip()[:10]
'''

# the live payload of round 52 (07:53:37, laguna and hy3 in the same millisecond)
ECONNRESET = {"name": "APIError",
              "data": {"message": "Connection reset by server", "isRetryable": True,
                       "metadata": {"code": "ECONNRESET"}}}
MODEL_NOT_FOUND = {"name": "UnknownError", "data": {"message": "Model not found: kenary/x"}}
BAD_GATEWAY = {"name": "APIError", "data": {"message": "502 Bad Gateway"}}
# round 58, muse-spark-1-3 at t+225 s — the stream cut; the ticket's message rule does not name it
STREAM_CUT = {"name": "UnknownError",
              "data": {"message": "\"the model's provider interrupted the response stream\""}}


# ── the base's sandbox / fake / harness, copied ───────────────────────────────

def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Sandbox:
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

    def _add(self, agent: str):
        from tools.contest.workspace import Workspace
        branch = f"contest/{ROUND}/{agent}"
        path = self.root / "wt" / agent
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), self.base_sha)
        return Workspace(agent=agent, path=path.resolve(), branch=branch,
                         base_sha=self.base_sha, kind="worktree")

    def ws(self, agent: str):
        return next(w for w in self.workspaces if w.agent == agent)


def _agent_of(directory: str) -> str:
    return Path(directory).name


def _commits(directory: str) -> int:
    base = _git(directory, "merge-base", "main", "HEAD")
    return len(_git(directory, "log", "--oneline", f"{base}..HEAD").splitlines())


def _claim(directory: str, sha: str, outcome: str = "FIXED") -> None:
    agent = _agent_of(directory)
    csv_path = Path(directory) / "runs" / agent / "PROGRESS.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("ticket,finding,outcome,commit,note\n")
        fh.write(f"{TICKET},,{outcome},{sha},test\n")


def _work(directory: str, *, test: bool) -> str:
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


def _prompts(fake) -> list:
    out = []
    for r in fake.calls("POST"):
        if r["path"].endswith("/prompt_async"):
            text = "".join(p.get("text", "") for p in (r["body"] or {}).get("parts", []))
            out.append((r["path"].split("/")[2], text))
    return out


def _session_posts(fake) -> list:
    return [r for r in fake.calls("POST") if r["path"] == "/session"]


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
    def __call__(self, url, headers, payload, timeout, **kw):
        return json.dumps({"verdict": "reject", "reason": "stub"})


class Harness:
    def __init__(self, sb: Sandbox, fake, config, agent="agent-a"):
        self.sb, self.fake, self.config = sb, fake, config
        self.ws = sb.ws(agent)
        spec = next(s for s in config.agents if s.name == agent)
        self.server = KiloServer.attach(fake.url)
        self.client = KiloClient(self.server, str(self.ws.path))
        self.tap = EventTap(fake.url, str(self.ws.path), str(sb.out_dir / agent / "events.jsonl")).start()
        deadline = time.monotonic() + 5
        while fake.subscribers < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.policy = Policy(config, completion_fn=StubGate())
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


def _run_one(tmp_path, scenario, config=None):
    sb = Sandbox(tmp_path)
    config = config or make_config(["agent-a"])
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, config)
        started = time.monotonic()
        run = h.go()
        elapsed = time.monotonic() - started
        prompts = _prompts(fake)
        posts = _session_posts(fake)
        errors = fake.events_of("session.error")
    return sb, run, prompts, posts, elapsed, errors


def cfg(**over) -> ContestConfig:
    """The ticket's two keys with backoff 0 unless a scenario says otherwise."""
    kw = dict(max_error_retries=2, error_retry_backoff_sec=0)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def kinds(run) -> list:
    return [t.get("kind") for t in run.turns]


def _err_turn(payload, **extra):
    return {"events": ["busy"], "error": payload, **extra}


GOOD = {"on_prompt": work_ready, "events": ["busy", "idle"]}


# ── Acceptance ────────────────────────────────────────────────────────────────

def test_s01_live_reset_then_a_good_turn_is_ready_in_the_same_session(tmp_path):
    sb, run, prompts, posts, _, _ = _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]}, cfg())
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.attempt == 0
    assert kinds(run) == ["initial", "retry"], kinds(run)
    assert run.turns[0]["idle_status"] == "error"
    assert len(prompts) == 2 and prompts[0][0] == prompts[1][0], prompts
    assert len(posts) == 1
    assert run.commit == _git(sb.ws("agent-a").path, "rev-parse", "HEAD")


def test_s02_retry_prompt_names_the_cause_and_asks_to_go_on(tmp_path):
    _, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]}, cfg())
    text = prompts[1][1]
    assert "dropped the connection" in text, text
    assert "Connection reset by server" in text, text
    assert "do not start over" in text, text


def test_s03_backoff_one_second_is_waited_and_the_run_still_ends_fast(tmp_path):
    _, run, prompts, _, elapsed, errors = _run_one(
        tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]}, cfg(error_retry_backoff_sec=1))
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert elapsed < 5.0, elapsed
    assert run.turns[0]["idle_at"] + 1.0 <= run.turns[1]["sent_at"] + 0.05, run.turns


def test_s04_retries_exhausted_at_one_is_error_with_the_prefix(tmp_path):
    _, run, prompts, _, _, _ = _run_one(
        tmp_path, {"turns": [_err_turn(ECONNRESET), _err_turn(ECONNRESET)]}, cfg(max_error_retries=1))
    assert run.state is AgentState.ERROR, (run.state, run.last_error)
    assert run.last_error.startswith("after 1 retries: session.error:"), run.last_error
    assert kinds(run) == ["initial", "retry"], kinds(run)
    assert len(prompts) == 2


def test_s05_retries_exhausted_at_two_is_error_after_three_turns(tmp_path):
    _, run, prompts, _, _, _ = _run_one(
        tmp_path, {"turns": [_err_turn(ECONNRESET)] * 3}, cfg(max_error_retries=2))
    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("after 2 retries: session.error:"), run.last_error
    assert kinds(run) == ["initial", "retry", "retry"], kinds(run)
    assert len(prompts) == 3


def test_s06_zero_retries_is_todays_behaviour(tmp_path):
    _, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]},
                                        cfg(max_error_retries=0))
    assert run.state is AgentState.ERROR
    assert kinds(run) == ["initial"]
    assert len(prompts) == 1
    assert run.last_error.startswith("session.error:"), run.last_error
    assert "after 0" not in run.last_error


def test_s07_model_not_found_is_error_with_retries_left(tmp_path):
    _, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(MODEL_NOT_FOUND), GOOD]}, cfg())
    assert run.state is AgentState.ERROR
    assert len(prompts) == 1 and kinds(run) == ["initial"]
    assert "Model not found" in run.last_error


def test_s08_502_by_the_message_rule_is_retried(tmp_path):
    _, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(BAD_GATEWAY), GOOD]}, cfg())
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert kinds(run) == ["initial", "retry"]
    assert "502 Bad Gateway" in prompts[1][1]


def test_s09_the_bases_provider_error_boom_42_stays_error(tmp_path):
    payload = {"name": "ProviderError", "message": "boom-42"}
    sb, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(payload), GOOD]}, cfg())
    assert run.state is AgentState.ERROR
    assert "boom-42" in run.last_error
    assert run.turns[0]["idle_status"] == "error"
    assert len(prompts) == 1
    assert (sb.out_dir / "agent-a.session.json").is_file()


def test_s10_ctrl_c_during_the_backoff_ends_within_2s(tmp_path):
    """SIGINT to this process while the runner sits in a 30 s backoff."""
    sb = Sandbox(tmp_path)
    config = cfg(error_retry_backoff_sec=30, turn_timeout_sec=20)
    pid = os.getpid()
    scenario = {"turns": [_err_turn(ECONNRESET), GOOD]}
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, config)
        fired = threading.Event()

        def kick():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not fake.events_of("session.error"):
                time.sleep(0.02)
            time.sleep(0.3)
            fired.set()
            os.kill(pid, signal.SIGINT)

        threading.Thread(target=kick, daemon=True).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            h.go()
        elapsed = time.monotonic() - started
        assert fired.is_set()
        assert elapsed < 2.0 + 0.5, elapsed
        assert len(_prompts(fake)) == 1


def test_s11_a_retry_does_not_eat_a_rework(tmp_path):
    """reset → retry answers without a test → REWORK → fixed → READY; attempt is 1, not 2."""
    scenario = {"turns": [_err_turn(ECONNRESET),
                          {"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          GOOD]}
    _, run, prompts, posts, _, _ = _run_one(tmp_path, scenario, cfg())
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.attempt == 1, run.attempt
    assert kinds(run) == ["initial", "retry", "rework"], kinds(run)
    assert len(prompts) == 3 and len(posts) == 1


def test_s12_a_reset_after_a_rework_is_retried_too(tmp_path):
    scenario = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                          _err_turn(ECONNRESET),
                          GOOD]}
    _, run, prompts, _, _, _ = _run_one(tmp_path, scenario, cfg())
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert kinds(run) == ["initial", "rework", "retry"], kinds(run)
    assert run.attempt == 1


def test_s13_turns_jsonl_carries_the_retry_turn(tmp_path):
    sb, run, _, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]}, cfg())
    lines = [json.loads(l) for l in (sb.out_dir / "agent-a" / "turns.jsonl").read_text().splitlines() if l.strip()]
    assert [l.get("kind") for l in lines] == ["initial", "retry"], lines
    assert lines[0]["idle_status"] == "error"


def test_s14_retry_is_logged_at_info_with_the_count_and_the_cause(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET), GOOD]}, cfg())
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("tools.contest.runner")]
    hits = [m for m in msgs if "retry 1/2" in m and "Connection reset by server" in m]
    assert hits, msgs


def test_s15_a_string_payload_is_not_retryable(tmp_path):
    # the fake turns a str into {"name": s, "message": s}: no data, no code — the message rule alone
    _, run, prompts, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn("provider rejected the request"), GOOD]}, cfg())
    assert run.state is AgentState.ERROR
    assert len(prompts) == 1


def test_s16_retryable_and_session_json_still_written_on_final_error(tmp_path):
    sb, run, _, _, _, _ = _run_one(tmp_path, {"turns": [_err_turn(ECONNRESET)] * 3}, cfg())
    assert run.state is AgentState.ERROR
    assert (sb.out_dir / "agent-a.session.json").is_file()


# ── _retryable, the unit the ticket names ─────────────────────────────────────

@pytest.mark.parametrize("payload", [
    ECONNRESET,
    {"name": "APIError", "data": {"message": "x", "isRetryable": True}},
    {"name": "APIError", "data": {"message": "x", "metadata": {"code": "ECONNREFUSED"}}},
    {"name": "APIError", "data": {"message": "x", "metadata": {"code": "ETIMEDOUT"}}},
    {"name": "APIError", "data": {"message": "x", "metadata": {"code": "EPIPE"}}},
    {"name": "APIError", "data": {"message": "x", "metadata": {"code": "UND_ERR_SOCKET"}}},
    {"name": "APIError", "data": {"message": "x", "isRetryable": False, "metadata": {"code": "ECONNRESET"}}},
    BAD_GATEWAY,
    {"name": "APIError", "data": {"message": "503 Service Unavailable"}},
    {"name": "APIError", "data": {"message": "504 Gateway Time-out"}},
    {"name": "APIError", "data": {"message": "429 Too Many Requests"}},
    {"name": "APIError", "data": {"message": "The model is Overloaded right now"}},
    {"name": "APIError", "data": {"message": "Rate Limit exceeded"}},
    {"name": "APIError", "data": {"message": "request TIMEOUT after 60s"}},
    {"name": "APIError", "message": "502 upstream"},
])
def test_s17_retryable_shapes(payload):
    assert RUN._retryable(payload) is True, payload


@pytest.mark.parametrize("payload", [
    MODEL_NOT_FOUND,
    {"name": "UnknownError", "data": {"message": "the model's provider rejected the request"}},
    {"name": "APIError", "data": {"message": "This model's maximum context length is 128000 tokens"}},
    {"name": "ProviderError", "message": "boom-42"},
    {"name": "APIError", "data": {"message": "x", "isRetryable": False}},
    {"name": "APIError", "data": {"message": "x", "metadata": {"code": "ENOENT"}}},
    {},
    "Connection reset by server",
    None,
    42,
    ["ECONNRESET"],
])
def test_s18_not_retryable_shapes(payload):
    assert RUN._retryable(payload) is False, payload


def test_s19_retryable_returns_a_bool_not_a_match():
    assert RUN._retryable(BAD_GATEWAY) is True
    assert RUN._retryable(MODEL_NOT_FOUND) is False


# ── the names, the roster, the ini ────────────────────────────────────────────

def test_s20_retry_prompt_is_a_module_constant_with_the_sentence():
    assert isinstance(RUN.RETRY_PROMPT, str)
    assert "dropped the connection" in RUN.RETRY_PROMPT
    assert "do not start over" in RUN.RETRY_PROMPT


def test_s21_roster_keys_defaults_and_order():
    assert "max_error_retries" in CONTEST_KEYS and "error_retry_backoff_sec" in CONTEST_KEYS
    keys = list(CONTEST_KEYS)
    assert keys.index("max_questions_per_turn") < keys.index("max_error_retries") < keys.index("error_retry_backoff_sec")
    c = make_config(["a"])
    assert c.max_error_retries == 2 and c.error_retry_backoff_sec == 15
    assert set(CONTEST_KEYS) == set(ContestConfig.__dataclass_fields__) - {"agents", "gate_settings"}


def test_s22_roster_keys_parse_from_the_ini(tmp_path):
    text = """
[contest]
max_error_retries = 5
error_retry_backoff_sec = 3

[contest.agent.alpha]
model = kenary/hy3:free
"""
    p = tmp_path / "contest.ini"
    p.write_text(text, encoding="utf-8")
    c = load_roster(p)
    assert (c.max_error_retries, c.error_retry_backoff_sec) == (5, 3)


def test_s23_the_repos_contest_ini_names_both_keys():
    # not load_roster: the repo's gate profile wants ${CONTEST_GATE_API_KEY}
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(REPO / "contest.ini", encoding="utf-8")
    assert cp.getint("contest", "max_error_retries") == 2
    assert cp.getint("contest", "error_retry_backoff_sec") == 15


def test_s24_no_bare_time_sleep_in_the_runner():
    src = (REPO / "tools" / "contest" / "runner.py").read_text(encoding="utf-8")
    assert "time.sleep(" not in src


def test_s25_docstring_diagram_names_the_retry_loop():
    doc = RUN.__doc__ or ""
    assert "retry" in doc.lower()
