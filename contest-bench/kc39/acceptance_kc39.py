"""KC-39 (round 78) judge's acceptance suite: a continue that repeats its diff
starts a fresh session, not a same-session harvest.

Written from the ticket's "What must change" §1–6 and Acceptance list, through
the public contract only: `_diff_signature(ws)`, the `max_sessions_per_attempt`
config key, the fake's request log, `turns.jsonl`, `state.json`, `run.cost` /
`run.tokens` and the transcript files. §7 (the 90 % rework rule) is retired by
the ticket's own audit and is not scored. Must be red on the base (5b9362c).

S* — `_diff_signature` (§1).
R* — the runner end to end over the base's fake Kilo (§2–6).
C* — the committed config (§4).
"""
from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_runner import (  # noqa: E402
    Harness, Sandbox, _BenchFake, _git, _jsonl, _prompts, _session_posts,
    _write, make_config, work_ready,
)
from tools.contest.runner import AgentState  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


def _sig(ws):
    from tools.contest.runner import _diff_signature
    return _diff_signature(ws)


# ── the fake: one script per session ────────────────────────────────────────

class _PerSession(_BenchFake):
    """The nth session created replays ``scripts[n-1]`` (its turns and its
    ``session`` info); past the last script the last one repeats."""

    def __init__(self, scripts):
        self.scripts = [dict(s) for s in scripts]
        super().__init__(dict(self.scripts[0]))

    def _create_session(self, body, directory):
        n = len(self.sessions())
        script = self.scripts[min(n, len(self.scripts) - 1)]
        self.scenario["turns"] = list(script.get("turns") or [])
        self.scenario["session"] = dict(script.get("session") or {})
        return super()._create_session(body, directory)


def write_same(directory, text):
    """Byte-identical content to the same file every turn: no progress."""
    _write(Path(directory) / "pkg" / "thing.py", "def thing():\n    return 42\n")


_n = {"i": 100}   # the sandbox base returns 1


def write_new(directory, text):
    """New content every turn: real progress, never committed."""
    _n["i"] += 1
    _write(Path(directory) / "pkg" / "thing.py", f"def thing():\n    return {_n['i']}\n")


def _t(hook):
    return {"on_prompt": hook, "events": ["busy", "idle"]}


def _cfg(**over):
    kw = dict(max_continues_per_attempt=2, max_rework=0, max_sessions_per_attempt=2)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def _go(tmp_path, scripts, cfg):
    sb = Sandbox(tmp_path)
    with _PerSession(scripts) as fake:
        run = Harness(sb, fake, cfg).go()
    return sb, fake, run


def _aborts(fake):
    return [r["path"].split("/")[2] for r in fake.calls("POST") if r["path"].endswith("/abort")]


def _turn_rows(sb):
    return _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")


STUCK2 = {"turns": [_t(write_same), _t(write_same)]}            # initial + continue 1
STUCK3 = {"turns": [_t(write_same), _t(write_same), _t(write_same)]}
FINISH = {"turns": [_t(work_ready)]}


# ── S: the signature ────────────────────────────────────────────────────────

def test_S1_clean_tree_is_empty(tmp_path):
    sb = Sandbox(tmp_path)
    assert _sig(sb.ws("agent-a")) == ""


def test_S2_same_files_different_content_differ(tmp_path):
    sb = Sandbox(tmp_path, ["a", "b"])
    _write(sb.ws("a").path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    _write(sb.ws("b").path / "pkg" / "thing.py", "def thing():\n    return 3\n")
    sa, sb_ = _sig(sb.ws("a")), _sig(sb.ws("b"))
    assert sa and sb_ and sa != sb_


def test_S3_same_content_same_signature(tmp_path):
    sb = Sandbox(tmp_path, ["a", "b"])
    for a in ("a", "b"):
        _write(sb.ws(a).path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    assert _sig(sb.ws("a")) == _sig(sb.ws("b")) != ""
    assert _sig(sb.ws("a")) == _sig(sb.ws("a"))


def test_S4_untracked_file_changes_signature(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    before = _sig(ws)
    _write(ws.path / "pkg" / "extra.py", "X = 1\n")
    assert _sig(ws) not in ("", before)


def test_S5_untracked_content_counts(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "extra.py", "X = 1\n")
    one = _sig(ws)
    _write(ws.path / "pkg" / "extra.py", "X = 2\n")
    assert one and _sig(ws) not in ("", one)


def test_S6_runs_dir_is_not_work(tmp_path):
    """runs/ is gitignored and holds the agent's PROGRESS.csv in every live
    worktree: its presence must neither blank the signature nor change it."""
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    before = _sig(ws)
    _write(ws.path / "runs" / "agent-a" / "PROGRESS.csv", "a\n")
    assert _sig(ws) == before != ""
    _write(ws.path / "runs" / "agent-a" / "PROGRESS.csv", "a\nb\n")
    assert _sig(ws) == before


def test_S8_stuck_model_with_a_progress_file_still_resets(tmp_path):
    """The live shape: the agent has written its runs/ row, then loops."""
    def same_with_row(directory, text):
        write_same(directory, text)
        _write(Path(directory) / "runs" / "agent-a" / "PROGRESS.csv", "ticket\n")
    stuck = {"turns": [_t(same_with_row), _t(same_with_row)]}
    sb, fake, run = _go(tmp_path, [stuck, FINISH], _cfg())
    assert len(_session_posts(fake)) == 2


def test_S7_committed_work_is_not_uncommitted(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    _git(ws.path, "commit", "-qam", "x")
    assert _sig(ws) == ""


# ── R: the runner ───────────────────────────────────────────────────────────

def test_R1_stuck_model_gets_a_fresh_session_and_finishes(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    assert run.state is AgentState.READY, run.last_error
    s1, s2 = fake.sessions()
    assert _aborts(fake)[:1] == [s1.id]


def test_R2_same_provider_and_model(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    p1, p2 = _session_posts(fake)
    assert p1["body"]["model"] == p2["body"]["model"]


def test_R3_abort_before_second_post(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    s1 = fake.sessions()[0]
    paths = [r["path"] for r in fake.calls("POST")]
    posts = [i for i, p in enumerate(paths) if p == "/session"]
    abort = paths.index(f"/session/{s1.id}/abort")
    assert posts[0] < abort < posts[1]


def test_R4_new_session_prompt_carries_the_git_status(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    s1, s2 = fake.sessions()
    prompts = _prompts(fake)
    assert [sid for sid, _ in prompts] == [s1.id, s1.id, s2.id]
    assert "pkg/thing.py" in prompts[2][1]


def test_R5_attempt_unchanged_continues_reset(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    assert run.attempt == 0
    assert run.continues == 0
    assert not any(r.get("kind") == "rework" for r in _turn_rows(sb))


def test_R6_new_session_recorded_on_the_turn(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    s2 = fake.sessions()[1]
    rows = _turn_rows(sb)
    assert [r.get("new_session") for r in rows if r.get("new_session")] == [s2.id]
    assert run.session_id == s2.id


def test_R7_continue_turns_carry_diff_signature(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    sigs = [r["diff_signature"] for r in _turn_rows(sb) if r.get("diff_signature")]
    assert len(sigs) >= 2 and len(set(sigs)) == 1


def test_R8_ceiling_one_means_no_reset(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK3], _cfg(max_sessions_per_attempt=1))
    assert len(_session_posts(fake)) == 1 and not _aborts(fake)
    assert len(_prompts(fake)) == 3
    assert run.state is not AgentState.READY


def test_R9_ceiling_two_resets_once_then_harvests_in_place(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, STUCK3, FINISH], _cfg())
    assert len(_session_posts(fake)) == 2
    s1, s2 = fake.sessions()
    assert [sid for sid, _ in _prompts(fake)] == [s1.id, s1.id, s2.id, s2.id, s2.id]
    assert run.state is not AgentState.READY


def test_R10_zero_is_today(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK3], _cfg(max_sessions_per_attempt=0))
    assert len(_session_posts(fake)) == 1 and not _aborts(fake)
    assert len(_prompts(fake)) == 3
    assert not any(r.get("new_session") for r in _turn_rows(sb))


def test_R11_changing_content_never_resets(tmp_path):
    progress = {"turns": [_t(write_new), _t(write_new), _t(write_new)]}
    sb, fake, run = _go(tmp_path, [progress], _cfg())
    assert len(_session_posts(fake)) == 1 and not _aborts(fake)
    assert len(_prompts(fake)) == 3
    sigs = [r.get("diff_signature") for r in _turn_rows(sb) if r.get("diff_signature")]
    assert len(sigs) == len(set(sigs))


def test_R12_only_the_last_continue_resets(tmp_path):
    """max_continues = 3: a repeat at continue 2 is still granted in place;
    the repeat at the last one (3) opens the fresh session."""
    s1 = {"turns": [_t(write_same)] * 3}
    sb, fake, run = _go(tmp_path, [s1, FINISH], _cfg(max_continues_per_attempt=3))
    assert run.state is AgentState.READY, run.last_error
    a, b = fake.sessions()
    assert [sid for sid, _ in _prompts(fake)] == [a.id, a.id, a.id, b.id]


def test_R13_cost_and_tokens_are_summed(tmp_path):
    one = dict(STUCK2, session={"cost": 0.01, "tokens": {"total": 100, "input": 80, "output": 20}})
    two = dict(FINISH, session={"cost": 0.02, "tokens": {"total": 200, "input": 150, "output": 50}})
    sb, fake, run = _go(tmp_path, [one, two], _cfg())
    assert run.state is AgentState.READY, run.last_error
    assert run.cost == pytest.approx(0.03)
    assert run.tokens["total"] == 300
    assert run.tokens["input"] == 230 and run.tokens["output"] == 70


def test_R14_transcript_keeps_both_sessions(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    s1, s2 = fake.sessions()
    text = "".join(p.read_text(encoding="utf-8")
                   for p in sb.out_dir.glob("agent-a.session*.json"))
    assert s1.id in text and s2.id in text


def test_R15_state_json_shows_sessions(tmp_path):
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    # the dict `state.json` holds for the agent, through JSON and back
    agent = json.loads(json.dumps(run.to_dict()))
    counts = [v for k, v in agent.items()
              if "session" in k and k != "session_id" and isinstance(v, int)
              and not isinstance(v, bool)]
    assert 2 in counts


def test_R16_summary_has_a_sessions_column(tmp_path):
    from tools.contest.export import render_table
    from tools.contest.runner import AgentRun, RoundState
    sb, fake, run = _go(tmp_path, [STUCK2, FINISH], _cfg())
    back = AgentRun.from_dict(json.loads(json.dumps(run.to_dict())))
    state = RoundState(round_no=78, ticket="t", base_sha=sb.base_sha, started_at=0.0,
                       agents=[back])
    lines = render_table(state, [])
    lines = lines.splitlines() if isinstance(lines, str) else lines
    cols = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    cells = [c.strip() for c in lines[2].strip().strip("|").split("|")]
    idx = [i for i, c in enumerate(cols) if "session" in c.lower()]
    assert idx and cells[idx[0]] == "2"


def test_R17_no_first_continue_reset_on_a_clean_first_signature(tmp_path):
    """max_continues = 1: the only continue is also the last, but there is no
    previous continue to compare with — it is granted, no reset."""
    sb, fake, run = _go(tmp_path, [STUCK2], _cfg(max_continues_per_attempt=1))
    assert len(_session_posts(fake)) == 1 and not _aborts(fake)
    assert len(_prompts(fake)) == 2


# ── C: the committed config ─────────────────────────────────────────────────

def test_C1_contest_ini_names_the_key():
    cp = configparser.ConfigParser()
    cp.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    vals = [cp[s].get("max_sessions_per_attempt") for s in cp.sections()
            if cp[s].get("max_sessions_per_attempt") is not None]
    assert vals and int(vals[0].split("#")[0]) == 2


def test_C2_config_default_is_two():
    import dataclasses
    from tools.contest.roster import ContestConfig
    (f,) = [f for f in dataclasses.fields(ContestConfig) if f.name == "max_sessions_per_attempt"]
    assert f.default == 2
