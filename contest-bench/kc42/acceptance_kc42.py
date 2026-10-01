"""KC-42 (round 81) judge's acceptance suite: an agent that has not touched a
file is nudged, then given a fresh session, then declared DEAD.

Written from the ticket's "What must change" §1–5 and its Acceptance list,
through the public contract only: the `first_touch_sec` / `first_touch_nudges`
config keys, `AgentState.DEAD`, the fake's request log, `state.json`,
`render_table` and `export_patches`. Field names inside `turns.jsonl` are not
fixed by the ticket and are not scored. Must be red on the base (866784c).

The fake here is Kilo at its most useless: a turn that beats `session.status
busy` every 0.1 s and never writes a file — round 64's `nex-n2-5-pro`. An abort
ends it with `session.idle`, as the live server does; a later prompt into the
same session ends it too, as the live server's loop picks the new message up.

F* — the first-touch clock end to end (§1–4).
D* — DEAD in the round: not harvested, own row, does not hold the round (§4–5).
C* — the committed config and enum.
B* — the required bench files.
"""
from __future__ import annotations

import configparser
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

from test_contest_runner import (  # noqa: E402
    Harness, Sandbox, _BenchFake, _git, _prompts, _round, _session_posts,
    _write, make_config, work_ready,
)
from tools.contest.runner import AgentState, ContestConfig  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


class _Beating(_BenchFake):
    """A turn with ``"beat": secs`` emits ``busy`` every 0.1 s for that long,
    then replies and idles; ``"touch_at": t`` runs ``"touch"`` (a callable on
    the directory) once at t seconds. An abort, or the next prompt into the
    same session, ends the beat early."""

    def _run_turn(self, session, turn, text):
        if "beat" not in turn:
            return super()._run_turn(session, turn, text)
        mine = session.turn_index
        start = time.monotonic()
        touched = False
        while not self._stop.is_set():
            el = time.monotonic() - start
            if session.aborted:
                self._emit({"type": "session.idle", "properties": {"sessionID": session.id}})
                return
            if session.turn_index != mine:
                return                      # a newer prompt took the session over
            if not touched and turn.get("touch_at") is not None and el >= turn["touch_at"]:
                turn["touch"](session.directory)
                touched = True
            if el >= turn["beat"]:
                break
            self._emit({"type": "session.status",
                        "properties": {"sessionID": session.id, "status": {"type": "busy"}}})
            time.sleep(0.1)
        rest = {k: v for k, v in turn.items() if k not in ("beat", "touch_at", "touch")}
        rest.setdefault("events", ["idle"])
        super()._run_turn(session, rest, text)


FOREVER = 600


def _dead_turn():
    return {"beat": FOREVER}


def _cfg(**over):
    kw = dict(first_touch_sec=2, first_touch_nudges=1, max_sessions_per_attempt=2,
              max_continues_per_attempt=0, max_rework=0,
              turn_timeout_sec=240, idle_event_timeout_sec=60)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def _go(tmp_path, scenario, cfg):
    sb = Sandbox(tmp_path)
    t0 = time.monotonic()
    with _Beating(scenario) as fake:
        run = Harness(sb, fake, cfg).go()
        prompts = _prompts(fake)
        sessions = [s.id for s in fake.sessions()]
        aborts = [r["path"].split("/")[2] for r in fake.calls("POST")
                  if r["path"].endswith("/abort")]
    return sb, run, prompts, sessions, aborts, time.monotonic() - t0


def _revert(directory):
    _git(Path(directory), "checkout", "--", "pkg/thing.py")


def _edit(directory):
    _write(Path(directory) / "pkg" / "thing.py", "def thing():\n    return 5\n")


# ── F: the clock ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dead_run(tmp_path_factory):
    return _go(tmp_path_factory.mktemp("dead"),
               {"turns": [_dead_turn() for _ in range(6)]}, _cfg())


def test_F1_untouched_agent_ends_dead(dead_run):
    _sb, run, *_ = dead_run
    assert run.state == AgentState.DEAD, (run.state, run.last_error)


def test_F2_one_nudge_goes_into_the_first_session(dead_run):
    _sb, _run, prompts, sessions, *_ = dead_run
    first = [t for sid, t in prompts if sid == sessions[0]]
    assert len(first) == 2, [t[:60] for t in first]


def test_F3_nudge_names_the_declared_files(dead_run):
    _sb, _run, prompts, sessions, *_ = dead_run
    nudge = [t for sid, t in prompts if sid == sessions[0]][1]
    assert "pkg/thing.py" in nudge


def test_F4_escalation_is_a_fresh_session(dead_run):
    _sb, run, prompts, sessions, aborts, _ = dead_run
    assert len(sessions) == 2
    assert sessions[0] in aborts
    assert any(sid == sessions[1] for sid, _ in prompts)
    assert run.attempt == 0


def test_F5_dead_session_is_aborted(dead_run):
    _sb, _run, _p, sessions, aborts, _ = dead_run
    assert sessions[-1] in aborts


def test_F6_dead_is_fast(dead_run):
    *_, elapsed = dead_run
    # three deadlines of 2 s plus polling; nowhere near the 240 s turn clock
    assert elapsed < 90, elapsed


def test_F8_touch_before_deadline_never_nudges_even_after_revert(tmp_path):
    """Edit at 0.3 s, held for 12 s (seen by any sane poll), reverted, then
    13 s of busy on a clean tree — over three 4 s deadlines. A clock disarmed
    for good does not nudge; one re-armed by the clean tree does."""
    def edit_then_revert(d):
        _edit(d)
        threading.Timer(12, _revert, args=(d,)).start()
    turn = {"beat": 25, "touch_at": 0.3, "touch": edit_then_revert}
    _sb, run, prompts, sessions, _a, _e = _go(tmp_path, {"turns": [turn]},
                                              _cfg(first_touch_sec=4))
    assert len(prompts) == 1, [t[:60] for _, t in prompts]
    assert len(sessions) == 1
    assert run.state != AgentState.DEAD


def test_F9_touch_after_nudge_no_escalation(tmp_path):
    scenario = {"turns": [_dead_turn(), {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _sb, run, prompts, sessions, _a, _e = _go(tmp_path, scenario, _cfg())
    assert len(sessions) == 1
    assert run.state == AgentState.READY, (run.state, run.last_error)


def test_F10_zero_is_off(tmp_path):
    turn = {"beat": 6, "on_prompt": None}
    scenario = {"turns": [dict(turn, touch_at=5.5, touch=lambda d: work_ready(d, ""))]}
    _sb, run, prompts, sessions, _a, _e = _go(tmp_path, scenario, _cfg(first_touch_sec=0))
    assert len(prompts) == 1 and len(sessions) == 1
    assert run.state == AgentState.READY, (run.state, run.last_error)


def test_F11_same_late_touch_with_the_clock_on_is_nudged(tmp_path):
    """Control for F10: the clock is what makes the difference."""
    scenario = {"turns": [{"beat": 13, "touch_at": 12, "touch": lambda d: work_ready(d, "")},
                          {"on_prompt": work_ready, "events": ["busy", "idle"]}]}
    _sb, _run, prompts, _s, _a, _e = _go(tmp_path, scenario, _cfg())
    assert len(prompts) >= 2


def test_F12_no_second_session_allowed_means_dead_after_the_nudge(tmp_path):
    _sb, run, prompts, sessions, _a, _e = _go(
        tmp_path, {"turns": [_dead_turn() for _ in range(4)]},
        _cfg(max_sessions_per_attempt=1))
    assert run.state == AgentState.DEAD
    assert len(sessions) == 1 and len(prompts) == 2


def test_F13_two_nudges_budget(tmp_path):
    _sb, run, prompts, sessions, _a, _e = _go(
        tmp_path, {"turns": [_dead_turn() for _ in range(6)]},
        _cfg(first_touch_nudges=2, max_sessions_per_attempt=1))
    assert run.state == AgentState.DEAD
    assert len(prompts) == 3


def test_F14_a_commit_disarms(tmp_path):
    def commit(d):
        _edit(d)
        _git(Path(d), "commit", "-qam", "x")
    turn = {"beat": 7, "touch_at": 0.3, "touch": commit}
    _sb, run, prompts, _s, _a, _e = _go(tmp_path, {"turns": [turn]}, _cfg())
    assert len(prompts) == 1


# ── D: in the round ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dead_round(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("round")
    sb = Sandbox(tmp)
    t0 = time.monotonic()
    with _Beating({"turns": [_dead_turn() for _ in range(6)]}) as fake:
        state = _round(sb, fake, _cfg())
    return sb, state, time.monotonic() - t0


def test_D1_round_state_dead(dead_round):
    _sb, state, _ = dead_round
    assert state.agents[0].state == AgentState.DEAD


def test_D2_summary_row_says_dead(dead_round):
    from tools.contest.export import render_table
    _sb, state, _ = dead_round
    lines = render_table(state, [])
    cols = [c.strip() for c in lines[0].strip("|").split("|")]
    cells = [c.strip() for c in lines[2].strip("|").split("|")]
    assert cells[cols.index("state")] == "DEAD"


def test_D3_not_harvested(dead_round):
    from tools.contest.cli import export_patches
    sb, state, _ = dead_round
    out = sb.out_dir / "export"
    out.mkdir(exist_ok=True)
    paths = export_patches(state, list(sb.workspaces), out)
    assert not [p for p in paths if p], paths
    assert state.agents[0].commit in (None, "")


def test_D4_round_does_not_wait(dead_round):
    *_, elapsed = dead_round
    assert elapsed < 120, elapsed


# ── C: config and enum ──────────────────────────────────────────────────────

def test_C1_dead_is_terminal():
    assert AgentState.DEAD.terminal and AgentState.DEAD == "DEAD"


def test_C2_contest_ini_names_the_keys():
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(REPO_ROOT / "contest.ini")
    flat = {k: v for s in cp.sections() for k, v in cp[s].items()}
    assert int(flat["first_touch_sec"]) == 420
    assert int(flat["first_touch_nudges"]) == 1


def test_C3_config_defaults():
    cfg = ContestConfig(agents=())
    assert cfg.first_touch_sec == 420 and cfg.first_touch_nudges == 1


# ── B: bench files ──────────────────────────────────────────────────────────

def test_B1_replay_and_probe_exist():
    for f in ("replay64.py", "live_probe.py", "RESULTS.md"):
        assert (REPO_ROOT / "contest-bench" / "kc42" / f).is_file(), f


def test_B2_replay_fires_for_exactly_the_three():
    r64 = Path("/home/renat/Project/opensource/github/agent-offline/qwen25/contest-out/64")
    script = REPO_ROOT / "contest-bench" / "kc42" / "replay64.py"
    if not r64.is_dir() or not script.is_file():
        pytest.skip("no round 64 data or no replay")
    p = subprocess.run([sys.executable, str(script), str(r64)], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
