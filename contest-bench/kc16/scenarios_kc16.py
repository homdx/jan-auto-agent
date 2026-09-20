"""KC-16 black-box scenarios: `python3 -m tools.contest run …` as a subprocess.

KC16_REPO names the entry worktree; the command runs from a sandbox repo with a
committed `epic-tasks/`, a two-agent roster attached to `tests/_kilo_fake.py`
(imported from the base repo — no entry touches the fake), and the fake's
`on_prompt` playing the agent inside the worktree. Nothing here imports the
entry's modules: the command line, the exit code, stdout/stderr and the files
it leaves are the only interface.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

BASE_REPO = Path(os.environ.get("KC16_BASE", Path(__file__).resolve().parents[2]))
ENTRY = Path(os.environ["KC16_REPO"]).resolve()
sys.path.insert(0, str(BASE_REPO / "tests"))
from _kilo_fake import FakeKiloServer  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

T1, T2 = "01-kc16-a.md", "02-kc7-b.md"
AGENTS = {"laguna": "kenary/laguna-s-2-1:free", "mistral": "kenary/mistral-medium-3-5:free"}
BRIDGE = 'class CollectBridge:\n    def _shrink(self, raw: str) -> str:\n        return raw.strip()[:10]\n'


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ticket(idn: str, status: str) -> str:
    return (f"# {idn} — a ticket for the round\n\n**Status:** {status} — round\n\n"
            "**File:** `pkg/thing.py`\n**Also touches:** `tests/test_thing.py`\n\nbody\n")


def roster(url: str, agents=("laguna", "mistral"), *, max_parallel=1, api_key="k",
           gate_url="http://127.0.0.1:1/v1") -> str:
    text = ("[contest]\n" f"server = {url}\n" f"max_parallel = {max_parallel}\n"
            "max_rework = 2\nturn_timeout_sec = 30\nidle_event_timeout_sec = 60\n"
            "gate_llm_profile = gate\nout_dir = contest-out\nrounds_dir = rounds\n"
            f"\n[gate]\nbase_url = {gate_url}\napi_key = {api_key}\nmodel = test/gate\n"
            "response_format = true\n")
    for a in agents:
        text += f"\n[contest.agent.{a}]\nmodel = {AGENTS[a]}\n"
    return text


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        r = self.repo
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@example.invalid")
        git(r, "config", "user.name", "t")
        write(r / "tools/auto/collect_bridge.py", BRIDGE)
        write(r / "pkg/__init__.py", "")
        write(r / "pkg/thing.py", "def thing():\n    return 1\n")
        write(r / "tests/test_thing.py",
              "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 1\n")
        write(r / "epic-tasks" / T1, ticket("KC-16", "open"))
        write(r / "epic-tasks" / T2, ticket("KC-7", "open"))
        write(r / ".gitignore", "runs/\nrounds/\ncontest-out/\n__pycache__/\n.pytest_cache/\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "base")
        self.fake = None

    @property
    def base_sha(self) -> str:
        return git(self.repo, "rev-parse", "main")

    def set_status(self, name, status):
        p = self.repo / "epic-tasks" / name
        body = re.sub(r"^\*\*Status:\*\* \S+", f"**Status:** {status}", p.read_text(), flags=re.M)
        write(p, body)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"{name}: {status}")

    def ws(self, agent) -> Path:
        return self.repo / "rounds" / f"01-{agent}"

    def out(self) -> Path:
        c = self.repo / "contest-out"
        for cand in (c / "1", c / "01"):
            if cand.is_dir():
                return cand
        return c / "1"

    def start(self, scenario=None, agents=("laguna", "mistral"), **kw):
        self.fake = FakeKiloServer(scenario or {}).start()
        write(self.repo / "contest.ini", roster(self.fake.url, agents, **kw))
        return self.fake

    def stop(self):
        if self.fake is not None:
            self.fake.stop()
            self.fake = None

    def run(self, *args, env_extra=None, timeout=240):
        env = {k: v for k, v in os.environ.items() if k != "CONTEST_GATE_API_KEY"}
        env["PYTHONPATH"] = str(ENTRY)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if env_extra:
            env.update(env_extra)
        r = subprocess.run([sys.executable, "-m", "tools.contest", *args], cwd=str(self.repo),
                           capture_output=True, text=True, env=env, timeout=timeout)
        return r.returncode, r.stdout, r.stderr


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path)
    yield s
    s.stop()


# ── the fake agent ───────────────────────────────────────────────────────────

def agent_of(d) -> str:
    return git(d, "rev-parse", "--abbrev-ref", "HEAD").rsplit("/", 1)[-1]


def claim(d: Path) -> None:
    csv = d / "runs" / agent_of(d) / "PROGRESS.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    new = not csv.exists()
    with csv.open("a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("ticket,finding,outcome,commit,note\n")
        fh.write(f"{T1},,FIXED,{git(d, 'rev-parse', 'HEAD')},test\n")


def commit(d: Path) -> None:
    git(d, "add", "-A")
    base = git(d, "merge-base", "main", "HEAD")
    if git(d, "log", "--oneline", f"{base}..HEAD"):
        git(d, "commit", "-q", "--amend", "--no-edit")
    else:
        git(d, "commit", "-q", "-m", "KC-16: thing")
    claim(d)


def work_ready(directory, text):
    d = Path(directory)
    write(d / "pkg/thing.py", "def thing():\n    return 42\n")
    write(d / "tests/test_thing.py",
          "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
    commit(d)


def work_breaks_tests(directory, text):
    """The change and a test edit, but the pinned test still says 1: only the
    pytest roots can tell — `no_test_file` is satisfied, `tests_failed` is not."""
    d = Path(directory)
    write(d / "pkg/thing.py", "def thing():\n    return 42\n")
    write(d / "tests/test_thing.py",
          "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 1\n"
          "\n\ndef test_more():\n    assert True\n")
    commit(d)


def work_no_test(directory, text):
    d = Path(directory)
    write(d / "pkg/thing.py", "def thing():\n    return 42\n")
    commit(d)


def work_nothing(directory, text):
    pass


def turn(on_prompt, permission=None, delay=None):
    spec = {"on_prompt": on_prompt, "events": ["busy", "idle"]}
    if permission is not None:
        spec["permission"] = permission
    if delay is not None:
        spec["delay"] = delay
    return spec


def for_agents(**handlers):
    def on_prompt(directory, text):
        handlers[agent_of(directory)](directory, text)
    return turn(on_prompt)


def table(out: str) -> dict:
    rows = [json.loads(l) for l in out.splitlines() if l.startswith('{"name"')]
    return {r["name"]: r for r in rows}


def plan_says(out: str, *needles) -> bool:
    low = out.lower()
    return all(n.lower() in low for n in needles)


# ── s1 the command line ──────────────────────────────────────────────────────

def test_s01_help_and_no_subcommand(sb):
    code, out, err = sb.run("run", "--help")
    assert code == 0
    for flag in ("--ticket", "--roster", "--base", "--models", "--max-parallel",
                 "--no-tests", "--no-gate", "--resume", "--out"):
        assert flag in out, flag
    code, out, err = sb.run()
    assert code == 2 and "usage" in (out + err).lower()


# ── intake ───────────────────────────────────────────────────────────────────

def test_s02_open_ticket_in_the_way_is_named_and_nothing_is_built(sb):
    sb.start()
    code, out, err = sb.run("run", "--ticket", "2", "--no-gate", "--no-tests")
    text = out + err
    assert code == 1, text
    assert "open too" in text and ("(1)" in text or T1 in text), text
    assert not (sb.repo / "rounds").exists() or not any((sb.repo / "rounds").iterdir()), "worktrees built before intake passed"
    assert sb.fake.calls("POST") == []


def test_s03_ticket_that_is_queued_is_refused_and_named(sb):
    sb.set_status(T1, "queued")
    sb.start()
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    text = out + err
    assert code == 1, text
    assert T1 in text and "queued" in text, text


def test_s04_unresolvable_base_is_named(sb):
    sb.set_status(T2, "queued")
    sb.start()
    code, out, err = sb.run("run", "--ticket", "1", "--base", "no-such-ref", "--no-gate", "--no-tests")
    text = out + err
    assert code == 1, text
    assert "no-such-ref" in text, text


def test_s05_dirty_epic_tasks_is_named(sb):
    sb.set_status(T2, "queued")
    write(sb.repo / "epic-tasks" / T1, ticket("KC-16", "open") + "an uncommitted edit\n")
    sb.start()
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    text = out + err
    assert code == 1, text
    assert "epic-tasks" in text and ("uncommitted" in text or "untracked" in text or "clean" in text), text


def test_s06_every_intake_failure_is_reported_at_once(sb):
    """`--ticket 2` (01 in the way) with a bad `--base`: both lines, one run."""
    sb.start()
    code, out, err = sb.run("run", "--ticket", "2", "--base", "no-such-ref", "--no-gate", "--no-tests")
    text = out + err
    assert code == 1, text
    assert "open too" in text, text
    assert "no-such-ref" in text, "the base failure was not reported alongside the ticket failure:\n" + text


def test_s07_unreachable_server_is_an_intake_failure(sb):
    sb.set_status(T2, "queued")
    write(sb.repo / "contest.ini", roster("http://127.0.0.1:1"))
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 1, out + err
    assert not (sb.repo / "rounds").exists() or not any((sb.repo / "rounds").iterdir())


# ── the round ────────────────────────────────────────────────────────────────

def test_s08_one_ready_one_gave_up_patches_plan_table_and_exit_zero(sb):
    sb.set_status(T2, "queued")
    fake = sb.start({"turns": [for_agents(laguna=work_ready, mistral=work_no_test)] * 3})
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    text = out + err
    assert code == 0, text
    # the plan: ticket, base, agents+models, max_parallel, tests, gate, out
    assert T1 in out and sb.base_sha[:12] in out, out
    assert AGENTS["laguna"] in out and AGENTS["mistral"] in out, out
    assert plan_says(out, "parallel") and plan_says(out, "tests") and plan_says(out, "gate"), out
    assert re.search(r"tests\W+off", out, re.I) and re.search(r"gate\W+off", out, re.I), out
    rows = table(out)
    assert set(rows) == {"laguna", "mistral"}, out
    assert rows["laguna"]["state"] == "READY" and rows["laguna"]["commit"]
    assert rows["mistral"]["state"] == "GAVE_UP" and rows["mistral"]["commit"]
    o = sb.out()
    assert (o / "laguna.patch").is_file() and (o / "mistral.GAVE_UP.patch").is_file(), sorted(p.name for p in o.iterdir())
    assert not (o / "mistral.patch").exists()
    assert out.count("laguna.patch") >= 1 and out.count("mistral.GAVE_UP.patch") >= 1, "patch lines"
    # git am onto the base reproduces the tree
    fresh = sb.root / "fresh"
    git(sb.repo, "worktree", "add", "-q", str(fresh), sb.base_sha)
    git(fresh, "am", "--quiet", str(o / "laguna.patch"))
    assert git(fresh, "diff", "--stat", git(sb.ws("laguna"), "rev-parse", "HEAD")) == ""
    state = json.loads((o / "state.json").read_text())
    assert {a["agent"]["name"] for a in state["agents"]} == {"laguna", "mistral"}
    names = {p.name for p in o.iterdir()}
    assert not names & {"SUMMARY.md", "entrants.json"}, names
    assert "kilo-serve.log" not in names, "attached server, nothing to spawn"


def test_s09_both_gave_up_exits_two_and_a_commitless_agent_gets_no_patch(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [for_agents(laguna=work_no_test, mistral=work_nothing)] * 3})
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 2, out + err
    rows = table(out)
    assert {r["state"] for r in rows.values()} == {"GAVE_UP"}
    o = sb.out()
    assert (o / "laguna.GAVE_UP.patch").is_file()
    assert not list(o.glob("mistral*.patch")), "an agent without a commit wrote a patch"


def test_s10_tests_run_by_default_and_a_breaking_change_is_rework(sb):
    sb.set_status(T2, "queued")
    fake = sb.start({"turns": [turn(work_breaks_tests)] * 3}, ["laguna"])
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate")
    text = out + err
    assert code == 2, text
    assert re.search(r"tests\W+on", out, re.I), out
    rows = table(out)
    assert rows["laguna"]["state"] == "GAVE_UP" and "tests_failed" in rows["laguna"]["last_reason"], rows
    prompts = [p["body"]["parts"][0]["text"] for p in fake.calls("POST", "/prompt_async")]
    reworks = [t for t in prompts if "not accepted yet" in t]
    assert len(reworks) == 2, len(reworks)
    assert all("test_thing" in t for t in reworks), "the pytest tail is not in the rework prompt"
    turns = [json.loads(l) for l in (sb.out() / "laguna" / "turns.jsonl").read_text().splitlines() if l.strip()]
    assert "tests_failed" in turns[0]["harvest"]["reasons"]


def test_s11_no_tests_makes_the_same_change_ready(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_breaks_tests)] * 3}, ["laguna"])
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    assert table(out)["laguna"]["state"] == "READY"


def test_s12_two_harvests_never_run_pytest_at_once(sb):
    """max_parallel=2, both agents commit in the same second, the sandbox's test
    sleeps 1.2 s and logs its start/end: the two windows must not overlap."""
    sb.set_status(T2, "queued")
    log = sb.root / "pytest-windows.log"
    write(sb.repo / "tests/test_thing.py",
          "import time\nfrom pkg.thing import thing\n\n\ndef test_thing():\n"
          f"    t0 = time.time(); time.sleep(1.2)\n"
          f"    open({str(log)!r}, 'a').write(f'{{t0}} {{time.time()}}\\n')\n"
          "    assert thing() == 1\n")
    git(sb.repo, "add", "-A"); git(sb.repo, "commit", "-q", "-m", "slow test")

    def work(directory, text):
        d = Path(directory)
        write(d / "pkg/thing.py", "def thing():\n    return 1  # touched\n")
        with (d / "tests/test_thing.py").open("a") as fh:
            fh.write("\n\ndef test_touched():\n    assert True\n")
        commit(d)
    sb.start({"turns": [turn(work)]}, max_parallel=2)
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate")
    assert code == 0, out + err
    windows = sorted(tuple(map(float, l.split())) for l in log.read_text().splitlines())
    assert len(windows) == 2, windows
    (a0, a1), (b0, b1) = windows
    assert a1 <= b0 + 0.05, f"pytest ran concurrently: {windows}"


def test_s13_models_replaces_the_roster_and_max_parallel_is_printed(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_ready)]})
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests",
                            "--models", "x:free,y:free", "--max-parallel", "1")
    assert code == 0, out + err
    assert "kenary/x:free" in out and "kenary/y:free" in out, out
    assert "laguna" not in out.split("{")[0] and AGENTS["laguna"] not in out, out
    assert re.search(r"parallel\W+1\b", out, re.I), out
    assert sorted(p.name for p in sb.out().glob("*.patch")) == ["x.patch", "y.patch"]


def test_s14_resume_restarts_only_the_mid_flight_agent(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [for_agents(laguna=work_ready, mistral=work_ready)]})
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    sb.stop()
    path = sb.out() / "state.json"
    state = json.loads(path.read_text())
    mistral = next(a for a in state["agents"] if a["agent"]["name"] == "mistral")
    mistral.update(state="HARVESTING", session_id=None, commit=None, turns=[],
                   permissions={"asked": 0, "allowed": 0, "rejected": 0, "gated": 0, "gate_failed": 0})
    path.write_text(json.dumps(state))
    git(sb.ws("mistral"), "reset", "--hard", sb.base_sha)
    (sb.out() / "mistral.patch").unlink()
    fake = sb.start({"turns": [turn(work_ready)]})
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests", "--resume")
    assert code == 0, out + err
    sessions = fake.calls("POST", "/session")
    assert len(sessions) == 1 and sessions[0]["body"]["title"].endswith("/mistral"), sessions
    assert (sb.out() / "mistral.patch").is_file() and (sb.out() / "laguna.patch").is_file()


def test_s15_no_gate_is_a_gate_failed_reject_with_no_gate_call(sb):
    """The roster's gate URL is unroutable: a gate call would surface as a
    connection error in the decision's reason, not as `no gate model configured`."""
    sb.set_status(T2, "queued")
    outside = sb.root / "elsewhere"; outside.mkdir()
    perm = {"permission": "external_directory", "patterns": [str(outside) + "/*"],
            "metadata": {"command": f"cat {outside}/x", "directories": [str(outside)]}}
    sb.start({"turns": [turn(work_ready, perm)]}, ["laguna"])
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    dec = [json.loads(l) for l in (sb.out() / "laguna" / "decisions.jsonl").read_text().splitlines() if l.strip()]
    assert len(dec) == 1 and dec[0]["layer"] == "gate-failed" and dec[0]["reply"] == "reject", dec
    assert dec[0]["reason"] == "gate unavailable: no gate model configured", dec
    assert re.search(r"gate\W+off", out, re.I), out


def test_s16_out_dir_flag_is_honoured(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_ready)]}, ["laguna"])
    dest = sb.root / "elsewhere-out"
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests", "--out", str(dest))
    assert code == 0, out + err
    assert (dest / "laguna.patch").is_file() and (dest / "state.json").is_file()
    assert not (sb.repo / "contest-out").exists()
    assert str(dest) in out, "the plan's out line"


def test_s17_contest_local_ini_overlays_the_roster(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_ready)]}, ["laguna"], max_parallel=3)
    write(sb.repo / "contest.local.ini", "[contest]\nmax_parallel = 2\n")
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    assert re.search(r"parallel\W+2\b", out, re.I), out


def test_s18_the_committed_roster_runs_without_a_gate_key_in_the_environment(sb):
    """contest.ini's gate profile says `api_key = ${CONTEST_GATE_API_KEY}`; with no
    such variable and no contest.local.ini the round still runs (the ticket:
    "without a resolvable api_key the round still runs … this command adds no
    key check"). The fixture strips the variable from the subprocess."""
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_ready)]}, ["laguna"], api_key="${CONTEST_GATE_API_KEY}")
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    assert table(out)["laguna"]["state"] == "READY"


def test_s19_default_out_dir_is_under_the_repo_and_gave_up_without_commit_is_quiet(sb):
    sb.set_status(T2, "queued")
    sb.start({"turns": [turn(work_ready)]}, ["laguna"])
    code, out, err = sb.run("run", "--ticket", "1", "--no-gate", "--no-tests")
    assert code == 0, out + err
    o = sb.out()
    assert o.parent == sb.repo / "contest-out" and o.name in ("1", "01"), o
    assert (o / "laguna.patch").read_text().startswith("From ")
