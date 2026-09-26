"""tests/test_contest_cli.py — KC-16: `python3 -m tools.contest run --ticket NN`.

Every test builds a temp repo with a committed `epic-tasks/` of two open
tickets and a roster of two agents, chdirs into it (`cmd_run` takes the repo
from `cwd`), monkeypatches `KiloServer.spawn` onto the fake so no `kilo`
binary and no provider is ever touched, and calls `cli.main` for the exit
code and the printed plan. The harvest runs with `--no-tests` in all but one of
them: `run_tests` is the `tools/contest/runner.py` thread covered in
`tests/test_contest_runner.py`, and the one test that leaves the roots on runs
them in a temp worktree, not in this repo's own `tests/`.

The ticket's H1 here is `# 01 — first`, so `intake`'s "on offer ahead" line
reads `01 (1) is on offer ahead of 02 (2) — …`; a real ticket with `# KC-7 — …`
reads `KC-7 (46) is on offer ahead of …`, the same four lines either way.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest import cli  # noqa: E402
from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.kilo_client import KiloServerError  # noqa: E402
from tools.contest.roster import AgentSpec, load_roster  # noqa: E402
from tools.contest.runner import AgentRun, AgentState, RoundState  # noqa: E402
from tools.contest.workspace import Workspace, prepare_round  # noqa: E402

# every test binds an ephemeral-port HTTP server: one xdist worker for all of them
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

ROUND = 1
TICKET_01 = "01-first.md"
TICKET_02 = "02-second.md"
OUT = Path("contest-out") / f"{ROUND:02d}"
PLANNED = ("ticket", "base", "agents", "parallel", "workers", "tmpdir", "tests", "gate", "out")

#: KC-65: the env keys the round's server always gets besides
#: `KILO_CONFIG_CONTENT` — the workers' file, the plugin that reads it, the
#: plugin's one PYTHONPATH entry and xdist's own fallback for when it cannot load.
KC65_ENV_KEYS = frozenset({"CONTEST_PYTEST_WORKERS_FILE", "PYTEST_PLUGINS",
                           "PYTHONPATH", "PYTEST_XDIST_AUTO_NUM_WORKERS"})

BRIDGE = '''"""stub for the round's ground rule"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        return raw.strip()[:10]
'''

TICKET = """# {num} — {title}

**Status:** {status} — round {num} of the KC-16 CLI test.
**Severity:** MEDIUM
**File:** `pkg/thing.py`
**Symbol:** `thing`
**Round:** {num}
**Size:** S
**Also touches:** `tests/test_thing.py` (new)

body
"""

THING_CHANGED = "def thing():\n    return 42\n"
TEST = "def test_thing():\n    assert 1 + 1 == 2\n"


# ─────────────────────────────────────────────────────────────────────────────
# the sandbox
# ─────────────────────────────────────────────────────────────────────────────

def _git(cwd, *args) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ticket(num: str, title: str, status: str = "open") -> str:
    return TICKET.format(num=num, title=title, status=status)


class Sandbox:
    """A repo with a base commit, a committed `epic-tasks/` of two open tickets
    and a roster of two agents, whose worktrees go outside it."""

    def __init__(self, tmp_path, agents=("agent-a", "agent-b"), tickets=None):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.rounds = tmp_path / "rounds"
        self.kilo = tmp_path / "kilo"

        self.repo.mkdir()
        self.rounds.mkdir()
        self.kilo.write_text("", encoding="utf-8")
        _write(self.repo / "tools" / "auto" / "collect_bridge.py", BRIDGE)
        _write(self.repo / "pkg" / "__init__.py", "")
        _write(self.repo / "pkg" / "thing.py", "def thing():\n    return 1\n")
        _write(self.repo / "tests" / "test_base.py", "def test_base():\n    assert True\n")
        _write(self.repo / ".gitignore", "contest-out/\nruns/\n__pycache__/")
        for name, body in tickets or ((TICKET_01, _ticket("01", "first")),
                                      (TICKET_02, _ticket("02", "second"))):
            _write(self.repo / "epic-tasks" / name, body)
        _write(self.repo / "contest.ini", self._roster(agents))


        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "cli@example.invalid")
        _git(self.repo, "config", "user.name", "cli")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")

    def _roster(self, agents: tuple) -> str:
        blocks = "".join(
            f"\n[contest.agent.{agent}]\nmodel = kenary/{agent}:free\n" for agent in agents
        )
        return f"""[contest]
kilo_bin = {self.kilo}
server = spawn
max_parallel = 2
max_rework = 1
turn_timeout_sec = 60
# KC-55: 900, not 30 — the gate's worst case is gate_deadline_sec + GATE_TIMEOUT
# = 660 s, and a 30 s silence clock refused every gate-on round at intake. The
# stall test below rewrites it to 5 for itself; nothing else here reads it.
idle_event_timeout_sec = 900
max_questions_per_turn = 3
tmp_roots = /nowhere/*
gate_llm_profile = contest_gate_llm
gate_max_calls_per_session = 20
out_dir = contest-out
rounds_dir = {self.rounds}

[contest_gate_llm]
base_url = http://127.0.0.1:1/v1
api_key = ${{CONTEST_GATE_API_KEY}}
model = test/gate
api_format = openai
response_format = true
{blocks}"""

    def config(self, **overrides):
        """This sandbox's roster, with the overrides applied on top of it."""
        config = load_roster(self.repo / "contest.ini")
        for key, value in overrides.items():
            config = replace(config, **{key: value})
        return config

    def commit_ticket(self, ticket: str, body: str) -> None:
        """A change to `epic-tasks/`, committed — the intake check wants it clean."""
        _write(self.repo / "epic-tasks" / ticket, body)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", f"epic-tasks: {ticket}")

    def out(self) -> Path:
        return self.repo / OUT


# ─────────────────────────────────────────────────────────────────────────────
# the agents' turn
# ─────────────────────────────────────────────────────────────────────────────

def _commits(directory: str) -> int:
    base = _git(directory, "merge-base", "main", "HEAD")
    return len(_git(directory, "log", "--oneline", f"{base}..HEAD").splitlines())


def _commit(directory: str) -> str:
    """`add -A`, one commit — amended, since a rework keeps the branch at one."""
    _git(directory, "add", "-A")
    if _commits(directory) >= 1:
        _git(directory, "commit", "-q", "--amend", "--no-edit")
    else:
        _git(directory, "commit", "-q", "-m", "KC-16: thing")
    return _git(directory, "rev-parse", "HEAD")


def _claim(directory: str, sha: str) -> None:
    """The row `append_task.py` writes for the round's ticket."""
    agent = Path(directory).name.split("-", 1)[1]
    csv_path = Path(directory) / "runs" / agent / "PROGRESS.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        if new:
            handle.write("ticket,finding,outcome,commit,note\n")
        handle.write(f"{TICKET_01},,FIXED,{sha},test\n")


def work_ready(directory, text):
    """A change, a test, one commit, the claim: READY for the harvest."""
    _write(Path(directory) / "pkg" / "thing.py", THING_CHANGED)
    _write(Path(directory) / "tests" / "test_thing.py", TEST)
    _claim(directory, _commit(directory))


def work_no_test(directory, text):
    """The same, without the test file: REWORK, then GAVE_UP at max_rework=1."""
    _write(Path(directory) / "pkg" / "thing.py", THING_CHANGED)
    _claim(directory, _commit(directory))


def _permission_outside(pattern: str) -> dict:
    root = pattern.rstrip("/*")
    return {"permission": "external_directory", "patterns": [pattern],
            "metadata": {"command": f"rm -v {root}/x", "directories": [root]}}


def _one_ready_turn(directory, text):
    """agent-a ships a test and is READY; agent-b does not, and gives up."""
    if Path(directory).name.endswith("agent-a"):
        work_ready(directory, text)
    else:
        work_no_test(directory, text)


SCENARIO_ONE_READY = {"turns": [{"on_prompt": _one_ready_turn, "events": ["busy", "idle"]},
                                {"on_prompt": work_no_test, "events": ["busy", "idle"]}]}
SCENARIO_NO_TEST = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"]},
                              {"on_prompt": work_no_test, "events": ["busy", "idle"]}]}


# ─────────────────────────────────────────────────────────────────────────────
# fixtures and read-outs
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def gate_key(monkeypatch):
    """The roster reads the gate key from the environment, as `contest.ini` does.
    These tests are not about that credential, so it is always resolvable here."""
    monkeypatch.setenv("CONTEST_GATE_API_KEY", "unset-for-the-test")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """The temp repo, as `cmd_run`'s repo: the cwd is the checkout itself."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    return sb


@pytest.fixture
def spawn_holder(monkeypatch):
    """`KiloServer.spawn` → attach to the fake: no binary, no provider."""
    holder: list = []

    def spawn_server(binary, *, log_path, **kwargs):
        return KiloServer.attach(holder[-1].url)

    monkeypatch.setattr(cli.KiloServer, "spawn", staticmethod(spawn_server))
    return holder


def run_fake(sb, scenario, argv, holder):
    """`cli.main(["run", *argv])` against a fresh fake replaying *scenario*."""
    with FakeKiloServer(scenario) as fake:
        holder.append(fake)
        code = cli.main(["run", *argv])
    return code, fake


def _sessions(fake) -> list:
    return [record for record in fake.calls("POST") if record["path"] == "/session"]


def _prompts(fake) -> list:
    out = []
    for record in fake.calls("POST"):
        if record["path"].endswith("/prompt_async"):
            text = "".join(part.get("text", "") for part in (record["body"] or {}).get("parts", []))
            out.append((record["path"].split("/")[2], text))
    return out


def _jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _table(stdout: str) -> list:
    return [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]


def _plan(stdout: str) -> dict:
    """The printed plan: `{fact: value}` for the seven facts."""
    out = {}
    for line in stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in PLANNED:
            out[key] = value.strip()
    return out


def _patches(stdout: str) -> list:
    return [line[len("patch: "):] for line in stdout.splitlines() if line.startswith("patch: ")]


# ─────────────────────────────────────────────────────────────────────────────
# intake
# ─────────────────────────────────────────────────────────────────────────────

def test_intake_rejects_a_ticket_that_is_not_the_lowest_open_one(sandbox, capsys):
    """`--ticket 2` with `01` still on offer: four lines, the two ways out,
    nothing created."""
    code = cli.main(["run", "--ticket", "2", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == 1
    assert len(lines) == 4
    assert lines[0] == ("intake: 01 (1) is on offer ahead of 02 (2) — the session prompt names no "
                        "ticket; scripts/next_task.py would hand the sessions 01 (1)")
    assert lines[1] == "intake:   planned for the sessions: epic-tasks/01-first.md " \
                       "(**Status:** open)"
    assert "--ticket 1 --no-tests --no-gate" in lines[2]
    assert lines[3].startswith("intake:   or park it, then re-run:  sed -i")
    assert lines[3].rstrip().endswith("epic-tasks/01-first.md")
    assert not (sandbox.rounds / f"{ROUND:02d}-agent-a").exists()
    assert not sandbox.out().exists()


def test_intake_passes_once_the_way_is_set_to_queued(sandbox):
    """`02` set to `queued` in a second commit: `intake` returns the `Intake`."""
    sandbox.commit_ticket(TICKET_02, _ticket("02", "second", status="queued"))
    result = cli.intake(sandbox.repo, sandbox.repo / "epic-tasks", ROUND, "HEAD", sandbox.config())
    assert result is not None
    assert result.ticket_path.name == TICKET_01
    assert result.title == "01 — first"
    assert result.base_sha == _git(sandbox.repo, "rev-parse", "HEAD")
    assert result.out_dir == sandbox.out()


def test_intake_rejects_a_ticket_whose_status_is_queued(sandbox, capsys):
    sandbox.commit_ticket(TICKET_01, _ticket("01", "first", status="queued"))
    code = cli.main(["run", "--ticket", "1", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "01-first.md is not open (**Status:** queued)" in captured.err


def test_intake_rejects_a_ticket_number_with_no_file(sandbox, capsys):
    code = cli.main(["run", "--ticket", "9", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no ticket numbered 9 in" in captured.err


def test_intake_names_an_unresolvable_base(sandbox, capsys):
    code = cli.main(["run", "--ticket", "1", "--base", "no-such-ref", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no-such-ref" in captured.err and "does not resolve" in captured.err


def test_intake_names_a_dirty_epic_tasks(sandbox, capsys):
    _write(sandbox.repo / "epic-tasks" / "03-uncommitted.md", _ticket("03", "dirty"))
    code = cli.main(["run", "--ticket", "1", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "epic-tasks/ has uncommitted or untracked changes" in captured.err


def test_intake_reports_every_failure_at_once(sandbox, capsys):
    """Two problems, two lines: intake runs all its checks before it stops."""
    _write(sandbox.repo / "epic-tasks" / "03-uncommitted.md", _ticket("03", "dirty"))
    code = cli.main(["run", "--ticket", "2", "--base", "no-such-ref", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "01 (1) is on offer ahead of 02 (2)" in captured.err
    assert "does not resolve" in captured.err


def test_intake_names_a_server_that_does_not_answer(sandbox, capsys):
    """`server = http://…` is answered, not trusted: the health poll decides."""
    result = cli.intake(sandbox.repo, sandbox.repo / "epic-tasks", ROUND, "HEAD",
                        sandbox.config(server="http://127.0.0.1:1"))
    captured = capsys.readouterr()
    assert result is None
    assert "not healthy" in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# KC-24: the ticket on offer ahead, and the two ways out
# ─────────────────────────────────────────────────────────────────────────────

KC24_ROUND = 54
KC53 = "53-kc14-harvest-accepts-only-a-hex-sha.md"
KC54 = "54-kc15-a-bash-redirect-outside-the-worktree.md"
KC53_TITLE = "harvest accepts only a hex sha"
KC54_TITLE = "a bash redirect outside the worktree"

KC_TICKET = """# KC-{tag} — {title}

**Status:** {status} — round {num} of the KC-24 intake test.
**Severity:** LOW
**File:** `pkg/thing.py`
**Symbol:** `thing`
**Round:** {num}
**Size:** S
**Also touches:** `tests/test_thing.py` (new)

body
"""


def _kc_ticket(tag, title, num, status="open") -> str:
    return KC_TICKET.format(tag=tag, title=title, num=num, status=status)


def _kc_sandbox(tmp_path, status_53="open", status_54="open") -> Sandbox:
    """The ticket's Acceptance sandbox: 53 ahead of 54, both on offer at HEAD."""
    return Sandbox(tmp_path, tickets=[
        (KC53, _kc_ticket("14", KC53_TITLE, "53", status_53)),
        (KC54, _kc_ticket("15", KC54_TITLE, "54", status_54)),
    ])


def _park_line(captured) -> str:
    """The printed park command, the `intake:` prefix and the label cut off."""
    (line,) = [line for line in captured.err.splitlines() if line.startswith("intake:   or park it")]
    return line.split("re-run:", 1)[1].strip()


def test_intake_names_the_ticket_the_sessions_would_get_and_both_ways_out(
        tmp_path, monkeypatch, capsys, spawn_holder):
    """53 on offer ahead of 54: four `intake:` lines, a `run` line naming 53,
    a `sed` on 53's own status line number, and no `kilo serve` spawned."""
    sb = _kc_sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket", "54", "--models", "x", "--max-parallel", "2"])

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == cli.EXIT_FAILED
    assert len(lines) == 4
    assert lines[0] == ("intake: KC-14 (53) is on offer ahead of KC-15 (54) — the session prompt "
                        "names no ticket; scripts/next_task.py would hand the sessions KC-14 (53)")
    assert lines[1] == ("intake:   planned for the sessions: epic-tasks/" + KC53
                        + " (**Status:** open)")
    label, _, run_cmd = lines[2].partition("python3 -m tools.contest ")
    assert " ".join(label.split()) == "intake: run that one instead:"
    assert run_cmd.strip() == "run --ticket 53 --models x --max-parallel 2"
    park = _park_line(captured)
    assert park == ("sed -i '3s/^\\*\\*Status:\\*\\* open/**Status:** queued/' epic-tasks/" + KC53
                    + " && git commit -m 'epic-tasks: KC-14 queued — round 53 runs elsewhere' "
                      "-- epic-tasks/" + KC53)
    assert lines[3] == "intake:   or park it, then re-run:  " + park
    assert not spawn_holder, "no kilo serve may be started"
    assert not (sb.rounds / f"{KC24_ROUND:02d}-x").exists()
    assert not (sb.repo / "contest-out" / f"{KC24_ROUND:02d}").exists()


def test_intake_prints_one_block_per_ticket_on_offer_lowest_first(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, tickets=[
        ("52-kc12-stall-detection.md", _kc_ticket("12", "stall detection", "52")),
        (KC53, _kc_ticket("14", KC53_TITLE, "53")),
        (KC54, _kc_ticket("15", KC54_TITLE, "54")),
    ])
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == cli.EXIT_FAILED
    assert len(lines) == 8
    assert lines[0] == ("intake: KC-12 (52) is on offer ahead of KC-15 (54) — the session prompt "
                        "names no ticket; scripts/next_task.py would hand the sessions KC-12 (52)")
    assert lines[4] == ("intake: KC-14 (53) is on offer ahead of KC-15 (54) — the session prompt "
                        "names no ticket; scripts/next_task.py would hand the sessions KC-14 (53)")
    assert "--ticket 52 --no-tests --no-gate" in lines[2]
    assert "--ticket 53 --no-tests --no-gate" in lines[6]


def test_intake_refuses_a_ticket_on_offer_that_is_not_open(tmp_path, monkeypatch, capsys):
    """`running` is not in `PARKED`, so it blocks exactly as `open` does."""
    sb = _kc_sandbox(tmp_path, status_53="running")
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])

    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "KC-14 (53) is on offer ahead of KC-15 (54)" in captured.err
    assert "epic-tasks/" + KC53 + " (**Status:** running)" in captured.err


def test_intake_passes_once_the_ticket_ahead_is_queued(tmp_path, monkeypatch, capsys):
    """`queued` is in `PARKED`: the pre-KC-24 behaviour, unchanged."""
    sb = _kc_sandbox(tmp_path, status_53="queued")
    monkeypatch.chdir(sb.repo)

    result = cli.intake(sb.repo, sb.repo / "epic-tasks", KC24_ROUND, "HEAD", sb.config())

    captured = capsys.readouterr()
    assert result is not None
    assert captured.err == ""
    assert result.ticket_path.name == KC54
    assert result.title == "KC-15 — " + KC54_TITLE


def test_intake_replaces_the_ticket_of_the_equal_spelling(tmp_path, monkeypatch, capsys):
    """`--ticket=54` → the printed `run` line carries `--ticket=53`."""
    sb = _kc_sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket=54", "--no-tests", "--no-gate"])

    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    line = next(line for line in captured.err.splitlines() if "tools.contest run" in line)
    assert "python3 -m tools.contest run --ticket=53 --no-tests --no-gate" in line


def test_intake_reads_the_status_from_the_base_not_the_checkout(tmp_path, monkeypatch, capsys):
    """HEAD has 53 open and `parked` parks it: `--base parked` passes intake and
    the bare `run` refuses — the checkout's word is not the sessions'."""
    sb = _kc_sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _git(sb.repo, "checkout", "-q", "-b", "parked")
    sb.commit_ticket(KC53, _kc_ticket("14", KC53_TITLE, "53", "queued"))
    _git(sb.repo, "checkout", "-q", "main")

    result = cli.intake(sb.repo, sb.repo / "epic-tasks", KC24_ROUND, "parked", sb.config())
    assert result is not None
    assert capsys.readouterr().err == ""

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "KC-14 (53) is on offer ahead of KC-15 (54)" in captured.err
    assert "sed -i" in captured.err, "the base is HEAD: the park is the sed"


def test_intake_refuses_when_the_base_has_the_ticket_on_offer(tmp_path, monkeypatch, capsys):
    """The reverse layout: 53 parked at HEAD, open on `parked`. The bare `run`
    passes and `--base parked` refuses — with the park line in the one-sentence
    form, since a `sed` in this checkout would not reach the sessions."""
    sb = _kc_sandbox(tmp_path, status_53="queued")
    monkeypatch.chdir(sb.repo)
    _git(sb.repo, "checkout", "-q", "-b", "parked")
    sb.commit_ticket(KC53, _kc_ticket("14", KC53_TITLE, "53", "open"))
    _git(sb.repo, "checkout", "-q", "main")

    result = cli.intake(sb.repo, sb.repo / "epic-tasks", KC24_ROUND, "HEAD", sb.config())
    assert result is not None
    assert capsys.readouterr().err == ""

    code = cli.main(["run", "--ticket", "54", "--base", "parked", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "KC-14 (53) is on offer ahead of KC-15 (54)" in captured.err
    assert "epic-tasks/" + KC53 + " (**Status:** open)" in captured.err
    parked = [line for line in captured.err.splitlines() if "reachable from --base" in line]
    assert len(parked) == 1
    assert parked[0] == ("intake:   or park it in a commit reachable from --base parked "
                         "(the sessions read epic-tasks/ from there, not from this checkout)")
    assert "sed -i" not in captured.err


def test_intake_reads_the_rounds_ticket_from_the_base(tmp_path, monkeypatch, capsys):
    """The requested ticket's own status is the base's word too: `parked` has 54
    as `queued`, so `--base parked` refuses it though this checkout says `open`."""
    sb = _kc_sandbox(tmp_path, status_53="queued")
    monkeypatch.chdir(sb.repo)
    _git(sb.repo, "checkout", "-q", "-b", "parked")
    sb.commit_ticket(KC54, _kc_ticket("15", KC54_TITLE, "54", "queued"))
    _git(sb.repo, "checkout", "-q", "main")

    result = cli.intake(sb.repo, sb.repo / "epic-tasks", KC24_ROUND, "HEAD", sb.config())
    assert result is not None
    assert capsys.readouterr().err == ""

    code = cli.main(["run", "--ticket", "54", "--base", "parked", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "intake: " + KC54 + " is not open (**Status:** queued)" in captured.err
    assert "on offer ahead of" not in captured.err


def test_the_printed_park_line_parks_the_ticket(tmp_path, monkeypatch, capsys):
    """The `sed && git commit` intake prints is a real command: run it and the
    round starts — the park needs the commit, not just the edit."""
    sb = _kc_sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    park = _park_line(captured)
    assert park.endswith("&& git commit -m 'epic-tasks: KC-14 queued — round 53 runs elsewhere' "
                         "-- epic-tasks/" + KC53)

    proc = subprocess.run(["bash", "-c", park], cwd=str(sb.repo), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    body = (sb.repo / "epic-tasks" / KC53).read_text(encoding="utf-8")
    assert "**Status:** queued" in body and "**Status:** open" not in body
    result = cli.intake(sb.repo, sb.repo / "epic-tasks", KC24_ROUND, "HEAD", sb.config())
    captured = capsys.readouterr()
    assert result is not None
    assert captured.err == ""


def test_intake_refuses_a_park_that_is_not_committed(tmp_path, monkeypatch, capsys):
    """A `sed` without its commit is refused as a dirty `epic-tasks/`: that is
    why the printed park line carries the commit."""
    sb = _kc_sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    sed_only = _park_line(captured).split(" && ")[0]

    proc = subprocess.run(["bash", "-c", sed_only], cwd=str(sb.repo),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    code = cli.main(["run", "--ticket", "54", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "epic-tasks/ has uncommitted or untracked changes" in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# KC-25: the roster's provider/model pairs against GET /provider
# ─────────────────────────────────────────────────────────────────────────────

def _provider(provider_id, name, model_ids):
    """One entry of a `GET /provider` body, for `roster_on_offer` alone."""
    return {"id": provider_id, "name": name, "source": "static",
            "models": {model_id: {"id": model_id, "providerID": provider_id,
                                  "name": model_id, "status": "active"}
                       for model_id in model_ids}}


def _offer(providers, connected=("kenary",)):
    """A whole `GET /provider` body around the providers named."""
    return {"all": list(providers), "default": {}, "connected": list(connected),
            "failed": []}


#: The offer those tests read: one provider whose `id` is `kenary` and whose
#: `name` is the display string `kenari` — the pair `kenari/hy3:free` is the
#: mistake the round lost four slots to.
KC25_OFFER = _offer((_provider("kenary", "kenari",
                               ("hy3:free", "agnes-2-5-flash:free",
                                "agnes-2-0-flash:free")),))


def test_roster_on_offer_names_the_id_to_use_when_the_display_name_is_spelled():
    """`kenari` is the display name, not the id: the refusal names the id to use."""
    (agent,) = cli.agents_from_models("kenari/hy3:free")
    (line,) = cli.roster_on_offer(KC25_OFFER, (agent,))
    assert line == ("[hy3] kenari/hy3:free: no provider 'kenari' — that is the display name "
                    "of provider 'kenary'; use kenary/hy3:free")


def test_roster_on_offer_names_the_connected_providers_when_no_name_matches():
    (agent,) = cli.agents_from_models("sensenova123/sensenova-6.8-flash-lite")
    (line,) = cli.roster_on_offer(KC25_OFFER, (agent,))
    assert line == ("[sensenova-6-8-flash-lite] sensenova123/sensenova-6.8-flash-lite: "
                    "no provider 'sensenova123' — connected: kenary")


def test_roster_on_offer_names_a_provider_without_credentials():
    (agent,) = cli.agents_from_models("hy3:free")
    (line,) = cli.roster_on_offer(_offer((_provider("kenary", "kenari", ("hy3:free",)),),
                                         connected=()), (agent,))
    assert line == ("[hy3] kenary/hy3:free: provider 'kenary' has no credentials "
                    "(not connected)")


def test_roster_on_offer_lists_the_models_on_offer_and_a_close_one():
    """`hy3` gets no suggestion: `difflib.get_close_matches`' 0.6 cutoff keeps a
    model from its own `:free` variant, so that pair gets the bare line."""
    agents = cli.agents_from_models("agnes-2-5-flash,hy3")
    assert cli.roster_on_offer(KC25_OFFER, agents) == [
        "[agnes-2-5-flash] kenary/agnes-2-5-flash: no model 'agnes-2-5-flash' under 'kenary' — "
        "on offer: agnes-2-0-flash:free, agnes-2-5-flash:free, hy3:free "
        "(did you mean agnes-2-5-flash:free?)",
        "[hy3] kenary/hy3: no model 'hy3' under 'kenary' — on offer: agnes-2-0-flash:free, "
        "agnes-2-5-flash:free, hy3:free",
    ]


def test_roster_on_offer_is_one_line_per_bad_agent_in_roster_order():
    """A good agent in the middle is skipped and the bad ones keep the roster order."""
    agents = cli.agents_from_models("kenari/hy3:free,hy3:free,agnes-2-5-flash")
    lines = cli.roster_on_offer(KC25_OFFER, agents)
    assert len(lines) == 2
    # both hy3 ids are the same name, so KC-34's variant rule suffixes them:
    # the names differ, the provider and model do not
    assert lines[0].startswith("[hy3-var1] kenari/hy3:free: no provider 'kenari'")
    assert lines[1].startswith("[agnes-2-5-flash] kenary/agnes-2-5-flash: no model")


def test_roster_on_offer_is_empty_for_a_roster_entirely_on_offer():
    assert cli.roster_on_offer(KC25_OFFER,
                               cli.agents_from_models("hy3:free,kenary/agnes-2-0-flash:free")) == []


def test_roster_on_offer_ignores_a_model_whose_status_is_not_active():
    """`active` is the only status the server documents here; the others are not
    refusals, and nothing is inferred from `capabilities`."""
    models = dict(KC25_OFFER["all"][0]["models"])
    models["hy3:free"]["status"] = "unknown"
    offer = _offer((_provider("kenary", "kenari", models),))
    assert cli.roster_on_offer(offer, cli.agents_from_models("hy3:free")) == []


def test_agents_from_models_takes_its_default_provider_from_the_flag():
    """`--provider sensenova123 --models sensenova-6.8-flash-lite`."""
    (spec,) = cli.agents_from_models("sensenova-6.8-flash-lite", provider="sensenova123")
    assert spec == AgentSpec(provider_id="sensenova123", model_id="sensenova-6.8-flash-lite",
                             name="sensenova-6-8-flash-lite")


def test_a_models_id_with_its_own_prefix_wins_over_the_provider_flag():
    (spec,) = cli.agents_from_models("kenary/hy3:free", provider="sensenova123")
    assert (spec.provider_id, spec.model_id) == ("kenary", "hy3:free")


def test_a_models_id_splits_its_provider_at_the_first_slash_like_the_roster():
    """`kilo/nex-agi/nex-n2.5-pro:free` is provider `kilo`; the last `/` made it
    provider `kilo/nex-agi`, which no server has."""
    (spec,) = cli.agents_from_models("kilo/nex-agi/nex-n2.5-pro:free")
    assert spec == AgentSpec(provider_id="kilo", model_id="nex-agi/nex-n2.5-pro:free",
                             name="nex-n2-5-pro")
    assert spec.model == "kilo/nex-agi/nex-n2.5-pro:free"


def test_a_provider_without_credentials_names_the_login_command():
    (agent,) = cli.agents_from_models("hy3:free")
    (line,) = cli.roster_on_offer(_offer((_provider("kenary", "kenari", ("hy3:free",)),),
                                         connected=()), (agent,), kilo_bin="/x/kilo")
    assert line == ("[hy3] kenary/hy3:free: provider 'kenary' has no credentials "
                    "(not connected) — fix: /x/kilo auth login -p kenary")


def test_agents_from_models_defaults_to_the_kenary_provider():
    assert cli.agents_from_models("hy3:free")[0].provider_id == cli.DEFAULT_PROVIDER == "kenary"


KC25_SCENARIO = {"providers": KC25_OFFER,
                 "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}


def test_run_refuses_a_model_id_spelled_as_a_display_name(sandbox, capsys, spawn_holder):
    """`--models kenari/hy3:free` is refused at intake with the id to use — before
    a worktree, an output directory or a session exists."""
    code, fake = run_fake(sandbox, KC25_SCENARIO,
                          ["--ticket", "1", "--models", "kenari/hy3:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: [hy3] kenari/hy3:free: no provider 'kenari' — that is the display "
                     "name of provider 'kenary'; use kenary/hy3:free"]
    assert not (sandbox.rounds / f"{ROUND:02d}-hy3").exists()
    assert not sandbox.out().exists()
    assert _sessions(fake) == [], "no session may be created before intake passes"


def test_run_passes_the_same_roster_with_the_provider_id(sandbox, capsys, spawn_holder):
    """`kenary/hy3:free`: intake passes and the plan shows the pair it checked."""
    code, fake = run_fake(sandbox, KC25_SCENARIO,
                          ["--ticket", "1", "--models", "kenary/hy3:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert "intake:" not in captured.err
    assert _plan(captured.out)["agents"] == "1: kenary/hy3:free"
    assert sorted(entry.name for entry in sandbox.rounds.iterdir()) == [f"{ROUND:02d}-hy3"]
    assert len(_sessions(fake)) == 1
    assert _sessions(fake)[0]["body"]["model"] == {"providerID": "kenary", "id": "hy3:free"}


def test_run_checks_the_provider_flag_against_the_offer(sandbox, capsys, spawn_holder):
    """`--provider sensenova123` is the default behind a bare id, and that pair is
    checked too: the offer has no such provider."""
    code, fake = run_fake(sandbox, KC25_SCENARIO,
                          ["--ticket", "1", "--provider", "sensenova123",
                           "--models", "sensenova-6.8-flash-lite",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: [sensenova-6-8-flash-lite] sensenova123/sensenova-6.8-flash-lite: "
                     "no provider 'sensenova123' — connected: kenary"]
    assert not sandbox.out().exists()
    assert _sessions(fake) == []


def test_run_reports_a_failed_get_provider_as_one_intake_line(sandbox, capsys, spawn_holder):
    """`GET /provider` answering 500 is one line and never crashes intake."""
    scenario = dict(KC25_SCENARIO, providers_status=500)
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert len(lines) == 1
    assert lines[0].startswith("intake: GET /provider failed: ")
    assert "500" in lines[0] and "/provider" in lines[0]
    assert "server:" not in captured.err
    assert not sandbox.out().exists()
    assert _sessions(fake) == []


def test_intake_checks_the_offer_against_the_attached_server(sandbox, capsys, spawn_holder):
    """`server = <url>`: the attached server answers the offer check and nothing
    is spawned for it — the throwaway is only the price of `server = spawn`."""
    with FakeKiloServer({"providers": KC25_OFFER}) as fake:
        config = sandbox.config(server=fake.url, agents=cli.agents_from_models("kenari/hy3:free"))
        result = cli.intake(sandbox.repo, sandbox.repo / "epic-tasks", ROUND, "HEAD", config)

    captured = capsys.readouterr()
    assert result is None
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert lines == ["intake: [hy3] kenari/hy3:free: no provider 'kenari' — that is the display "
                     "name of provider 'kenary'; use kenary/hy3:free"]
    assert not spawn_holder, "an attached server must not start a second one"
    assert fake.calls(method="GET", path="/provider")


def test_run_puts_the_offers_context_limit_on_the_agents_spec(sandbox, capsys, spawn_holder,
                                                       monkeypatch):
    """KC-56: `limit.context` from the `GET /provider` read intake makes lands
    on the agent's spec the round runs — no second read of the offer."""
    offer = copy.deepcopy(KC25_OFFER)
    offer["all"][0]["models"]["hy3:free"]["limit"] = {"context": 262_144, "output": 32_000}
    seen: dict = {}
    real = cli.run_round

    def spy(config, *args, **kwargs):
        seen["config"] = config
        return real(config, *args, **kwargs)

    monkeypatch.setattr(cli, "run_round", spy)
    code, fake = run_fake(sandbox, dict(KC25_SCENARIO, providers=offer),
                          ["--ticket", "1", "--models", "kenary/hy3:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    capsys.readouterr()
    assert code == 0
    (spec,) = seen["config"].agents
    assert spec.model == "kenary/hy3:free" and spec.context_limit == 262_144


def test_context_limits_reads_limit_context_per_provider_model():
    """`GET /provider` → `provider/model: limit.context`; a model with no
    positive integer limit is left out, and junk is an empty dict."""
    offer = {"all": [
        {"id": "kenary", "models": {
            "a:free": {"id": "a:free", "limit": {"context": 262_144, "output": 32_000}},
            "b:free": {"id": "b:free", "limit": {"context": 0}},
            "c:free": {"id": "c:free"},
            "d:free": {"id": "d:free", "limit": {"context": "big"}},
            "e:free": {"id": "e:free", "limit": {"context": True}},
            # the `models` key is what a roster names, whatever `id` says
            "f:free": {"id": "other-name", "limit": {"context": 64_000}},
        }},
        {"id": "other", "models": {"x": {"limit": {"context": 1000}}}},
        {"name": "no id", "models": {"y": {"limit": {"context": 5}}}},
        "junk",
    ]}
    assert cli._context_limits(offer) == {"kenary/a:free": 262_144, "kenary/f:free": 64_000,
                                          "other/x": 1000}
    assert cli._context_limits(None) == {} and cli._context_limits({"all": None}) == {}


def test_with_context_limits_puts_the_limit_on_the_matching_spec():
    """Only the agent whose `provider/model` has an entry changes; the rest keep
    `None`, and nothing else about a spec moves."""
    a = AgentSpec("a", "kenary", "a:free", variant="high")
    b = AgentSpec("b", "kenary", "b:free")
    (a2, b2) = cli._with_context_limits((a, b), {"kenary/a:free": 262_144})
    assert a2 == replace(a, context_limit=262_144) and a2.variant == "high"
    assert b2 is b and b2.context_limit is None


def test_the_offer_check_closes_its_throwaway_server(tmp_path, monkeypatch, capsys):
    """`server = spawn` starts a throwaway for the offer call alone: it is closed
    and its log removed, because `cmd_run` starts its own afterwards."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    closed, logs = [], []

    class Throwaway:
        @property
        def base_url(self) -> str:
            return fake.url

        def close(self) -> None:
            closed.append(True)

    def spawn_server(binary, *, log_path):
        logs.append(log_path)
        return Throwaway()

    monkeypatch.setattr(cli.KiloServer, "spawn", staticmethod(spawn_server))
    with FakeKiloServer({"providers": KC25_OFFER}) as fake:
        config = sb.config(agents=cli.agents_from_models("kenari/hy3:free"))
        result = cli.intake(sb.repo, sb.repo / "epic-tasks", ROUND, "HEAD", config)

    captured = capsys.readouterr()
    assert result is None
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert lines == ["intake: [hy3] kenari/hy3:free: no provider 'kenari' — that is the display "
                     "name of provider 'kenary'; use kenary/hy3:free"]
    assert closed == [True], "the check closes the server it started for the offer"
    assert len(logs) == 1 and not Path(logs[0]).exists(), "its log goes with it"


def test_run_help_names_the_provider_flag():
    help_out = subprocess.run([sys.executable, "-m", "tools.contest", "run", "--help"],
                              capture_output=True, text=True, cwd=REPO_ROOT)
    assert help_out.returncode == 0
    assert "--provider" in help_out.stdout
    assert "provider/model" in help_out.stdout


def test_agents_from_models_squeezes_the_name_and_keeps_its_own_provider():
    specs = cli.agents_from_models("x:free,y:free")
    assert [spec.model for spec in specs] == ["kenary/x:free", "kenary/y:free"]
    (explicit,) = cli.agents_from_models("anthropic/claude-haiku-latest:free")
    assert explicit.provider_id == "anthropic"
    assert explicit.name == "claude-haiku-latest"


def test_export_patches_skips_a_claimed_commit_without_a_workspace(tmp_path):
    """A claimed commit with no workspace to format from: no file, just a warning."""
    state = RoundState(round_no=ROUND, ticket=TICKET_01, base_sha="0" * 40, started_at=1.0,
                       agents=[AgentRun(agent=AgentSpec("agent-a", "kenary", "agent-a:free"),
                                        workspace=None, commit="1" * 40)])
    written = cli.export_patches(state, [], tmp_path / "out")
    assert written == []
    assert not (tmp_path / "out").exists()


def test_export_patches_names_a_stalled_or_error_patch_by_its_state(tmp_path):
    """A `STALLED` agent with a commit gets `.STALLED.patch`, an `ERROR` one
    `.ERROR.patch`; `READY` and `GAVE_UP` keep their names, and each file is the
    branch's own `git format-patch`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "thing.py", "def thing():\n    return 1\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "patch@example.invalid")
    _git(repo, "config", "user.name", "patch")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    workspaces: dict = {}
    shas: dict = {}
    runs: list = []
    for name, state in (("agent-a", AgentState.READY), ("agent-b", AgentState.GAVE_UP),
                        ("agent-c", AgentState.STALLED), ("agent-d", AgentState.ERROR)):
        path = tmp_path / "wt" / name
        _git(repo, "worktree", "add", "-q", "-b", f"contest/{ROUND:02d}/{name}", str(path), base)
        _write(path / "pkg" / "thing.py", THING_CHANGED)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"KC-21: {name}")
        shas[name] = _git(path, "rev-parse", "HEAD")
        workspaces[name] = Workspace(agent=name, path=path, branch=f"contest/{ROUND:02d}/{name}",
                                     base_sha=base, kind="worktree")
        runs.append(AgentRun(agent=AgentSpec(name, "kenary", f"{name}:free"), workspace=workspaces[name],
                             state=state, commit=shas[name]))
    state = RoundState(round_no=ROUND, ticket=TICKET_01, base_sha=base, started_at=1.0, agents=runs)

    out = tmp_path / "out"
    written = cli.export_patches(state, list(workspaces.values()), out)
    assert [path.name for path in written] == ["agent-a.patch", "agent-b.GAVE_UP.patch",
                                               "agent-c.STALLED.patch", "agent-d.ERROR.patch"]
    for name, patch in zip(("agent-a", "agent-b", "agent-c", "agent-d"), written):
        text = patch.read_text(encoding="utf-8")
        assert text.startswith(f"From {shas[name]}")
        assert "def thing():" in text
    assert not (out / "agent-c.patch").exists() and not (out / "agent-d.GAVE_UP.patch").exists()


def test_a_stall_with_a_valid_commit_counts_as_ready_and_exits_zero(sandbox, capsys, spawn_holder):
    """A silence stall after the commit: the terminal harvest scores agent-a READY,
    so the run exports `agent-a.patch` and exits 0, while agent-b stalled with an
    empty branch stays STALLED with no patch and no harvest."""
    ini = sandbox.repo / "contest.ini"
    # FL-1 (round 84): 5 s, not 1 s. The window only has to be short enough
    # that the silent agent stalls inside the turn deadline — its exact value
    # is not the claim — and agent-a commits from its hook, so a 1 s window
    # was a real `git` commit racing a wall clock. FakeKiloServer heartbeats
    # through the hook now, but a 1 s window leaves no room for that beat to
    # be *delivered* late on a loaded box.
    ini.write_text(ini.read_text(encoding="utf-8").replace("idle_event_timeout_sec = 900",
                                                            "idle_event_timeout_sec = 5"),
                   encoding="utf-8")
    scenario = {"turns": [{"on_prompt": lambda directory, text: work_ready(directory, text)
                                          if Path(directory).name.endswith("agent-a") else None,
                           "events": [], "idle": False}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()
    out = sandbox.out()

    assert code == 0
    assert [row["state"] for row in _table(captured.out)] == ["READY", "STALLED"]
    assert (out / "agent-a.patch").is_file()
    assert not (out / "agent-a.STALLED.patch").exists()
    assert not (out / "agent-b.patch").exists() and not (out / "agent-b.STALLED.patch").exists()

    state = RoundState.from_dict(json.loads((out / "state.json").read_text(encoding="utf-8")))
    ready, stalled = state.agents
    assert ready.commit and ready.state is AgentState.READY
    assert ready.turns[0]["idle_status"] == "stalled"
    assert ready.turns[0]["harvest"]["verdict"] == "READY"
    assert stalled.commit is None and stalled.state is AgentState.STALLED
    assert "harvest" not in stalled.turns[0]


# ─────────────────────────────────────────────────────────────────────────────
# the round, end to end
# ─────────────────────────────────────────────────────────────────────────────

def test_run_exports_a_patch_per_agent_and_exits_zero(sandbox, capsys, spawn_holder):
    """One READY and one GAVE_UP with a commit: exit 0, a `.patch` and a
    `.GAVE_UP.patch`, both present, the tree of the branch, the table printed."""
    code, fake = run_fake(sandbox, SCENARIO_ONE_READY,
                          ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    out = sandbox.out()

    assert code == 0
    assert (out / "agent-a.patch").is_file()
    assert not (out / "agent-a.GAVE_UP.patch").exists()
    assert (out / "agent-b.GAVE_UP.patch").is_file()
    assert not (out / "agent-b.patch").exists()
    assert (out / "state.json").is_file()
    assert (out / "agent-a" / "turns.jsonl").is_file()

    state = RoundState.from_dict(json.loads((out / "state.json").read_text(encoding="utf-8")))
    by_name = {run.agent.name: run for run in state.agents}
    assert sorted(by_name) == ["agent-a", "agent-b"]
    assert by_name["agent-a"].state is AgentState.READY
    assert by_name["agent-b"].state is AgentState.GAVE_UP
    assert by_name["agent-b"].commit

    rows = _table(captured.out)
    assert [row["name"] for row in rows] == ["agent-a", "agent-b"]
    assert [row["state"] for row in rows] == ["READY", "GAVE_UP"]
    assert _patches(captured.out) == [str(out / "agent-a.patch"),
                                      str(out / "agent-b.GAVE_UP.patch")]

    worktree = sandbox.rounds / f"{ROUND:02d}-agent-a"
    assert _git(worktree, "rev-parse", "HEAD") == by_name["agent-a"].commit

    fresh = sandbox.tmp / "am"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main")
    _git(fresh, "config", "user.email", "am@example.invalid")
    _git(fresh, "config", "user.name", "am")
    _git(fresh, "fetch", "-q", str(sandbox.repo), f"{sandbox.base}:base")
    _git(fresh, "checkout", "-q", "base")
    _git(fresh, "am", "-q", str(out / "agent-a.patch"))
    assert _git(fresh, "rev-parse", "HEAD^{tree}") == _git(worktree, "rev-parse", "HEAD^{tree}")


def test_run_exits_two_when_every_agent_gave_up(sandbox, capsys, spawn_holder):
    code, fake = run_fake(sandbox, SCENARIO_NO_TEST,
                          ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    assert code == 2
    assert [row["state"] for row in _table(captured.out)] == ["GAVE_UP", "GAVE_UP"]


def test_run_rejects_when_the_roster_is_missing(sandbox, capsys):
    code = cli.main(["run", "--ticket", "1", "--roster", "no-such.ini", "--no-tests", "--no-gate"])
    captured = capsys.readouterr()
    assert code == 1
    assert "roster file does not exist" in captured.err


def test_resume_restarts_only_the_mid_flight_agent(sandbox, capsys, spawn_holder):
    """`state.json` with one READY and one mid-flight: only the second is
    prompted, and only inside its own worktree."""
    config = sandbox.config()
    prepared = {ws.agent: ws for ws in prepare_round(sandbox.repo, config, ROUND, "HEAD")}
    a, b = prepared["agent-a"], prepared["agent-b"]
    _write(a.path / "pkg" / "thing.py", THING_CHANGED)
    _git(a.path, "add", "-A")
    _git(a.path, "commit", "-q", "-m", "KC-16: thing")
    commit = _git(a.path, "rev-parse", "HEAD")

    spec_a = next(spec for spec in config.agents if spec.name == "agent-a")
    spec_b = next(spec for spec in config.agents if spec.name == "agent-b")
    prior = RoundState(round_no=ROUND, ticket=TICKET_01, base_sha=sandbox.base, started_at=1.0,
                       agents=[AgentRun(agent=spec_a, workspace=a, state=AgentState.READY, commit=commit),
                               AgentRun(agent=spec_b, workspace=b, state=AgentState.WAITING,
                                        session_id="ses_gone")])
    _write(sandbox.out() / "state.json", json.dumps(prior.to_dict()))

    code, fake = run_fake(sandbox, {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]},
                          ["--ticket", "1", "--no-gate", "--no-tests", "--resume"], spawn_holder)
    sessions = _sessions(fake)
    assert code == 0
    assert len(sessions) == 1
    assert sessions[0]["query"]["directory"] == str(b.path)
    assert "runs/agent-b/PROGRESS.csv" in _prompts(fake)[0][1]
    state = RoundState.from_dict(json.loads((sandbox.out() / "state.json").read_text(encoding="utf-8")))
    assert {run.state for run in state.agents} == {AgentState.READY}


def test_resume_without_a_state_file_is_an_intake_failure(sandbox, capsys):
    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests", "--resume"])
    captured = capsys.readouterr()
    assert code == 1
    assert "state.json" in captured.err and "nothing to resume" in captured.err


def _worktree_with_a_commit(sb, agent: str = "agent-a") -> tuple:
    """The round prepared, then that worktree holding one committed change — the
    crashed attempt an operator reruns the command into (KC-23)."""
    prepared = {ws.agent: ws for ws in prepare_round(sb.repo, sb.config(), ROUND, "HEAD")}
    worktree = prepared[agent].path
    _write(worktree / "leftover.txt", "mid-run garbage\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "KC-16: thing")
    return worktree, _git(worktree, "rev-parse", "HEAD")


def test_run_refuses_to_reset_a_worktree_that_carries_a_commit(sandbox, capsys, spawn_holder):
    """A rerun over a branch with a commit is an intake failure naming
    ``--fresh``: exit 1, no server started, the work left exactly where it was."""
    worktree, commit = _worktree_with_a_commit(sandbox)

    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip().startswith("intake: ")
    assert "--fresh" in captured.err
    assert "--resume" in captured.err
    assert str(worktree) in captured.err
    assert not spawn_holder, "no kilo serve may be started"
    assert _git(worktree, "rev-parse", "HEAD") == commit
    assert (worktree / "leftover.txt").exists()
    assert not sandbox.out().exists()


def test_run_fresh_resets_the_worktree_and_proceeds(sandbox, capsys, spawn_holder):
    """The same sandbox with ``--fresh``: the work is discarded, the branch lands
    back on the base, and the round runs to READY."""
    worktree, commit = _worktree_with_a_commit(sandbox)

    code, fake = run_fake(sandbox, {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]},
                          ["--ticket", "1", "--no-gate", "--no-tests", "--fresh"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert "intake:" not in captured.err
    assert not (worktree / "leftover.txt").exists()
    assert _git(worktree, "rev-parse", "HEAD") != commit
    assert [row["state"] for row in _table(captured.out)] == ["READY", "READY"]


def test_models_and_max_parallel_replace_the_roster(sandbox, capsys, spawn_holder):
    """`--models x:free,y:free --max-parallel 1`: the plan names only those two
    models, and the worktrees are theirs alone."""
    code, fake = run_fake(sandbox, {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]},
                          ["--ticket", "1", "--models", "x:free,y:free", "--max-parallel", "1",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    plan = _plan(captured.out)
    assert code == 0
    assert plan["agents"] == "2: kenary/x:free, kenary/y:free"
    assert plan["parallel"] == "1" and plan["tests"] == "off" and plan["gate"] == "off"
    assert plan["out"] == str(sandbox.out())
    assert "agent-a" not in captured.out and "agent-b" not in captured.out
    assert sorted(entry.name for entry in sandbox.rounds.iterdir()) == ["01-x", "01-y"]
    creates = [record["query"]["directory"] for record in _sessions(fake)]
    assert len(creates) == 2
    assert all("01-x" in path or "01-y" in path for path in creates)


def test_no_gate_records_gate_failed_and_makes_no_call(sandbox, capsys, spawn_holder, monkeypatch):
    """`--no-gate` with no resolvable api_key at all: the mechanical layer still
    decides, and every ask it cannot decide is the existing `gate-failed` reject
    — with no HTTP call attempted and no key check at intake."""
    calls = []

    def request_completion(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the gate must not be called with --no-gate")

    monkeypatch.delenv("CONTEST_GATE_API_KEY")
    monkeypatch.setattr("tools.contest.policy.request_completion", request_completion)
    monkeypatch.setattr("tools.llm_stream.request_completion", request_completion)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"],
                           "permission": _permission_outside("/var/lib/*")},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()
    assert code == 0
    assert calls == []
    assert _plan(captured.out)["gate"] == "off"
    decisions = _jsonl(sandbox.out() / "agent-a" / "decisions.jsonl")
    assert decisions, "the ask must be recorded like any decision"
    (line,) = decisions
    assert (line["layer"], line["reply"]) == ("gate-failed", "reject")
    assert line["reason"] == "gate unavailable: no gate model configured"
    assert line["gate_model"] == ""


def test_the_round_starts_without_a_resolvable_gate_key(sandbox, capsys, spawn_holder, monkeypatch):
    """No `--no-gate` and no key in the environment: the roster still loads and
    the round runs — the gate answers `gate unavailable: …` per ask instead."""
    calls = []

    def request_completion(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the gate must not be called with --no-gate")

    monkeypatch.delenv("CONTEST_GATE_API_KEY")
    monkeypatch.setattr("tools.contest.policy.request_completion", request_completion)
    monkeypatch.setattr("tools.llm_stream.request_completion", request_completion)
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"],
                           "permission": _permission_outside("/var/lib/*")},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    assert code == 0
    # KC-37 §3 / KC-55 §6: the plan names the model and the host the gate sits
    # on, not just `on` — the host is what round 66 lost to
    assert _plan(captured.out)["gate"] == "test/gate @ 127.0.0.1"
    decisions = _jsonl(sandbox.out() / "agent-a" / "decisions.jsonl")
    (line,) = decisions
    assert line["layer"] == "gate-failed" and line["reply"] == "reject"
    assert "gate unavailable" in line["reason"]
    assert line["gate_model"] == "test/gate"


def test_tests_flag_toggles_the_roots_in_the_plan(sandbox, capsys, spawn_holder):
    """The default is on: the plan says so, and the roots really run in the
    worktree — in a temp dir, not in this repo's own `tests/`."""
    one_turn = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, one_turn, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    assert code == 0 and _plan(capsys.readouterr().out)["tests"] == "off"

    # the first run left a commit in the worktree: KC-23 makes the rerun say so
    code, fake = run_fake(sandbox, one_turn, ["--ticket", "1", "--no-gate", "--fresh"], spawn_holder)
    assert code == 0 and _plan(capsys.readouterr().out)["tests"] == "on"
    turns = _jsonl(sandbox.out() / "agent-a" / "turns.jsonl")
    assert turns[0]["harvest"]["verdict"] == "READY"


def test_module_entry_point_help_and_usage():
    """`run --help` lists every flag; a bare call is usage with exit 2."""
    help_out = subprocess.run([sys.executable, "-m", "tools.contest", "run", "--help"],
                              capture_output=True, text=True, cwd=REPO_ROOT)
    assert help_out.returncode == 0
    for flag in ("--ticket", "--roster", "--base", "--models", "--backend", "--provider",
                 "--max-parallel", "--no-tests", "--no-gate", "--resume", "--out"):
        assert flag in help_out.stdout

    bare = subprocess.run([sys.executable, "-m", "tools.contest"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
    assert bare.returncode == 2
    assert "usage: tools.contest" in bare.stderr


# ─────────────────────────────────────────────────────────────────────────────
# KC-34 §7 — a model listed twice runs as <name>-var1, <name>-var2
# ─────────────────────────────────────────────────────────────────────────────

def test_agents_from_models_leaves_a_single_name_untouched():
    """§7 guard, green on the base: a name that appears once is today's name."""
    (spec,) = cli.agents_from_models("hy3:free")
    assert (spec.name, spec.provider_id, spec.model_id) == ("hy3", "kenary", "hy3:free")
    assert [agent.name for agent in cli.agents_from_models("x:free,y:free")] == ["x", "y"]


def test_agents_from_models_suffixes_a_repeated_name():
    """Two identical ids are two variants: one worktree, branch and patch each."""
    specs = cli.agents_from_models("hy3:free,hy3:free")
    assert [spec.name for spec in specs] == ["hy3-var1", "hy3-var2"]
    assert [spec.model_id for spec in specs] == ["hy3:free", "hy3:free"]
    assert [spec.provider_id for spec in specs] == ["kenary", "kenary"]


def test_agents_from_models_suffixes_only_the_repeated_name_in_list_order():
    """The operator's own command line: three singles keep their names, the two
    `hy3` ids get the suffixes in the order they were listed."""
    models = "agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,hy3:free,hy3:free"
    names = [spec.name for spec in cli.agents_from_models(models)]
    assert names == ["agnes-2-5-flash", "mimo-v2-5", "step-3-7-flash",
                     "hy3-var1", "hy3-var2"]
    assert sorted(names) != names, "the roster order is the list order, not alphabetical"


def test_agents_from_models_numbering_runs_across_the_list():
    """Interleaved repeats keep counting: the second and third `hy3` are var2
    and var3, the single `x` between them is untouched."""
    names = [spec.name for spec in cli.agents_from_models("hy3:free,x:free,hy3:free,hy3:free")]
    assert names == ["hy3-var1", "x", "hy3-var2", "hy3-var3"]


def test_agents_from_models_treats_the_same_name_at_two_tags_as_variants():
    """Same name, different `:tag`: two ids, two variants, the tags survive."""
    specs = cli.agents_from_models("hy3:free,hy3:pro")
    assert [spec.name for spec in specs] == ["hy3-var1", "hy3-var2"]
    assert [spec.model_id for spec in specs] == ["hy3:free", "hy3:pro"]


def test_agents_from_models_skips_a_variant_name_the_list_already_holds():
    """`hy3-var1:free` is a name on its own; the first variant of `hy3` has to
    skip it rather than land on a name two agents would share."""
    names = [spec.name for spec in cli.agents_from_models("hy3-var1:free,hy3:free,hy3:free")]
    assert names == ["hy3-var1", "hy3-var2", "hy3-var3"]


def test_agents_from_models_keeps_each_variant_on_its_own_provider():
    """`kenary/hy3:free,openrouter/hy3:free` are two variants of one name, each
    keeping the provider its own id said."""
    specs = cli.agents_from_models("kenary/hy3:free,openrouter/hy3:free")
    assert [(spec.name, spec.provider_id) for spec in specs] == [
        ("hy3-var1", "kenary"), ("hy3-var2", "openrouter")]


def test_agents_from_models_applies_the_provider_flag_to_every_variant():
    specs = cli.agents_from_models("hy3:free,hy3:free", provider="openrouter")
    assert [(spec.name, spec.provider_id) for spec in specs] == [
        ("hy3-var1", "openrouter"), ("hy3-var2", "openrouter")]


def test_agents_from_models_is_a_pure_function_of_the_models_string():
    """`--resume` re-runs the same string and has to find the same agents in
    state.json: the names come from the string alone, not from the live roster."""
    models = "hy3:free,x:free,hy3:free"
    first = [spec.name for spec in cli.agents_from_models(models)]
    second = [spec.name for spec in cli.agents_from_models(models)]
    assert first == second == ["hy3-var1", "x", "hy3-var2"]


def test_run_with_a_single_model_keeps_its_name_and_patch(sandbox, capsys, spawn_holder):
    """§7 guard, green on the base: one `x:free` is the one folder `01-x` and
    one `x.patch`, exactly as today."""
    code, fake = run_fake(sandbox, {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]},
                          ["--ticket", "1", "--models", "x:free", "--max-parallel", "1",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert sorted(entry.name for entry in sandbox.rounds.iterdir()) == ["01-x"]
    assert sorted(_git(sandbox.rounds / "01-x", "branch", "--show-current").splitlines()) == [
        "contest/01/x"]
    assert len(_sessions(fake)) == 1
    assert (sandbox.out() / "x.patch").is_file()
    assert [row["name"] for row in _table(captured.out)] == ["x"]


def test_run_with_a_twice_listed_model_gives_each_variant_its_own_everything(
        sandbox, capsys, spawn_holder):
    """§7, red on the base, where both agents share the one folder `01-x` and
    the one branch `contest/01/x`: two folders, two branches, two sessions,
    two rows and two patches, one per variant name."""
    scenario = {"turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario,
                          ["--ticket", "1", "--models", "x:free,x:free", "--max-parallel", "2",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert sorted(entry.name for entry in sandbox.rounds.iterdir()) == ["01-x-var1", "01-x-var2"]
    assert sorted(_git(sandbox.rounds / "01-x-var1", "branch", "--show-current").splitlines()) == [
        "contest/01/x-var1"]
    assert sorted(_git(sandbox.rounds / "01-x-var2", "branch", "--show-current").splitlines()) == [
        "contest/01/x-var2"]

    creates = _sessions(fake)
    assert len(creates) == 2
    assert len({record["query"]["directory"] for record in creates}) == 2
    assert all("01-x-var" in record["query"]["directory"] for record in creates)

    rows = _table(captured.out)
    assert [row["name"] for row in rows] == ["x-var1", "x-var2"]
    assert [row["state"] for row in rows] == ["READY", "READY"]

    out = sandbox.out()
    assert (out / "x-var1.patch").is_file()
    assert (out / "x-var2.patch").is_file()
    assert not (out / "x.patch").exists()
    assert _patches(captured.out) == [str(out / "x-var1.patch"), str(out / "x-var2.patch")]

    # two separate worktrees, so two separate claims as well as two patches
    assert (sandbox.rounds / "01-x-var1" / "runs" / "x-var1" / "PROGRESS.csv").is_file()
    assert (sandbox.rounds / "01-x-var2" / "runs" / "x-var2" / "PROGRESS.csv").is_file()
    assert (out / "x-var1.patch").read_text(encoding="utf-8").strip().startswith("From ")
    assert (out / "x-var2.patch").read_text(encoding="utf-8").strip().startswith("From ")


# ─────────────────────────────────────────────────────────────────────────────
# KC-34 §4-§6 — `--backend openrouter`
# ─────────────────────────────────────────────────────────────────────────────

OPENROUTER_ROSTER = """
[contest]
kilo_bin = /no/such/kilo
backend = kilo
openrouter_llm_profile = contest_openrouter_llm
max_parallel = 2
gate_llm_profile = contest_gate_llm
out_dir = contest-out
rounds_dir = {rounds}

[contest_gate_llm]
base_url = http://127.0.0.1:1/v1
api_key = ${{CONTEST_GATE_API_KEY}}
model = test/gate
api_format = openai
response_format = true

[contest_openrouter_llm]
base_url = http://127.0.0.1:9/v1
api_key = test-openrouter-key
model =
api_format = openai
response_format = false

[contest.agent.alpha]
model = kenary/hy3:free
"""


def _write_openrouter_roster(sb) -> None:
    """The sandbox's roster, with the profile an openrouter round reads.

    `backend = kilo` in the file and `kilo_bin` pointed at nothing: the flag is
    what makes the round openrouter, and the round must not need a kilo binary
    or a server to be valid.
    """
    (sb.repo / "contest.ini").write_text(
        OPENROUTER_ROSTER.format(rounds=sb.rounds), encoding="utf-8")


def _args(*argv: str) -> argparse.Namespace:
    """`cli._parser().parse_args` for the `run` command — the flag surface."""
    return cli._parser().parse_args(["run", "--ticket", "1", *argv])


def test_the_parser_refuses_a_backend_it_does_not_know(capsys):
    with pytest.raises(SystemExit) as raised:
        _args("--backend", "ollama")
    assert raised.value.code == 2


def test_models_under_an_openrouter_backend_default_to_the_gateway_provider(sandbox):
    """A bare id has no Kilo to route through, so the default provider there is
    the gateway, not `kenary` — and the variant names come along with it."""
    config = cli._apply_flags(sandbox.config(backend="openrouter"),
                              _args("--models", "hy3:free,hy3:free"))

    assert [(spec.name, spec.provider_id) for spec in config.agents] == [
        ("hy3-var1", "openrouter"), ("hy3-var2", "openrouter")]


def test_models_under_a_kilo_backend_default_to_kenary(sandbox):
    config = cli._apply_flags(sandbox.config(), _args("--models", "hy3:free,hy3:free"))

    assert [(spec.name, spec.provider_id) for spec in config.agents] == [
        ("hy3-var1", "kenary"), ("hy3-var2", "kenary")]


def test_the_provider_flag_wins_over_the_backend_default(sandbox):
    (spec,) = cli._apply_flags(sandbox.config(backend="openrouter"),
                               _args("--provider", "kenary", "--models", "hy3:free")).agents
    assert spec.provider_id == "kenary"


def test_a_models_id_naming_its_own_provider_wins_over_the_backend_default(sandbox):
    (spec,) = cli._apply_flags(sandbox.config(backend="openrouter"),
                               _args("--models", "kenary/hy3:free")).agents
    assert spec.provider_id == "kenary"


def test_backend_flag_builds_an_openrouter_backend_without_a_kilo_server(
        sandbox, capsys, monkeypatch):
    """`--backend openrouter --models agnes-2-5-flash:free` is a valid
    invocation: intake passes with no kilo binary to find and no offer to check,
    the roster's own profile is resolved on demand (the file declares
    `backend = kilo`, so `load_roster` never read it), and the round builds one
    `OpenRouterBackend` in the agent's worktree."""
    _write_openrouter_roster(sandbox)
    built: list = []
    backends: list = []

    def make_backend_record(api_key, base_url, directory, **kwargs):
        built.append((api_key, base_url, directory, kwargs))
        return type("Backend", (), {})()

    def fake_run_round(config, ticket, ticket_path, workspaces, *, make_backend,
                       out_dir, resume=None, run_tests=True, server_pid=None):
        backends.extend(make_backend(workspace) for workspace in workspaces)
        specs = {spec.name: spec for spec in config.agents}
        return RoundState(
            round_no=ROUND, ticket=ticket, base_sha=sandbox.base, started_at=time.time(),
            agents=[AgentRun(agent=specs.get(ws.agent) or AgentSpec(ws.agent, "", ""),
                             workspace=ws, state=AgentState.READY) for ws in workspaces])

    def never_start_server(*args, **kwargs):
        raise AssertionError("backend = openrouter must not start a Kilo server")

    monkeypatch.setattr(cli, "OpenRouterBackend", make_backend_record)
    monkeypatch.setattr(cli, "run_round", fake_run_round)
    monkeypatch.setattr(cli, "_start_server", never_start_server)

    code = cli.main(["run", "--ticket", "1", "--backend", "openrouter",
                     "--models", "agnes-2-5-flash:free", "--max-parallel", "1",
                     "--no-gate", "--no-tests"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "intake:" not in captured.err
    assert "server:" not in captured.err
    # KC-62: no server, so no neighbour check — the `kilo:` line is a kilo thing
    assert "kilo:" not in captured.err
    assert _plan(captured.out)["agents"] == "1: openrouter/agnes-2-5-flash:free"
    assert len(backends) == 1

    (api_key, base_url, directory, kwargs) = built[0]
    assert (api_key, base_url) == ("test-openrouter-key", "http://127.0.0.1:9/v1")
    # KC-65: the OpenRouter agents get the same env as the ones a `kilo serve`
    # hosts, or the sizing rule would be a Kilo-only rule.
    assert set(kwargs) == {"extra_env"}
    assert set(kwargs["extra_env"]) == KC65_ENV_KEYS
    assert Path(kwargs["extra_env"]["CONTEST_PYTEST_WORKERS_FILE"]).name == "pytest-workers"
    assert directory.rstrip("/").endswith(f"01-agnes-2-5-flash")
    assert [row["name"] for row in _table(captured.out)] == ["agnes-2-5-flash"]


class _ServedServer:
    """The two facts intake needs of a `kilo serve` that never really started."""

    pid = 4321

    def close(self):
        pass


def _neighbour_proc(root: Path, pid: int = 4321, *, neighbours: int = 5,
                    home: str = "/home/op") -> Path:
    """A fake `/proc`: the round's server plus *neighbours* sibling `kilo run`s
    on the same store, so the scan has something to count. The store is read off
    the server's own `environ`, never off a constant in the test."""
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    argv = ("/bin/kilo", "serve", "--port", "8123")
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (entry / "environ").write_bytes(f"HOME={home}\0".encode())
    (entry / "stat").write_text(f"{pid} (kilo) S 1\n", encoding="utf-8")
    for i in range(neighbours):
        other = 5000 + i
        other_dir = root / str(other)
        other_dir.mkdir(parents=True, exist_ok=True)
        argv = ("/bin/kilo", "run", "-m", "kenary/hy3:free")
        (other_dir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (other_dir / "environ").write_bytes(f"HOME={home}\0".encode())
        (other_dir / "stat").write_text(f"{other} (kilo) S 1\n", encoding="utf-8")
    return root


def _kc62_round(base_sha):
    """`run_round`'s stand-in: one READY agent per worktree, nothing to export."""

    def fake_round(config, ticket, ticket_path, workspaces, **kw):
        return RoundState(
            round_no=ROUND, ticket=ticket, base_sha=base_sha, started_at=time.time(),
            agents=[AgentRun(agent=spec, workspace=ws, state=AgentState.READY)
                    for spec, ws in zip(config.agents, workspaces)])

    return fake_round


def test_intake_prints_the_neighbour_line_above_the_threshold(
        sandbox, capsys, monkeypatch, spawn_holder):
    """KC-62 acceptance: five Kilo processes on the round's store print one line
    to stderr at intake, with the store read off the server's environment; three
    print nothing. A warning, not a refusal — either way the round runs."""
    five = _neighbour_proc(sandbox.tmp / "proc5")
    monkeypatch.setattr(cli, "_start_server", lambda *a, **kw: _ServedServer())
    monkeypatch.setattr(cli, "_PROC_ROOT", str(five))
    monkeypatch.setattr(cli, "run_round", _kc62_round(sandbox.base))
    monkeypatch.setattr(cli, "export_patches", lambda *a, **kw: [])

    code = cli.main(["run", "--ticket", "1", "--max-parallel", "1",
                     "--no-gate", "--no-tests"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "intake:" not in captured.err
    assert "server:" not in captured.err
    line = next((l for l in captured.err.splitlines() if l.startswith("kilo:")), "")
    assert line == "kilo: 5 other Kilo processes share /home/op/.local/share/kilo " \
                   "— database errors likely", captured.err


def test_a_proc_the_scan_cannot_read_still_starts_the_round(
        sandbox, capsys, monkeypatch, spawn_holder):
    """KC-62 acceptance: intake scans for the neighbours that share the server's
    store, and a box it cannot read gives no line — the round runs anyway, no
    `intake:` error, no raised OSError. The store check is a notice, not a gate."""
    monkeypatch.setattr(cli, "_start_server", lambda *a, **kw: _ServedServer())
    monkeypatch.setattr(cli, "_PROC_ROOT", str(sandbox.tmp / "no-such-proc"))

    def fake_round(config, ticket, ticket_path, workspaces, **kw):
        return RoundState(
            round_no=ROUND, ticket=ticket, base_sha=sandbox.base,
            started_at=time.time(),
            agents=[AgentRun(agent=spec, workspace=ws, state=AgentState.READY)
                    for spec, ws in zip(config.agents, workspaces)])

    monkeypatch.setattr(cli, "run_round", fake_round)
    monkeypatch.setattr(cli, "export_patches", lambda *a, **kw: [])

    code = cli.main(["run", "--ticket", "1", "--max-parallel", "1",
                     "--no-gate", "--no-tests"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "intake:" not in captured.err
    assert "kilo:" not in captured.err


def test_backend_flag_without_a_profile_in_the_roster_is_a_server_line(
        sandbox, capsys, spawn_holder):
    """The roster names no `openrouter_llm_profile`: the agents have no
    credential of their own, so the roster fails to load and the round never
    starts — exit 1, no worktree, no server."""
    code = cli.main(["run", "--ticket", "1", "--backend", "openrouter",
                     "--models", "agnes-2-5-flash:free", "--max-parallel", "1",
                     "--no-gate", "--no-tests"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_FAILED
    assert captured.err.startswith("intake: [contest] backend = openrouter")
    assert "openrouter_llm_profile" in captured.err
    assert list(sandbox.rounds.iterdir()) == [], "no worktree before the roster is valid"


# ─────────────────────────────────────────────────────────────────────────────
# KC-49 — the reasoning variant, named or `highest`, through `run`
# ─────────────────────────────────────────────────────────────────────────────

KC49_REJECTED = {"name": "APIError",
                 "data": {"message": "the model's provider rejected the request. check "
                                     "the model id, request fields, and context length",
                          "statusCode": 400, "isRetryable": False}}

KC49_OFFER = _offer(({
    "id": "kenary", "name": "kenari", "source": "static",
    "models": {"glm-4-7-flash:free": {
        "id": "glm-4-7-flash:free", "providerID": "kenary", "name": "glm-4-7-flash:free",
        "status": "active", "capabilities": {"reasoning": True, "toolcall": True},
        "variants": {name: {"reasoningEffort": name}
                     for name in ("none", "low", "medium", "high", "xhigh", "max")}}},
},))


def _work_unless_probe(directory, text):
    """The round's agent does the work; the intake's `say: hello` only answers."""
    if text.strip() == "say: hello":
        return
    work_ready(directory, text)


KC49_SCENARIO = {"providers": KC49_OFFER,
                 "reject_variants": {"max": KC49_REJECTED, "xhigh": KC49_REJECTED},
                 "turns": [{"on_prompt": _work_unless_probe, "events": ["busy", "idle"],
                            "assistant": "hello"}]}


def test_run_resolves_highest_to_the_top_variant_that_answers(sandbox, capsys, spawn_holder):
    """`@highest` on glm-4-7-flash:free: `max` and `xhigh` are rejected by the
    provider, `high` answers — the round runs at `high`, and says so."""
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free@highest",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert ("variant: kenary/glm-4-7-flash:free@highest → high (failed: max: "
            "the model's provider rejected the request.") in captured.out
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free@high"
    sessions = _sessions(fake)
    assert [s["body"]["model"].get("variant") for s in sessions] == ["max", "xhigh", "high",
                                                                     "high"]
    round_session = sessions[-1]
    assert round_session["body"]["title"] == f"contest/{ROUND:02d}/glm-4-7-flash"
    round_prompts = [r for r in fake.calls("POST")
                     if r["path"] == f"/session/{fake.sessions()[-1].id}/prompt_async"]
    assert round_prompts and all(r["body"]["variant"] == "high" for r in round_prompts)
    assert len(fake.calls(method="DELETE")) == 3, "every probe session is deleted"


def test_run_with_no_variant_flag_runs_at_highest(sandbox, capsys, spawn_holder):
    """`highest` is the default: the same glm case with nothing on the command line."""
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "variant: kenary/glm-4-7-flash:free@highest → high" in captured.out
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free@high"
    assert [s["body"]["model"].get("variant") for s in _sessions(fake)] == [
        "max", "xhigh", "high", "high"]


# KC-11: the probe cache, read and written next to the roster by `run`

def _kc11_cache(sandbox, variant, age_days=1.0, usable=True):
    """A `contest-probe.json` next to the sandbox roster with one glm entry."""
    path = sandbox.repo / "contest-probe.json"
    path.write_text(json.dumps({"kenary/glm-4-7-flash:free": {
        "variant": variant, "usable": usable,
        "probed_at": time.time() - age_days * 86400, "kilo_version": "",
        "tried": [["max", "rejected"], ["xhigh", "rejected"]], "reasoning_tokens": 0}}))
    return path


def test_run_with_a_fresh_probe_cache_opens_no_probe_session(sandbox, capsys, spawn_holder):
    _kc11_cache(sandbox, "high")
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free@high"
    assert [s["body"]["model"].get("variant") for s in _sessions(fake)] == ["high"]


def test_run_reprobes_a_cached_variant_the_model_no_longer_lists(sandbox, capsys,
                                                                 spawn_holder):
    """A fresh entry naming a variant `GET /provider` does not list would fail
    every rung and refuse the round: it is stale, and the live ladder runs."""
    path = _kc11_cache(sandbox, "turbo")
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert [s["body"]["model"].get("variant") for s in _sessions(fake)] == [
        "max", "xhigh", "high", "high"]
    assert json.loads(path.read_text())["kenary/glm-4-7-flash:free"]["variant"] == "high"


def test_run_with_probe_ttl_days_zero_always_reprobes(sandbox, capsys, spawn_holder):
    """`probe_ttl_days = 0` is "always re-probe", not the 7-day default."""
    roster = sandbox.repo / "contest.ini"
    roster.write_text(roster.read_text().replace(
        "out_dir = contest-out", "out_dir = contest-out\nprobe_ttl_days = 0", 1))
    _kc11_cache(sandbox, "high", age_days=0.01)
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert [s["body"]["model"].get("variant") for s in _sessions(fake)] == [
        "max", "xhigh", "high", "high"]


def test_allow_unprobed_starts_an_agent_no_rung_answered_for(sandbox, capsys, spawn_holder):
    """Every rung, the plain request included, is refused: without the flag the
    round is refused; with it the agent starts with no variant and a note."""
    rejected = {name: KC49_REJECTED
                for name in (None, "none", "low", "medium", "high", "xhigh", "max")}
    scenario = dict(KC49_SCENARIO, reject_variants=rejected)
    argv = ["--ticket", "1", "--models", "glm-4-7-flash:free", "--no-gate", "--no-tests"]

    code, fake = run_fake(sandbox, scenario, argv, spawn_holder)
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    assert "no variant answered 'say: hello'" in captured.err

    code, fake = run_fake(sandbox, scenario, [*argv, "--allow-unprobed"], spawn_holder)
    captured = capsys.readouterr()
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert "started with no variant (--allow-unprobed)" in captured.out
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free"
    assert "variant" not in _sessions(fake)[-1]["body"]["model"]


def test_run_variant_default_sends_no_variant_and_probes_nothing(sandbox, capsys,
                                                                  spawn_holder):
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--variant", "default", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "variant:" not in captured.out
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free"
    (session,) = _sessions(fake)
    assert "variant" not in session["body"]["model"]


def test_run_refuses_a_variant_the_model_does_not_list(sandbox, capsys, spawn_holder):
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free@turbo",
                           "--no-gate", "--no-tests"], spawn_holder)
    lines = [line for line in capsys.readouterr().err.splitlines()
             if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: [glm-4-7-flash] kenary/glm-4-7-flash:free: no variant 'turbo' "
                     "— listed: none, low, medium, high, xhigh, max"]
    assert _sessions(fake) == [], "no session, probe or round, before intake passes"
    assert not sandbox.out().exists()


def test_run_variant_flag_probes_the_named_variant_once_and_sends_it(sandbox, capsys,
                                                                    spawn_holder):
    """KC-61: a named variant is asked about once at intake, then the round runs
    at it — so a dry key is seen before the round, not fifteen minutes into it."""
    code, fake = run_fake(sandbox, KC49_SCENARIO,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--variant", "medium", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert "variant:" not in captured.out
    assert "provider_quota" not in captured.err
    assert _plan(captured.out)["agents"] == "1: kenary/glm-4-7-flash:free@medium"
    sessions = _sessions(fake)
    assert [s["body"]["model"].get("variant") for s in sessions] == ["medium", "medium"]
    assert sessions[0]["body"]["title"] == "variant-probe/glm-4-7-flash:free/medium"
    round_session = sessions[-1]
    assert round_session["body"]["title"] == f"contest/{ROUND:02d}/glm-4-7-flash"
    assert len(fake.calls(method="DELETE")) == 1, "the probe session is deleted"


def test_run_variant_flag_answers_a_quota_at_intake_and_leaves_the_agent_out(sandbox, capsys,
                                                                       spawn_holder):
    """Acceptance 5: the intake's own probe answers the free-model daily limit —
    one console line naming the agent and the reset, and the agent is left out.
    It was the roster's only one, so nothing is left to start: no round, exit
    failed. (With others on the roster they run one short — resolve_variants.)"""
    scenario = {"providers": KC49_OFFER,
                "turns": [{"error": {"name": "APIError",
                                     "data": {"message": "Rate limit exceeded: "
                                                        "free-models-per-day",
                                              "statusCode": 429}}}]}
    _quota_phrases(sandbox, "free-models-per-day")

    code, fake = run_fake(sandbox, scenario,
                          ["--ticket", "1", "--models", "glm-4-7-flash:free",
                           "--variant", "medium", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    skipped = [line for line in captured.out.splitlines() if "provider_quota" in line]

    assert code == cli.EXIT_FAILED
    assert len(skipped) == 1, captured.out
    assert skipped[0].startswith("variant: [glm-4-7-flash] kenary/glm-4-7-flash:free: "
                                 "provider_quota — ")
    assert "free-models-per-day" in skipped[0] and skipped[0].endswith("not started")
    assert lines == ["intake: every agent is out of quota — nothing to start "
                     "(the variant: lines above name each one)"]
    # the only session is the intake's own probe, and it was deleted: the agent
    # never got a round of its own
    (session,) = _sessions(fake)
    assert session["body"]["title"] == "variant-probe/glm-4-7-flash:free/medium"
    assert len(fake.calls(method="DELETE")) == 1


# ─────────────────────────────────────────────────────────────────────────────
# KC-55: the gate shares an endpoint with the roster, and intake asks it once
# ─────────────────────────────────────────────────────────────────────────────

GATE_HOST = "kenari.id"


def _offer_with_base_url(base_url):
    """The sandbox's two agents on offer, under a provider whose `options.baseURL`
    the gate can share."""
    return _offer((dict(_provider("kenary", "kenari", ("agent-a:free", "agent-b:free")),
                        options={"baseURL": base_url}),))


KC55_OFFER = _offer_with_base_url(f"https://{GATE_HOST}/v1")

KC55_SCENARIO = {"providers": KC55_OFFER,
                 "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}


def _aim_the_gate(sandbox, base_url, model):
    """The sandbox's roster with its gate pointed at *base_url* / *model*."""
    ini = sandbox.repo / "contest.ini"
    ini.write_text(ini.read_text(encoding="utf-8")
                   .replace("base_url = http://127.0.0.1:1/v1", f"base_url = {base_url}")
                   .replace("model = test/gate", f"model = {model}"), encoding="utf-8")


def _quota_phrases(sandbox, phrases):
    """The sandbox's roster with `quota_patterns` set — the committed file's key is
    not in it, so a quota would be invisible at intake without this."""
    ini = sandbox.repo / "contest.ini"
    ini.write_text(ini.read_text(encoding="utf-8")
                   .replace("idle_event_timeout_sec = 900",
                            f"idle_event_timeout_sec = 900\nquota_patterns = {phrases}"),
                   encoding="utf-8")


def _gate_allows(*args, **kwargs):
    return json.dumps({"verdict": "allow", "reason": "scratch"})


def _gate_raises(text):
    def raise_it(*args, **kwargs):
        raise RuntimeError(text)

    return raise_it


def test_the_gate_sharing_an_endpoint_with_the_roster_warns_and_runs(
        sandbox, capsys, spawn_holder, monkeypatch):
    """Round 66: a different model, the same key and endpoint as two of the
    agents. One `gate:` line on stdout, and the round still starts."""
    _aim_the_gate(sandbox, f"https://{GATE_HOST}/v1", "hy3:free")
    monkeypatch.setattr("tools.contest.policy.request_completion", _gate_allows)

    code, fake = run_fake(sandbox, KC55_SCENARIO, ["--ticket", "1", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    shares = [line for line in captured.out.splitlines() if line.startswith("gate:")]
    assert shares == [
        f"gate: shares {GATE_HOST} with 2 agents (agent-a, agent-b) — a rate limit there "
        "hits the gate too; point [contest_gate_llm] in contest.local.ini at another endpoint"]
    assert _plan(captured.out)["gate"] == f"hy3:free @ {GATE_HOST}"
    assert len(_sessions(fake)) == 2


def test_a_gate_on_a_host_no_agent_uses_says_nothing(sandbox, capsys, spawn_holder, monkeypatch):
    _aim_the_gate(sandbox, "https://another.host/v1", "hy3:free")
    monkeypatch.setattr("tools.contest.policy.request_completion", _gate_allows)
    scenario = dict(KC55_SCENARIO,
                    providers=_offer((_provider("kenary", "kenari",
                                                ("agent-a:free", "agent-b:free")),)))

    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-tests"], spawn_holder)

    assert code == 0
    out = capsys.readouterr().out
    assert not [line for line in out.splitlines() if line.startswith("gate:")]


def test_the_plan_label_is_the_model_and_the_host(sandbox):
    """`hy3:free @ kenari.id` — the model id and the host, no key, no path."""
    config = replace(sandbox.config(), gate_settings=replace(
        sandbox.config().gate_settings, base_url=f"https://{GATE_HOST}/v1", model="hy3:free"))

    assert cli._gate_plan_label(config) == f"hy3:free @ {GATE_HOST}"
    assert cli._gate_plan_label(replace(config, gate_settings=None)) == "off"
    assert cli._gate_plan_label(replace(config, gate_settings=replace(
        config.gate_settings, base_url=""))) == "hy3:free"


def test_an_intake_probe_that_refuses_the_key_refuses_the_round(
        sandbox, capsys, spawn_holder, monkeypatch):
    """A dead key used to surface as 48 `gate-failed` rejects; it is a line at
    intake now, before a worktree or a session exists."""
    _aim_the_gate(sandbox, f"https://{GATE_HOST}/v1", "hy3:free")
    monkeypatch.setattr("tools.contest.policy.request_completion", _gate_raises(
        f"HTTP 401 from https://{GATE_HOST}/v1/chat/completions: Unauthorized"))

    code, fake = run_fake(sandbox, KC55_SCENARIO, ["--ticket", "1", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()

    assert code == cli.EXIT_FAILED
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert lines == ["intake: gate [contest_gate_llm] refused its key (HTTP 401) — set api_key "
                     "in contest.local.ini"]
    assert _sessions(fake) == [], "no session may exist before intake passes"
    assert not sandbox.out().exists()


def test_an_intake_probe_that_hits_a_rate_limit_warns_and_runs(
        sandbox, capsys, spawn_holder, monkeypatch):
    """A 429 is transient — the very thing the real ask retries — so it warns."""
    _aim_the_gate(sandbox, f"https://{GATE_HOST}/v1", "hy3:free")
    monkeypatch.setattr("tools.contest.policy.request_completion", _gate_raises(
        f"HTTP 429 from https://{GATE_HOST}/v1/chat/completions: Too Many Requests"))

    code, fake = run_fake(sandbox, KC55_SCENARIO, ["--ticket", "1", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    warnings = [line for line in captured.out.splitlines() if line.startswith("gate:")]
    assert warnings == [f"gate: shares {GATE_HOST} with 2 agents (agent-a, agent-b) — a rate "
                        "limit there hits the gate too; point [contest_gate_llm] in "
                        "contest.local.ini at another endpoint",
                        "gate: HTTP 429 on the intake probe — the round starts anyway; "
                        "the gate retries that itself"]
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]


def test_no_gate_probes_nothing_and_shares_nothing(sandbox, capsys, spawn_holder, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the gate must not be called with --no-gate")

    _aim_the_gate(sandbox, f"https://{GATE_HOST}/v1", "hy3:free")
    monkeypatch.setattr("tools.contest.policy.request_completion", refuse)

    code, fake = run_fake(sandbox, KC55_SCENARIO,
                          ["--ticket", "1", "--no-tests", "--no-gate"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert calls == []
    assert not [line for line in captured.out.splitlines() if line.startswith("gate:")]
    assert _plan(captured.out)["gate"] == "off"


def test_a_silence_clock_shorter_than_the_gate_worst_case_refuses_the_round(
        sandbox, capsys, spawn_holder, monkeypatch):
    """The gate's worst case is gate_deadline_sec + GATE_TIMEOUT = 660 s. While it
    waits the session is silent, so a 300 s clock would stall the agent the gate
    is asking for — refuse it here instead."""
    ini = sandbox.repo / "contest.ini"
    ini.write_text(ini.read_text(encoding="utf-8")
                   .replace("idle_event_timeout_sec = 900", "idle_event_timeout_sec = 300"),
                   encoding="utf-8")
    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("a refused round must not spend a model call")

    monkeypatch.setattr("tools.contest.policy.request_completion", refuse)

    code, fake = run_fake(sandbox, KC55_SCENARIO, ["--ticket", "1", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()

    assert code == cli.EXIT_FAILED
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert lines == ["intake: gate retries can outlast the silence clock: worst case 660 s ≥ "
                     "idle_event_timeout_sec 300 — lower gate_deadline_sec in contest.ini, "
                     "or raise idle_event_timeout_sec"]
    assert calls == [], "a refused round spends no model call"
    assert _sessions(fake) == []
    assert not sandbox.out().exists()


# ─────────────────────────────────────────────────────────────────────────────
# KC-37 §2: the gate is never a model the round is competing
# ─────────────────────────────────────────────────────────────────────────────

KC37_SCENARIO = {
    "providers": _offer((_provider("kenary", "kenari", ("hy3:free", "x:free", "y:free")),
                         _provider("kilo", "kilo", ("nex-agi/nex-n2.5-pro:free",))),
                        connected=("kenary", "kilo")),
    "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}],
}


def _kc37_round(sandbox, capsys, spawn_holder, monkeypatch, gate, models, *extra):
    """The round with the gate on *gate*: `(code, fake, captured, calls)`, where
    *calls* counts every model call the gate made, the intake probe included."""
    _aim_the_gate(sandbox, "https://another.host/v1", gate)
    calls = []

    def completion(*args, **kwargs):
        calls.append(args)
        return _gate_allows(*args, **kwargs)

    monkeypatch.setattr("tools.contest.policy.request_completion", completion)
    code, fake = run_fake(sandbox, KC37_SCENARIO,
                          ["--ticket", "1", "--models", models, "--no-tests", *extra],
                          spawn_holder)
    return code, fake, capsys.readouterr(), calls


def _intake_lines(err):
    return [line for line in err.splitlines() if line.startswith("intake:")]


def test_a_gate_model_the_roster_runs_refuses_the_round(sandbox, capsys, spawn_holder,
                                                         monkeypatch):
    """Round 64: the gate was `hy3:free` and `hy3` an agent. Refused at intake,
    before a session and before any model call, the gate probe included."""
    code, fake, captured, calls = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                              "hy3:free", "hy3:free,x:free")

    assert code == cli.EXIT_FAILED
    assert _intake_lines(captured.err) == [
        "intake: gate model hy3:free is also agent hy3 in this round",
        "intake:   the gate must be a second, independent model: set [contest_gate_llm] model",
        "intake:   in contest.local.ini to something the roster does not run, "
        "or pass --no-gate",
    ]
    assert calls == []
    assert _sessions(fake) == []
    assert not sandbox.out().exists()


def test_no_gate_skips_the_gate_model_check(sandbox, capsys, spawn_holder, monkeypatch):
    code, fake, captured, _ = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                          "hy3:free", "hy3:free,x:free", "--no-gate")

    assert code == 0, captured.err
    assert _intake_lines(captured.err) == []
    assert _plan(captured.out)["gate"] == "off"
    assert len(_sessions(fake)) == 2


def test_a_gate_model_the_roster_does_not_run_starts_the_round(sandbox, capsys, spawn_holder,
                                                               monkeypatch):
    code, fake, captured, _ = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                          "hy3:free", "x:free,y:free")

    assert code == 0, captured.err
    assert _intake_lines(captured.err) == []
    assert _plan(captured.out)["gate"].startswith("hy3:free")
    assert len(_sessions(fake)) == 2


def test_the_refusal_names_every_agent_on_the_gate_model(sandbox, capsys, spawn_holder,
                                                         monkeypatch):
    """KC-34 §7: two agents on one model, both named."""
    code, fake, captured, _ = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                          "hy3:free", "hy3:free,hy3:free")

    assert code == cli.EXIT_FAILED
    assert _intake_lines(captured.err)[0] == (
        "intake: gate model hy3:free is also agent hy3-var1 (and hy3-var2) in this round")
    assert _sessions(fake) == []


def test_a_gate_on_another_provider_does_not_collide(sandbox, capsys, spawn_holder,
                                                     monkeypatch):
    """`openrouter/hy3:free` and `kenary/hy3:free` are two models."""
    code, fake, captured, _ = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                          "openrouter/hy3:free", "kenary/hy3:free")

    assert code == 0, captured.err
    assert _intake_lines(captured.err) == []
    assert len(_sessions(fake)) == 1


@pytest.mark.parametrize("gate, models, agent", [
    # the gate spelled with the agent's own provider
    ("kenary/hy3:free", "kenary/hy3:free,x:free", "hy3"),
    # a model id with a `/` of its own: the gate's endpoint calls it that
    ("nex-agi/nex-n2.5-pro:free", "kilo/nex-agi/nex-n2.5-pro:free,x:free", "nex-n2-5-pro"),
])
def test_a_gate_model_with_a_slash_still_collides(sandbox, capsys, spawn_holder, monkeypatch,
                                                  gate, models, agent):
    code, fake, captured, calls = _kc37_round(sandbox, capsys, spawn_holder, monkeypatch,
                                              gate, models)

    assert code == cli.EXIT_FAILED
    assert _intake_lines(captured.err)[0] == (
        f"intake: gate model {gate} is also agent {agent} in this round")
    assert calls == []
    assert _sessions(fake) == []


def test_the_placeholder_gate_model_is_not_checked(sandbox):
    """No gate, or the committed placeholder: nothing to refuse."""
    config = replace(sandbox.config(),
                     agents=cli.agents_from_models("some/model,hy3:free"))
    gate = config.gate_settings
    assert cli.gate_model_refusals(replace(config, gate_settings=None), config.agents) == []
    assert cli.gate_model_refusals(
        replace(config, gate_settings=replace(gate, model=cli.GATE_PLACEHOLDER_MODEL)),
        config.agents) == []


# ─────────────────────────────────────────────────────────────────────────────
# KC-35: a roster model missing from Kilo's own model list
# ─────────────────────────────────────────────────────────────────────────────

#: The offer the round-64 logs were run against: `hy3:free` and the two
#: `agnes-2-x` ids are listed, `agnes-3-0-flash:free` and `nex-n2-5-pro:free`
#: are not, so the two agents that ended `ERROR` ten seconds in are missing.
KC35_OFFER = _offer((_provider("kenary", "kenari",
                               ("hy3:free", "agnes-2-5-flash:free",
                                "agnes-2-0-flash:free")),))

#: The same offer with the two ids added — what `kilo models` shows once
#: `KILO_CONFIG_CONTENT` registers them (the ticket's live verification).
KC35_REGISTERED_OFFER = _offer((_provider("kenary", "kenari",
                                          ("hy3:free", "agnes-2-5-flash:free",
                                           "agnes-2-0-flash:free",
                                           "agnes-3-0-flash:free",
                                           "nex-n2-5-pro:free")),))

KC35_SCENARIO = {"providers": KC35_OFFER,
                 "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
KC35_REGISTERED_SCENARIO = {"providers": KC35_REGISTERED_OFFER,
                            "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}

#: The `KILO_CONFIG_CONTENT` body registration writes for the ids named, under
#: the `kenary` provider: `name` is the id, `reasoning` is on.
def _kc35_overlay(*model_ids):
    return {"provider": {"kenary": {"models": {
        model_id: {"name": model_id, "reasoning": True} for model_id in model_ids}}}}


KC35_ENTRY = _kc35_overlay("agnes-3-0-flash:free")
KC35_TWO_ENTRIES = _kc35_overlay("agnes-3-0-flash:free", "nex-n2-5-pro:free")


def test_roster_missing_names_only_a_known_connected_provider_missing_the_model():
    """Unknown provider, no credentials, on offer — none of those are missing;
    registering a model cannot create a provider or supply a key."""
    offer = _offer((_provider("kenary", "kenari", ("hy3:free", "agnes-2-5-flash:free")),
                    _provider("sensenova123", "sensenova", ("sensenova-6.8-flash-lite",))),
                   connected=("kenary", "sensenova123"))
    agents = (cli.agents_from_models("agnes-3-0-flash:free,nex-n2-5-pro:free")
              + cli.agents_from_models("hy3:free")
              + cli.agents_from_models("sensenova123/sensenova-6.8-flash-lite")
              + cli.agents_from_models("kenari/hy3:free")
              + cli.agents_from_models("mystery/gpt-4o"))

    assert [agent.model for agent in cli.roster_missing(offer, agents)] == [
        "kenary/agnes-3-0-flash:free", "kenary/nex-n2-5-pro:free"]


def test_roster_missing_is_empty_for_a_roster_entirely_on_offer():
    assert cli.roster_missing(KC25_OFFER,
                              cli.agents_from_models("hy3:free,kenary/agnes-2-0-flash:free")) == []


def test_roster_missing_ignores_a_provider_without_credentials():
    offer = _offer((_provider("kenary", "kenari", ("hy3:free",)),), connected=())
    assert cli.roster_missing(offer, cli.agents_from_models("agnes-3-0-flash:free")) == []


def test_registration_overlay_groups_the_models_by_provider_in_roster_order():
    agents = (cli.agents_from_models("agnes-3-0-flash:free,nex-n2-5-pro:free")
              + cli.agents_from_models("sensenova123/sensenova-6.8-flash-lite"))

    assert cli.registration_overlay(agents) == {
        "provider": {
            "kenary": {"models": {
                "agnes-3-0-flash:free": {"name": "agnes-3-0-flash:free", "reasoning": True},
                "nex-n2-5-pro:free": {"name": "nex-n2-5-pro:free", "reasoning": True}}},
            "sensenova123": {"models": {
                "sensenova-6.8-flash-lite": {"name": "sensenova-6.8-flash-lite",
                                             "reasoning": True}}},
        }}
    # roster order, so the ids read back the way the roster did
    assert list(cli.registration_overlay(agents)["provider"]["kenary"]["models"]) == [
        "agnes-3-0-flash:free", "nex-n2-5-pro:free"]
    assert cli.registration_overlay(()) == {}


def test_merge_config_content_gives_the_overlay_when_nothing_is_set():
    assert json.loads(cli.merge_config_content(None, KC35_ENTRY)) == KC35_ENTRY
    assert json.loads(cli.merge_config_content("", KC35_ENTRY)) == KC35_ENTRY


def test_merge_config_content_keeps_the_operator_s_own_models_and_options():
    existing = json.dumps({
        "options": {"baseURL": "https://kenari.id/v1"},
        "provider": {"kenary": {"apiKey": "sk-operator",
                                "models": {"hy3:free": {"name": "hy3:free",
                                                         "reasoning": True}}}}})
    merged = json.loads(cli.merge_config_content(existing, KC35_ENTRY))

    assert merged["options"]["baseURL"] == "https://kenari.id/v1"
    assert merged["provider"]["kenary"]["apiKey"] == "sk-operator"
    assert merged["provider"]["kenary"]["models"]["hy3:free"] == {"name": "hy3:free",
                                                                  "reasoning": True}
    assert merged["provider"]["kenary"]["models"]["agnes-3-0-flash:free"] == {
        "name": "agnes-3-0-flash:free", "reasoning": True}
    # neither input is rewritten: the operator's string is what it was
    assert json.loads(existing)["provider"]["kenary"]["models"] == {"hy3:free":
        {"name": "hy3:free", "reasoning": True}}


@pytest.mark.parametrize("bad", ('[1, 2]', '"a string"', "123", "not json", "true"))
def test_merge_config_content_refuses_a_value_that_is_not_an_object(bad):
    with pytest.raises(ValueError, match="KILO_CONFIG_CONTENT is not a JSON object"):
        cli.merge_config_content(bad, KC35_ENTRY)


def _content_spawn_holder(monkeypatch):
    """`KiloServer.spawn` → the next fake in line, each call's `env` recorded.

    The order is the point of KC-35: the first throwaway is the operator's own
    server, the second gets `KILO_CONFIG_CONTENT`, and the round's own server is
    the last call. `fakes` is appended to by the test, in that order; one entry
    stands in for every later call.
    """
    calls: list = []
    fakes: list = []
    # no operator overlay unless a test says so: the flag's own merge starts
    # from nothing here
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)

    def spawn_server(binary, *, log_path, **kwargs):
        calls.append(dict(kwargs.get("env") or {}))
        return KiloServer.attach(fakes[min(len(calls) - 1, len(fakes) - 1)].url)

    monkeypatch.setattr(cli.KiloServer, "spawn", staticmethod(spawn_server))
    return calls, fakes


def _register_missing_flags(*models):
    """The `run` flags for `--register-missing` on the model ids named."""
    return ["--ticket", "1", "--models", ",".join(models), "--register-missing",
            "--no-gate", "--no-tests"]


def test_run_prints_the_kc25_line_and_one_hint_for_a_missing_model(sandbox, capsys, spawn_holder):
    """The id Kilo refuses from its own list is named with the entry to add —
    before a worktree, an output directory or a session exists."""
    code, fake = run_fake(sandbox, KC35_SCENARIO,
                          ["--ticket", "1", "--models", "hy3:free,agnes-3-0-flash:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == [
        "intake: [agnes-3-0-flash] kenary/agnes-3-0-flash:free: no model "
        "'agnes-3-0-flash:free' under 'kenary' — on offer: agnes-2-0-flash:free, "
        "agnes-2-5-flash:free, hy3:free (did you mean agnes-2-0-flash:free?)",
        "intake: hint: 1 id(s) are not in Kilo's model list for provider 'kenary'. "
        '"did you mean" compares spelling only — if the provider serves the id, add it '
        "under provider.kenary.models in kilo.jsonc, or re-run with --register-missing.",
    ]
    assert not (sandbox.rounds / f"{ROUND:02d}-agnes-3-0-flash").exists()
    assert not sandbox.out().exists()
    assert _sessions(fake) == [], "no session may be created before intake passes"


def test_run_prints_one_hint_for_both_ids_of_a_provider(sandbox, capsys, spawn_holder):
    """Two ids of one provider are two failures and one hint, naming the count."""
    code, fake = run_fake(sandbox, KC35_SCENARIO,
                          ["--ticket", "1", "--models",
                           "agnes-3-0-flash:free,nex-n2-5-pro:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert len(lines) == 3
    assert lines[0].startswith("intake: [agnes-3-0-flash] kenary/agnes-3-0-flash:free: no model")
    assert lines[1].startswith("intake: [nex-n2-5-pro] kenary/nex-n2-5-pro:free: no model")
    assert lines[2] == ("intake: hint: 2 id(s) are not in Kilo's model list for provider "
                        "'kenary'. \"did you mean\" compares spelling only — if the provider "
                        "serves the id, add it under provider.kenary.models in kilo.jsonc, or "
                        "re-run with --register-missing.")


def test_run_prints_no_hint_for_a_roster_entirely_on_offer(sandbox, capsys, spawn_holder):
    code, fake = run_fake(sandbox, KC35_SCENARIO,
                          ["--ticket", "1", "--models", "hy3:free",
                           "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()

    assert code == 0
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert not [line for line in (captured.out + captured.err).splitlines()
                if "hint:" in line]


def test_register_missing_registers_the_models_and_runs_the_round(
        sandbox, capsys, monkeypatch):
    """`--register-missing` proves the ids are on offer in a second throwaway and
    gives the round's own server the same overlay, so no first turn can fail with
    `Model not found`."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    registered = FakeKiloServer(KC35_REGISTERED_SCENARIO).start()
    try:
        fakes.extend((plain, registered))
        code = cli.main(["run", *_register_missing_flags("hy3:free",
                                                          "agnes-3-0-flash:free",
                                                          "nex-n2-5-pro:free")])
    finally:
        registered.stop()
        plain.stop()
    captured = capsys.readouterr()

    assert code == 0, captured.err
    # the first throwaway is the operator's own server; the overlay goes to the
    # second and to the round's own server, the same string both times
    assert not calls[0]
    assert len(calls) == 3
    assert calls[1] == {"KILO_CONFIG_CONTENT": json.dumps(KC35_TWO_ENTRIES)}
    # the round's own server gets the same overlay, byte for byte, plus only
    # KC-65's four entries on top of it
    assert calls[2]["KILO_CONFIG_CONTENT"] == calls[1]["KILO_CONFIG_CONTENT"]
    assert set(calls[2]) - {"KILO_CONFIG_CONTENT"} == KC65_ENV_KEYS
    models = json.loads(calls[1]["KILO_CONFIG_CONTENT"])["provider"]["kenary"]["models"]
    assert list(models) == ["agnes-3-0-flash:free", "nex-n2-5-pro:free"], "roster order"
    assert all(entry["reasoning"] is True and entry["name"] == model_id
               for model_id, entry in models.items())
    # the intake saw the missing ids, so no hint and no refusal
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert not [line for line in captured.err.splitlines() if "hint:" in line]

    lines = captured.out.splitlines()
    agent_line = next(line for line in lines if line.startswith("agents "))
    assert lines[lines.index(agent_line) + 1] == (
        "registered for this round (kilo.jsonc untouched): "
        "kenary/agnes-3-0-flash:free, kenary/nex-n2-5-pro:free")
    # the intake's first throwaway only read the offer; the sessions are the
    # round's, on the server that carries the overlay
    assert _sessions(plain) == []
    assert len(_sessions(registered)) == 3


def test_intake_carries_the_overlay_and_the_registered_ids(sandbox, monkeypatch):
    """`Intake.config_content` and `Intake.registered` are what `cmd_run` prints
    and passes to the round's server."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    registered = FakeKiloServer(KC35_REGISTERED_SCENARIO).start()
    try:
        fakes.extend((plain, registered))
        config = sandbox.config(agents=cli.agents_from_models("agnes-3-0-flash:free"),
                                gate_settings=None)
        result = cli.intake(sandbox.repo, sandbox.repo / "epic-tasks", ROUND, "HEAD", config,
                            register_missing=True)
    finally:
        registered.stop()
        plain.stop()

    assert result is not None
    assert result.config_content == json.dumps(KC35_ENTRY)
    assert result.registered == ("kenary/agnes-3-0-flash:free",)
    assert [agent.model for agent in result.agents] == ["kenary/agnes-3-0-flash:free"]


def test_intake_without_the_flag_carries_no_overlay(sandbox, monkeypatch):
    """Both fields default: nothing is registered, nothing is spawned twice."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_REGISTERED_SCENARIO).start()
    try:
        fakes.append(plain)
        config = sandbox.config(agents=cli.agents_from_models("hy3:free"),
                                gate_settings=None)
        result = cli.intake(sandbox.repo, sandbox.repo / "epic-tasks", ROUND, "HEAD", config)
    finally:
        plain.stop()

    assert result is not None
    assert result.config_content is None
    assert result.registered == ()
    assert len(calls) == 1 and not calls[0], "one throwaway, no overlay"


def test_register_missing_fails_when_the_stub_ignores_the_variable(
        sandbox, capsys, monkeypatch):
    """The overlay did not reach a server that answers it: refuse here, before a
    worktree — the same ids would otherwise die on their first turn."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    try:
        fakes.extend((plain, plain))
        code = cli.main(["run", *_register_missing_flags("hy3:free",
                                                              "agnes-3-0-flash:free")])
    finally:
        plain.stop()
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: registration did not take: kenary/agnes-3-0-flash:free"]
    # the overlay really was sent: the second throwaway got it and still answered
    # without it
    assert len(calls) == 2 and not calls[0]
    assert "agnes-3-0-flash:free" in calls[1]["KILO_CONFIG_CONTENT"]
    assert not sandbox.out().exists()
    assert _sessions(plain) == []


def test_register_missing_refuses_an_attached_server(sandbox, capsys, monkeypatch):
    """With a URL the server reads its own `kilo.jsonc` and this process cannot
    change it: one refusal, and the other agents still get their KC-25 check."""
    ini = sandbox.repo / "contest.ini"
    with FakeKiloServer(KC35_SCENARIO) as fake:
        ini.write_text(ini.read_text(encoding="utf-8")
                       .replace("server = spawn", f"server = {fake.url}"), encoding="utf-8")
        code = cli.main(["run", *_register_missing_flags("agnes-3-0-flash:free",
                                                              "sensenova123/sensenova-6.8-flash-lite")])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: --register-missing needs server = spawn: an attached server reads "
                     "its own kilo.jsonc",
                     "intake: [sensenova-6-8-flash-lite] "
                     "sensenova123/sensenova-6.8-flash-lite: no provider 'sensenova123' — "
                     "connected: kenary"]
    assert "hint:" not in captured.err
    assert not sandbox.out().exists()
    assert _sessions(fake) == []


def test_register_missing_still_refuses_a_provider_it_cannot_invent(
        sandbox, capsys, spawn_holder):
    """No provider, no display name, no credentials: registering a model does
    none of those, so the KC-25 line stands and no hint is printed."""
    code, fake = run_fake(sandbox, KC35_SCENARIO,
                          _register_missing_flags("sensenova123/sensenova-6.8-flash-lite"),
                          spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: [sensenova-6-8-flash-lite] "
                     "sensenova123/sensenova-6.8-flash-lite: no provider 'sensenova123' — "
                     "connected: kenary"]
    assert "hint:" not in captured.err
    assert not sandbox.out().exists()
    assert _sessions(fake) == []


def test_register_missing_still_refuses_a_provider_without_credentials(
        sandbox, capsys, spawn_holder):
    offer = _offer((_provider("kenary", "kenari", ("hy3:free",)),), connected=())
    scenario = {"providers": offer,
                "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario, _register_missing_flags("hy3:free"),
                          spawn_holder)
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert len(lines) == 1
    assert lines[0].startswith("intake: [hy3] kenary/hy3:free: provider 'kenary' has no "
                               "credentials (not connected)")
    assert "hint:" not in captured.err


def test_the_operator_s_own_kilo_config_content_survives_the_merge(
        sandbox, capsys, monkeypatch):
    """`KILO_CONFIG_CONTENT` already set is merged, not replaced: the operator's
    options, key and models are still there, and the ids the roster wanted are
    added to them."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    operator = json.dumps({
        "options": {"baseURL": "https://kenari.id/v1"},
        "provider": {"kenary": {"apiKey": "sk-operator",
                                "models": {"agnes-2-5-flash:free": {
                                    "name": "agnes-2-5-flash:free", "reasoning": True}}}}})
    monkeypatch.setenv("KILO_CONFIG_CONTENT", operator)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    registered = FakeKiloServer(KC35_REGISTERED_SCENARIO).start()
    try:
        fakes.extend((plain, registered, registered))
        code = cli.main(["run", *_register_missing_flags("hy3:free",
                                                              "agnes-3-0-flash:free")])
    finally:
        registered.stop()
        plain.stop()
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert list(calls[1]) == ["KILO_CONFIG_CONTENT"]
    merged = json.loads(calls[1]["KILO_CONFIG_CONTENT"])
    assert merged == {
        "options": {"baseURL": "https://kenari.id/v1"},
        "provider": {"kenary": {"apiKey": "sk-operator",
                                "models": {
                                    "agnes-2-5-flash:free": {
                                        "name": "agnes-2-5-flash:free", "reasoning": True},
                                    "agnes-3-0-flash:free": {
                                        "name": "agnes-3-0-flash:free", "reasoning": True}}}},
    }
    assert calls[2]["KILO_CONFIG_CONTENT"] == calls[1]["KILO_CONFIG_CONTENT"], (
        "the round's own server gets the same overlay, byte for byte")
    assert set(calls[2]) - {"KILO_CONFIG_CONTENT"} == KC65_ENV_KEYS
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]


#: The registered offer as Kilo 7.7.5 reports a reasoning model it lists: the
#: variants come with the entry, so they exist only on the server that carries
#: the overlay — the first throwaway does not list the model at all.
KC35_REGISTERED_VARIANTS_SCENARIO = {
    "providers": _offer(({
        "id": "kenary", "name": "kenari", "source": "static",
        "models": {model_id: {
            "id": model_id, "providerID": "kenary", "name": model_id, "status": "active",
            "capabilities": {"reasoning": True, "toolcall": True},
            "variants": {name: {"reasoningEffort": name} for name in ("low", "medium", "high")}}
            for model_id in ("hy3:free", "agnes-3-0-flash:free")}},),),
    "turns": [{"on_prompt": work_ready, "events": ["busy", "idle"]}]}


def test_register_missing_checks_the_variant_on_the_registered_offer(
        sandbox, capsys, monkeypatch):
    """A registered model's variant is checked where the model is listed — the
    server with the overlay — not against the first throwaway, which does not
    list the model and so lists no variant for it either."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    registered = FakeKiloServer(KC35_REGISTERED_VARIANTS_SCENARIO).start()
    try:
        fakes.extend((plain, registered))
        code = cli.main(["run", "--ticket", "1", "--models", "agnes-3-0-flash:free",
                         "--variant", "medium", "--register-missing",
                         "--no-gate", "--no-tests"])
    finally:
        registered.stop()
        plain.stop()
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert _plan(captured.out)["agents"] == "1: kenary/agnes-3-0-flash:free@medium"
    # KC-61: the named variant is asked about once, so there are two sessions on
    # the registered server — the probe and the round's — and neither is on the
    # plain throwaway, which lists no variant for the model at all.
    sessions = _sessions(registered)
    assert len(sessions) == 2
    assert sessions[0]["body"]["title"] == "variant-probe/agnes-3-0-flash:free/medium"
    (session,) = sessions[1:]
    assert session["body"]["model"]["variant"] == "medium"
    assert _sessions(plain) == []


def test_register_missing_names_the_ids_when_the_overlay_server_does_not_start(
        sandbox, capsys, monkeypatch):
    """No server with the overlay, no registration: one line naming what failed —
    not the KC-25 line with a hint to pass the flag the operator just passed."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    calls: list = []
    plain = FakeKiloServer(KC35_SCENARIO).start()

    def spawn_server(binary, *, log_path, **kwargs):
        calls.append(dict(kwargs.get("env") or {}))
        if kwargs.get("env"):
            raise KiloServerError("kilo serve exited before it was healthy")
        return KiloServer.attach(plain.url)

    monkeypatch.setattr(cli.KiloServer, "spawn", staticmethod(spawn_server))
    try:
        code = cli.main(["run", *_register_missing_flags("hy3:free",
                                                              "agnes-3-0-flash:free")])
    finally:
        plain.stop()
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert lines == ["intake: registration did not take: kenary/agnes-3-0-flash:free — "
                     "kilo serve with KILO_CONFIG_CONTENT did not start"]
    assert "hint:" not in captured.err
    assert len(calls) == 2 and "KILO_CONFIG_CONTENT" in calls[1]
    assert not sandbox.out().exists()


def test_register_missing_is_no_refusal_when_the_round_is_refused_otherwise(
        sandbox, capsys, monkeypatch):
    """A round already refused spends nothing on registration, and the ids the
    flag would register are neither refusals nor a hint — only the real reason
    is printed."""
    calls, fakes = _content_spawn_holder(monkeypatch)
    plain = FakeKiloServer(KC35_SCENARIO).start()
    try:
        fakes.append(plain)
        code = cli.main(["run", "--ticket", "9", "--models", "hy3:free,agnes-3-0-flash:free",
                         "--register-missing", "--no-gate", "--no-tests"])
    finally:
        plain.stop()
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]

    assert code == cli.EXIT_FAILED
    assert len(lines) == 1 and "no ticket numbered 9 in" in lines[0]
    assert "hint:" not in captured.err
    assert len(calls) == 1 and not calls[0], "no second throwaway for a refused round"


# ─────────────────────────────────────────────────────────────────────────────
# KC-65 — the agents' pytest worker count
# ─────────────────────────────────────────────────────────────────────────────

KC65_AGENTS = ("agent-a", "agent-b", "agent-c", "agent-d", "agent-e", "agent-f")

def _kc65_offer(agents=KC65_AGENTS):
    """The offer for a roster of *agents*: the intake checks the roster against it."""
    return _offer((_provider("kenary", "kenari", tuple(f"{a}:free" for a in agents)),))


def _kc65_roster(sb, agents, **extra):
    """The sandbox's roster with the KC-65 keys the test sets and the agents named."""
    extra_text = "".join(f"{key} = {value}\n" for key, value in extra.items())
    return sb._roster(agents).replace("max_parallel = 2\n",
                                      f"max_parallel = 2\n{extra_text}")


def _kc65_state(sb, agents, states):
    """A `state.json` with one run per agent in the state named — what `--resume` reads."""
    runs = [
        AgentRun(agent=AgentSpec(name=a, provider_id="kenary", model_id=f"{a}:free"),
                 workspace=Workspace(agent=a, path=sb.rounds / f"{ROUND:02d}-{a}",
                                     branch=f"contest/{ROUND:02d}/{a}", base_sha=sb.base,
                                     kind="clone"),
                 state=AgentState(st))
        for a, st in zip(agents, states)
    ]
    return RoundState(round_no=ROUND, ticket=TICKET_01, base_sha=sb.base,
                      started_at=time.time(), agents=runs)


def _kc65_run(sb, monkeypatch, *, agents=KC65_AGENTS, extra=(), argv=(), unwritable=False):
    """`cli.main(["run", ...])` with the round itself stubbed out.

    `prepare_round`, `run_round` and `export_patches` are stubbed: the question here is the
    env the round's server gets and the worker file it writes, not six worktrees. The intake
    still checks the roster against the fake's offer, so a roster the server does not offer
    is refused the way a real round's would be.
    """
    _write(sb.repo / "contest.ini", _kc65_roster(sb, agents, **dict(extra)))
    monkeypatch.setattr(cli, "prepare_round", lambda *a, **k: [])
    monkeypatch.setattr(cli, "run_round",
                        lambda config, ticket, ticket_path, workspaces, **kw:
                        RoundState(round_no=ROUND, ticket=TICKET_01, base_sha=sb.base,
                                   started_at=time.time(),
                                   agents=[AgentRun(
                                       agent=spec,
                                       workspace=Workspace(
                                           agent=spec.name, path=sb.rounds / spec.name,
                                           branch=f"contest/{ROUND:02d}/{spec.name}",
                                           base_sha=sb.base, kind="clone"),
                                       state=AgentState.READY)
                                           for spec in config.agents]))
    monkeypatch.setattr(cli, "export_patches", lambda *a, **k: [])
    if unwritable:
        def refusing(target, value):
            raise OSError("permission denied")
        monkeypatch.setattr(cli, "write_pytest_workers", refusing)
    calls, fakes = _content_spawn_holder(monkeypatch)
    fakes.append(FakeKiloServer({"providers": _kc65_offer(agents)}).start())
    try:
        code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests", *argv])
    finally:
        fakes[-1].stop()
    return code, calls, sb.out()


def _kc65_env_free(monkeypatch):
    """The four KC-65 vars cleared: the round's env must carry them, not inherit them."""
    monkeypatch.delenv("CONTEST_PYTEST_WORKERS_FILE", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)


def test_the_start_worker_file_and_the_env_the_rounds_server_gets(sandbox, monkeypatch, capsys):
    """Six live agents on 8 cores with a pool of 6: the file holds 2, the fallback the
    same 2, the plugin the dir that reads the file, and `PYTHONPATH` the plugin's dir
    first with the inherited value kept after it — never the runner's repo root."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    inherited = f"{sandbox.tmp}/inherited-a{os.pathsep}{sandbox.tmp}/inherited-b"
    monkeypatch.setenv("PYTHONPATH", inherited)

    code, calls, out = _kc65_run(sandbox, monkeypatch, argv=("--max-parallel", "6"))
    captured = capsys.readouterr()
    assert code == 0

    plugin_dir = str(cli.PYTEST_WORKERS_PLUGIN_DIR)
    assert calls[0] == {}, "the offer check's throwaway gets none of the round's env"
    assert calls[-1] == {
        "PYTEST_PLUGINS": "contest_pytest_workers",
        "PYTHONPATH": f"{plugin_dir}{os.pathsep}{inherited}",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
        "CONTEST_PYTEST_WORKERS_FILE": str(out / "pytest-workers"),
    }
    entries = calls[-1]["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == plugin_dir
    assert set(entries) == {plugin_dir,
                            f"{sandbox.tmp}/inherited-a", f"{sandbox.tmp}/inherited-b"}
    assert str(cli.PYTEST_WORKERS_PLUGIN_DIR.parents[2]) not in entries, (
        "only the plugin's dir, not the runner's repo root")
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "2\n"
    plan = _plan(captured.out)
    assert plan["workers"] == "2 each (auto, min 2)"
    assert plan["tmpdir"] == "-"


def test_the_worker_count_is_the_agents_not_the_pool(sandbox, monkeypatch):
    """Two agents with a pool of 5 count as two: 4 workers each, not the 2 the pool would
    give — the round sizes the crowd it runs, not the slots it could have."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 agents=("agent-a", "agent-b"),
                                 argv=("--max-parallel", "5"))
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "4\n"
    assert calls[-1]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"


def test_the_worker_count_is_never_below_one_nor_above_the_cores(sandbox, monkeypatch):
    """A pool of 16 sizes for six live agents, so the count is the floor; on a box with
    one core it is capped there instead."""
    _kc65_env_free(monkeypatch)
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    code, calls, out = _kc65_run(sandbox, monkeypatch, argv=("--max-parallel", "16"))
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "1\n"

    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    code, calls, out = _kc65_run(sandbox, monkeypatch, extra={"pytest_workers_min": 1},
                                 argv=("--max-parallel", "16"))
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "1\n"


def test_a_fixed_worker_count_is_the_count_and_the_plan_says_so(sandbox, monkeypatch, capsys):
    """`pytest_workers_per_agent = 3` is 3 whatever the cores, and the plan names it as
    fixed rather than auto."""
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    _kc65_env_free(monkeypatch)
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 extra={"pytest_workers_per_agent": 3},
                                 argv=("--max-parallel", "6"))
    captured = capsys.readouterr()
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "3\n"
    assert calls[-1]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"
    assert _plan(captured.out)["workers"] == "3 each (fixed 3)"


def test_resume_counts_the_rounds_agents_not_the_command_line(sandbox, monkeypatch):
    """Six live agents in `state.json` and two names on the command line, a pool of 8: the
    count is for the six — 2 workers each, not the 4 the two would get."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    (sandbox.out() / "state.json").parent.mkdir(parents=True, exist_ok=True)
    _write(sandbox.out() / "state.json",
           json.dumps(_kc65_state(sandbox, KC65_AGENTS, ["WAITING"] * 6).to_dict()))
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 agents=("agent-a", "agent-b"),
                                 argv=("--resume", "--max-parallel", "8"))
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "2\n"
    assert calls[-1]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2"


def test_resume_counts_the_live_agents_not_the_rounds(sandbox, monkeypatch):
    """A resumed round whose eight agents already ended has two live, so 4 workers each —
    not the 2 the eight would get."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    agents = tuple(f"agent-{i}" for i in range(1, 9))
    (sandbox.out() / "state.json").parent.mkdir(parents=True, exist_ok=True)
    _write(sandbox.out() / "state.json", json.dumps(
        _kc65_state(sandbox, agents, ["READY"] * 6 + ["WAITING"] * 2).to_dict()))
    code, calls, out = _kc65_run(sandbox, monkeypatch, agents=agents,
                                 argv=("--resume", "--max-parallel", "8"))
    assert code == 0
    assert (out / "pytest-workers").read_text(encoding="utf-8") == "4\n"
    assert calls[-1]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"


def test_resume_without_a_state_json_fails_before_the_plan(sandbox, monkeypatch, capsys):
    """`--resume` reads `state.json` before the plan is printed, because the plan names
    the count that file holds — so a missing file is a refusal, not a plan."""
    _kc65_env_free(monkeypatch)
    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests", "--resume"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert len(lines) == 1 and f"no {sandbox.out() / 'state.json'}" in lines[0]
    assert not captured.out.strip(), "no plan is printed for a refused round"


def test_an_unwritable_worker_file_is_a_warn_line_and_a_round_without_it(
        sandbox, monkeypatch, capsys):
    """A write that fails is one warn line: the env then carries no
    `CONTEST_PYTEST_WORKERS_FILE`, the plugin answers `None`, and xdist's own fallback is
    what the agents' pytest reads."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    code, calls, out = _kc65_run(sandbox, monkeypatch, unwritable=True,
                                 argv=("--max-parallel", "6"))
    captured = capsys.readouterr()
    assert code == 0
    warns = [line for line in captured.err.splitlines() if line.startswith("warn:")]
    assert len(warns) == 1 and str(out / "pytest-workers") in warns[0]
    assert not (out / "pytest-workers").exists()
    assert "CONTEST_PYTEST_WORKERS_FILE" not in calls[-1]
    assert calls[-1]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2", "the fallback still has a count"
    assert set(calls[-1]) == KC65_ENV_KEYS - {"CONTEST_PYTEST_WORKERS_FILE"}


def test_agent_tmpdir_is_the_rounds_env_the_dir_is_0700_and_it_is_removed(
        sandbox, tmp_path, monkeypatch, capsys):
    """`agent_tmpdir` is where the round puts its own scratch: the dir is made 0700 before
    the server spawns, `TMPDIR`/`TEMP`/`TMP` point at it, the plan names it, and the round
    removes it on the way out — never its parent."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("KC65_SCRATCH", str(scratch))
    code, calls, out = _kc65_run(sandbox, monkeypatch, extra={"agent_tmpdir": "${KC65_SCRATCH}"})
    captured = capsys.readouterr()
    assert code == 0

    agent_tmp = scratch / f"contest-{ROUND}"
    assert not agent_tmp.exists(), "the round removes its own scratch"
    assert scratch.exists(), "the parent is the operator's and stays"
    assert calls[-1]["TMPDIR"] == str(agent_tmp)
    assert calls[-1]["TEMP"] == calls[-1]["TMP"] == str(agent_tmp)
    assert set(calls[-1]) == KC65_ENV_KEYS | {"TMPDIR", "TEMP", "TMP"}
    assert _plan(captured.out)["tmpdir"] == str(agent_tmp)


def test_agent_tmpdir_is_inherited_when_unset(sandbox, tmp_path, monkeypatch, capsys):
    """No `agent_tmpdir` key: no `TMPDIR`/`TEMP`/`TMP` in the env at all, so the agents
    inherit whatever the operator's box has."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    code, calls, out = _kc65_run(sandbox, monkeypatch)
    captured = capsys.readouterr()
    assert code == 0
    assert set(calls[-1]) == KC65_ENV_KEYS
    assert not any(key in calls[-1] for key in ("TMPDIR", "TEMP", "TMP"))
    assert _plan(captured.out)["tmpdir"] == "-"


def test_an_existing_agent_tmpdir_is_left_in_place(sandbox, tmp_path, monkeypatch):
    """The round removes only the dir it made: one that was there before it is the
    operator's, and it stays with what it holds."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    agent_tmp = tmp_path / "scratch" / f"contest-{ROUND}"
    agent_tmp.mkdir(parents=True)
    (agent_tmp / "kept").write_text("x", encoding="utf-8")
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 extra={"agent_tmpdir": str(tmp_path / "scratch")})
    assert code == 0
    assert calls[-1]["TMPDIR"] == str(agent_tmp)
    assert (agent_tmp / "kept").read_text(encoding="utf-8") == "x"


def test_a_round_that_fails_before_its_server_leaves_no_agent_tmpdir(
        sandbox, tmp_path, monkeypatch):
    """A worktree refusal or a server that does not start ends the round early; the
    round's own scratch is either not made yet or removed on the way out."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    scratch = tmp_path / "scratch"
    agent_tmp = scratch / f"contest-{ROUND}"

    def refusing(*a, **k):
        raise cli.WorkspaceError("worktree refused")
    monkeypatch.setattr(cli, "prepare_round", refusing)
    _write(sandbox.repo / "contest.ini",
           _kc65_roster(sandbox, KC65_AGENTS, agent_tmpdir=str(scratch)))
    calls, fakes = _content_spawn_holder(monkeypatch)
    fakes.append(FakeKiloServer({"providers": _kc65_offer(KC65_AGENTS)}).start())
    try:
        code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests"])
    finally:
        fakes[-1].stop()
    assert code == cli.EXIT_FAILED
    assert not agent_tmp.exists()

    def no_server(*a, **k):
        raise cli.KiloServerError("no server")
    monkeypatch.setattr(cli, "_make_backends", no_server)
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 extra={"agent_tmpdir": str(scratch)})
    assert code == cli.EXIT_FAILED
    assert not agent_tmp.exists()


def test_an_uncreatable_agent_tmpdir_is_a_refusal_naming_the_path(
        sandbox, monkeypatch, capsys):
    """The parent is a file, so the round's own scratch cannot be made: intake fails
    naming the path, rather than falling back to `/tmp` and telling nobody."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    _kc65_env_free(monkeypatch)
    code, calls, out = _kc65_run(sandbox, monkeypatch,
                                 extra={"agent_tmpdir": str(sandbox.repo / "pkg" / "thing.py")})
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILED
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert len(lines) == 1
    assert "cannot create" in lines[0]
    assert str(sandbox.repo / "pkg" / "thing.py" / f"contest-{ROUND}") in lines[0]
    assert calls == [{}], "only the offer check's throwaway; no round server of its own"
