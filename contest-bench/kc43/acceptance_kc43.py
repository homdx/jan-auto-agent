"""KC-43 (round 82) judge's acceptance suite: a round runs as numbered legs,
the worktree carried, the session fresh, a mechanical leg record handed on.

Written from the ticket's "What must change" §1–6 and its Acceptance list,
through the public contract only: `python3 -m tools.contest run --legs N`
end to end against the fake Kilo (as `tests/test_contest_cli.py` drives it),
`contest-out/<NN>.<leg>/`, `state.json`, the fake's request log,
`leg_record(run, ws, out_dir) -> Path`, `ContestConfig.legs`, `contest.ini`.
How an entry splits `run_leg` / `run_round` / `cmd_run` internally is not
scored. Must be red on the base (4b2792e).

The fake's scripted turns are per session, so every leg's fresh session
starts again at turn 0; each scenario keeps its own per-worktree leg count.
`max_rework = 0` throughout: a REWORK harvest ends the leg (GAVE_UP) without a
critique prompt, so the relay itself is what is measured.

L* — legs end to end (§1, §2, §4).
R* — READY ends the relay, REWORK does not, scoring once (§5, §6).
N* — the empty leg and `leg_record` (§3).
B* — `legs = 1` is today's round (regression guard).
C* — config, flag, the carry guard.
"""
from __future__ import annotations

import configparser
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_cli as tc  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest import cli  # noqa: E402
from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.roster import AgentSpec, load_roster  # noqa: E402
from tools.contest.runner import AgentRun, AgentState, ContestConfig  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

OUT = tc.OUT                              # contest-out/01
CRITIQUE = "is not accepted yet"          # harvest.rework_message's header


# ── the sandbox and the run ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _gate_key(monkeypatch):
    monkeypatch.setenv("CONTEST_GATE_API_KEY", "unset-for-the-test")


def _sandbox(tmp_path, monkeypatch, *, legs_line: str | None = None):
    sb = tc.Sandbox(tmp_path)
    ini = sb.repo / "contest.ini"
    text = ini.read_text(encoding="utf-8").replace("max_rework = 1", "max_rework = 0")
    if legs_line:
        text = text.replace("max_rework = 0", f"max_rework = 0\n{legs_line}")
    ini.write_text(text, encoding="utf-8")
    tc._git(sb.repo, "commit", "-qam", "roster")
    monkeypatch.chdir(sb.repo)
    return sb


def _run(monkeypatch, scenario, argv):
    holder: list = []

    def spawn_server(binary, *, log_path, **kwargs):
        return KiloServer.attach(holder[-1].url)

    monkeypatch.setattr(cli.KiloServer, "spawn", staticmethod(spawn_server))
    with FakeKiloServer(scenario) as fake:
        holder.append(fake)
        code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests", *argv])
    return code, fake


def _agent(directory) -> str:
    return Path(directory).name.split("-", 1)[1]


def _sessions(fake) -> dict:
    """agent -> [(session id, directory)] in creation order."""
    out: dict = {}
    for s in fake.sessions():
        out.setdefault(_agent(s.directory), []).append((s.id, str(Path(s.directory).resolve())))
    return out


def _first_prompts(fake) -> dict:
    """agent -> [the first prompt of each of its sessions], in order."""
    by_sid = {}
    for sid, text in tc._prompts(fake):
        by_sid.setdefault(sid, text)
    out: dict = {}
    for s in fake.sessions():
        if s.id in by_sid:
            out.setdefault(_agent(s.directory), []).append(by_sid[s.id])
    return out


def _leg_dir(sb, leg: int) -> Path:
    return sb.repo / OUT.parent / f"{OUT.name}.{leg}"


def _state(path: Path) -> dict:
    return json.loads((path / "state.json").read_text(encoding="utf-8"))


def _names_leg(obj, leg: int) -> bool:
    """state.json names its leg: a `leg*` key equal to the number, or `01.<leg>`."""
    blob = json.dumps(obj)
    if re.search(rf'"\s*0?1\.{leg}"', blob) or re.search(rf'"0?1\.{leg}\b', blob):
        return True

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if "leg" in k.lower() and not isinstance(v, (dict, list)) and str(v) in (str(leg), f"1.{leg}", f"01.{leg}"):
                    return True
                if walk(v):
                    return True
        elif isinstance(o, list):
            return any(walk(v) for v in o)
        return False
    return walk(obj)


class _Legs:
    """Per worktree: which leg this prompt belongs to, and HEAD after each leg."""

    def __init__(self, plan):
        self.plan = plan                  # agent -> [action per leg]
        self.leg: dict = {}
        self.heads: dict = {}             # agent -> [sha after leg n]

    def on_prompt(self, directory, text):
        a = _agent(directory)
        n = self.leg.get(a, 0)
        self.leg[a] = n + 1
        acts = self.plan[a]
        act = acts[min(n, len(acts) - 1)]
        if act == "ready":
            tc.work_ready(directory, text)
        elif act == "partial":
            p = Path(directory) / "pkg" / "thing.py"
            p.write_text(p.read_text(encoding="utf-8") + f"# leg {n + 1}\n", encoding="utf-8")
            tc._claim(directory, tc._commit(directory))
        # "nothing": no file touched
        self.heads.setdefault(a, []).append(tc._git(directory, "rev-parse", "HEAD"))

    def scenario(self):
        return {"turns": [{"on_prompt": self.on_prompt, "events": ["busy", "idle"]}]}


# ── L: three legs, nobody finishes ──────────────────────────────────────────

@pytest.fixture(scope="module")
def three_legs(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("CONTEST_GATE_API_KEY", "unset-for-the-test")
    try:
        sb = _sandbox(tmp_path_factory.mktemp("three"), mp)
        legs = _Legs({"agent-a": ["partial"], "agent-b": ["partial"]})
        code, fake = _run(mp, legs.scenario(), ["--legs", "3"])
        return sb, code, fake, legs
    finally:
        mp.undo()


def test_L1_three_leg_folders(three_legs):
    sb, *_ = three_legs
    for leg in (1, 2, 3):
        assert (_leg_dir(sb, leg) / "state.json").is_file(), leg


def test_L2_each_state_names_its_leg(three_legs):
    sb, *_ = three_legs
    for leg in (1, 2, 3):
        assert _names_leg(_state(_leg_dir(sb, leg)), leg), leg


def test_L3_same_worktree_new_session_each_leg(three_legs):
    _sb, _code, fake, _l = three_legs
    for agent, rows in _sessions(fake).items():
        assert len(rows) == 3, (agent, rows)
        assert len({sid for sid, _ in rows}) == 3
        assert len({d for _, d in rows}) == 1, rows


def test_L4_work_survives_into_the_next_leg(three_legs):
    _sb, _code, _fake, legs = three_legs
    for agent, heads in legs.heads.items():
        assert len(heads) == 3, heads
        # each leg amended the previous leg's commit: the tree was carried
        assert len(set(heads)) == 3


def test_L5_leg2_prompt_says_continue_and_carries_leg1_record(three_legs):
    _sb, _code, fake, legs = three_legs
    for agent, prompts in _first_prompts(fake).items():
        p2 = prompts[1]
        assert "continue" in p2.lower(), p2[:400]
        assert legs.heads[agent][0][:7] in p2, (agent, p2[-1500:])


def test_L6_relay_is_not_a_rework(three_legs):
    _sb, _code, fake, _l = three_legs
    for agent, prompts in _first_prompts(fake).items():
        for p in prompts[1:]:
            assert CRITIQUE not in p


def test_L7_attempt_unchanged_across_legs(three_legs):
    sb, *_ = three_legs
    for leg in (2, 3):
        for run in _state(_leg_dir(sb, leg))["agents"]:
            assert run.get("attempt", 0) == 0, (leg, run.get("agent"), run.get("attempt"))


def test_L8_leg3_lists_records_newest_first(three_legs):
    _sb, _code, fake, legs = three_legs
    for agent, prompts in _first_prompts(fake).items():
        p3 = prompts[2]
        h1, h2 = legs.heads[agent][0][:7], legs.heads[agent][1][:7]
        assert h1 in p3 and h2 in p3, p3[-1500:]
        assert p3.index(h2) < p3.index(h1)


def test_L9_leg_prompt_still_names_the_ticket(three_legs):
    _sb, _code, fake, _l = three_legs
    for agent, prompts in _first_prompts(fake).items():
        assert "pkg/thing.py" in prompts[1]


def test_L10_intermediate_legs_export_nothing(three_legs):
    sb, *_ = three_legs
    for leg in (1, 2):
        assert not list(_leg_dir(sb, leg).glob("*.patch")), leg


def test_L11_no_bare_round_folder(three_legs):
    sb, *_ = three_legs
    assert not (sb.repo / OUT / "state.json").exists()


# ── R: READY ends the relay ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ready_on_leg2(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("CONTEST_GATE_API_KEY", "unset-for-the-test")
    try:
        sb = _sandbox(tmp_path_factory.mktemp("r2"), mp)
        legs = _Legs({"agent-a": ["partial", "ready"], "agent-b": ["partial"]})
        code, fake = _run(mp, legs.scenario(), ["--legs", "3"])
        return sb, code, fake, legs
    finally:
        mp.undo()


def test_R1_leg2_ready_stops_that_agent(ready_on_leg2):
    _sb, _code, fake, _l = ready_on_leg2
    assert len(_sessions(fake)["agent-a"]) == 2


def test_R2_the_round_waits_for_the_others(ready_on_leg2):
    _sb, _code, fake, _l = ready_on_leg2
    assert len(_sessions(fake)["agent-b"]) == 3


def test_R3_ready_is_exported_and_exits_zero(ready_on_leg2):
    sb, code, *_ = ready_on_leg2
    assert code == 0
    assert list((sb.repo / OUT.parent).glob("*/agent-a.patch")), \
        sorted(str(p) for p in (sb.repo / OUT.parent).rglob("*.patch"))


def test_R4_final_state_has_the_ready(ready_on_leg2):
    sb, *_ = ready_on_leg2
    last = _state(_leg_dir(sb, 3))
    states = {r["agent"]["name"] if isinstance(r.get("agent"), dict) else r.get("agent"): r["state"]
              for r in last["agents"]}
    assert states.get("agent-a") == "READY", states


def test_R5_leg1_ready_runs_one_leg(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    legs = _Legs({"agent-a": ["ready"], "agent-b": ["partial"]})
    code, fake = _run(monkeypatch, legs.scenario(), ["--legs", "2"])
    ss = _sessions(fake)
    assert len(ss["agent-a"]) == 1 and len(ss["agent-b"]) == 2, ss
    assert code == 0


# ── N: the empty leg and leg_record ─────────────────────────────────────────

def test_N1_empty_leg_record_says_none_and_prompt_names_files(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    legs = _Legs({"agent-a": ["nothing"], "agent-b": ["nothing"]})
    _code, fake = _run(monkeypatch, legs.scenario(), ["--legs", "2"])
    for agent, prompts in _first_prompts(fake).items():
        assert len(prompts) == 2, (agent, len(prompts))
        p2 = prompts[1]
        assert "pkg/thing.py" in p2
        # files and end commit are empty; the harvest did run (REWORK)
        assert p2.lower().count("none") >= 2, p2[-1500:]


def _ws_and_run(tmp_path):
    sb = tc.Sandbox(tmp_path)
    wt = tmp_path / "01-agent-a"
    tc._git(sb.repo, "worktree", "add", "-q", "-b", "contest/01/agent-a", str(wt), sb.base)
    ws = Workspace(agent="agent-a", path=wt.resolve(), branch="contest/01/agent-a",
                   base_sha=sb.base, kind="worktree")
    run = AgentRun(agent=AgentSpec(name="agent-a", provider_id="kenary", model_id="agent-a:free"), workspace=ws)
    return sb, ws, run


def _record_text(run, ws, out_dir) -> str:
    from tools.contest import runner
    assert hasattr(runner, "leg_record"), "no runner.leg_record"
    path = runner.leg_record(run, ws, out_dir)
    assert isinstance(path, Path) and path.is_file(), path
    return path.read_text(encoding="utf-8")


def test_N2_leg_record_signature():
    from tools.contest import runner
    params = list(inspect.signature(runner.leg_record).parameters.values())
    required = [p for p in params if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert len(required) == 3, params


def test_N3_leg_record_of_nothing_is_all_none(tmp_path):
    _sb, ws, run = _ws_and_run(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    text = _record_text(run, ws, out)
    assert text.lower().count("none") >= 3, text
    assert "pkg/thing.py" not in text


def test_N4_leg_record_of_a_commit(tmp_path):
    _sb, ws, run = _ws_and_run(tmp_path)
    tc.work_no_test(str(ws.path), "")
    sha = tc._git(ws.path, "rev-parse", "HEAD")
    run.commit = sha
    run.turns = [{"kind": "initial", "attempt": 0,
                  "harvest": {"verdict": "REWORK", "reasons": ["no_test_added"]}}]
    out = tmp_path / "out"
    out.mkdir()
    text = _record_text(run, ws, out)
    assert "pkg/thing.py" in text
    assert re.search(r"\+|\d+ insertion|\|\s*\d", text), text          # a diffstat
    assert sha[:7] in text
    assert "REWORK" in text and "no_test_added" in text


def test_N5_leg_record_uncommitted_tree_has_no_end_commit(tmp_path):
    """Edits, no commit: the files show, the end commit is still `none`."""
    _sb, ws, run = _ws_and_run(tmp_path)
    tc._write(ws.path / "pkg" / "thing.py", tc.THING_CHANGED)
    out = tmp_path / "out"
    out.mkdir()
    text = _record_text(run, ws, out)
    assert "none" in text.lower()
    assert ws.base_sha[:7] not in text or "pkg/thing.py" in text


# ── B: legs = 1 is today's round ────────────────────────────────────────────

def _one_leg(tmp_path, monkeypatch, argv, legs_line=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    sb = _sandbox(tmp_path, monkeypatch, legs_line=legs_line)
    legs = _Legs({"agent-a": ["ready"], "agent-b": ["partial"]})
    code, fake = _run(monkeypatch, legs.scenario(), argv)
    files = sorted(p.name for p in (sb.repo / OUT).iterdir())
    folders = sorted(p.name for p in (sb.repo / OUT.parent).iterdir())
    prompts = sorted(re.sub(r"\b[0-9a-f]{7,40}\b", "<SHA>", t.replace(str(sb.tmp), "<TMP>"))
               for _, t in tc._prompts(fake))
    return code, files, folders, prompts, sb




def test_B1_default_is_the_bare_folder(tmp_path, monkeypatch):
    code, files, folders, _p, _sb = _one_leg(tmp_path, monkeypatch, [])
    assert code == 0
    assert folders == [OUT.name], folders
    assert not [f for f in files if "leg" in f.lower()], files
    assert "agent-a.patch" in files and "state.json" in files


def test_B2_legs_1_flag_same_as_default(tmp_path, monkeypatch):
    a = _one_leg(tmp_path / "a", monkeypatch, [])
    b = _one_leg(tmp_path / "b", monkeypatch, ["--legs", "1"])
    assert a[:4] == b[:4]


def test_B3_legs_1_ini_same_as_default(tmp_path, monkeypatch):
    a = _one_leg(tmp_path / "a", monkeypatch, [])
    b = _one_leg(tmp_path / "b", monkeypatch, [], legs_line="legs = 1")
    assert a[:4] == b[:4]


def test_B4_one_leg_prompt_has_no_relay_text(tmp_path, monkeypatch):
    _c, _f, _d, prompts, _sb = _one_leg(tmp_path, monkeypatch, [])
    for p in prompts:
        assert not re.search(r"\bleg\b|\blegs\b|relay", p, re.I), p[:300]


def test_B5_one_leg_state_json_has_no_leg_suffix_path(tmp_path, monkeypatch):
    _c, _f, _d, _p, sb = _one_leg(tmp_path, monkeypatch, [])
    blob = (sb.repo / OUT / "state.json").read_text(encoding="utf-8")
    assert f"{OUT.name}.1" not in blob


# ── C: config, flag, carry guard ────────────────────────────────────────────

def test_C1_config_default_one():
    assert ContestConfig(agents=()).legs == 1


def test_C2_contest_ini_says_one():
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(REPO_ROOT / "contest.ini")
    assert int(cp["contest"]["legs"]) == 1


def test_C3_roster_reads_legs(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path, monkeypatch, legs_line="legs = 3")
    assert load_roster(sb.repo / "contest.ini").legs == 3


def test_C4_ini_legs_used_without_flag(tmp_path, monkeypatch):
    sb = _sandbox(tmp_path, monkeypatch, legs_line="legs = 2")
    legs = _Legs({"agent-a": ["partial"], "agent-b": ["partial"]})
    _code, fake = _run(monkeypatch, legs.scenario(), [])
    assert len(_sessions(fake)["agent-a"]) == 2
    assert (_leg_dir(sb, 2) / "state.json").is_file()


def test_C5_flag_overrides_ini(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, legs_line="legs = 3")
    legs = _Legs({"agent-a": ["partial"], "agent-b": ["partial"]})
    _code, fake = _run(monkeypatch, legs.scenario(), ["--legs", "1"])
    assert len(_sessions(fake)["agent-a"]) == 1


def test_C6_help_names_the_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    assert "--legs" in capsys.readouterr().out


def test_C7_previous_rounds_worktree_still_refused(tmp_path, monkeypatch):
    """A finished relay leaves worktrees with commits; a new run of the same
    round without --fresh is refused exactly as today (KC-23), legs or not."""
    _sandbox(tmp_path, monkeypatch)
    legs = _Legs({"agent-a": ["partial"], "agent-b": ["partial"]})
    _run(monkeypatch, legs.scenario(), ["--legs", "2"])
    code, fake = _run(monkeypatch, legs.scenario(), ["--legs", "2"])
    assert code == 1
    assert not fake.sessions()


def test_C8_bad_legs_refused(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    legs = _Legs({"agent-a": ["partial"], "agent-b": ["partial"]})
    try:
        code, fake = _run(monkeypatch, legs.scenario(), ["--legs", "0"])
    except SystemExit as exc:
        code, fake = exc.code, None
    assert code not in (0, None)
    assert fake is None or not fake.sessions()
