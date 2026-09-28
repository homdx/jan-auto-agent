"""KC-40 (round 79) judge's acceptance suite: a nearly full session holding an
uncommitted diff is asked for the model's own summary, and the reply is copied
out, before anything is compacted or lost.

Written from the ticket's "What must change" §1–7 and its Acceptance list,
through the public contract only: `SUMMARY_PROMPT`, the `summary_at_percent`
key, the fake's request log, `<agent>.summary.md` under `out_dir`,
`turns.jsonl`'s `summary_attempted` / `summary_captured`, `run.summary` through
`state.json`. The live probe (`contest-bench/kc40/live_probe.py`) is judged by
hand, not here. Must be red on the base (2a0132f).

E* — the edge (§1): who is asked and who is not.
P* — the prompt, the copy and the records (§2, §3, §4, §7).
F* — the fallback: KC-39's fresh session (§5), the summary carried (§6).
C* — the config key.
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
    Harness, Sandbox, _BenchFake, _jsonl, _prompts, _session_posts,
    make_config, work_edit_no_commit, work_edit_same, work_no_test, work_ready,
)
from tools.contest.runner import AgentState  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

#: `context_limit_fallback`: the only size the agent has.
WINDOW = 32768
#: 92 % of it — past `summary_at_percent` (90) and `compact_at_percent` (80).
HIGH = 30_200
#: 85 % — past the compact, under the summary.
MID = 27_900

NOTE_1 = "NOTE-ALPHA: changed pkg/thing.py to return 7; left: the test."
NOTE_2 = "NOTE-BRAVO: second session, the test is still left."


def _prompt_text():
    from tools.contest.runner import SUMMARY_PROMPT
    return SUMMARY_PROMPT


class _PerSession(_BenchFake):
    """The nth session created replays ``scripts[n-1]``; past the last, the last."""

    def __init__(self, scripts):
        self.scripts = [dict(s) for s in scripts]
        super().__init__(dict(self.scripts[0]))

    def _create_session(self, body, directory):
        n = len(self.sessions())
        script = self.scripts[min(n, len(self.scripts) - 1)]
        self.scenario["turns"] = list(script.get("turns") or [])
        return super()._create_session(body, directory)


def _fill(tokens):
    return {"tokens": {"input": tokens, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}}}


def edit(tokens=None):
    t = {"on_prompt": work_edit_no_commit, "events": ["busy", "idle"]}
    if tokens:
        t["message_info"] = _fill(tokens)
    return t


def reply(text, tokens=HIGH + 300):
    """The model's answer to the summary prompt: text only, no file touched."""
    return {"events": ["busy", "idle"], "assistant": text, "message_info": _fill(tokens)}


def same():
    return {"on_prompt": work_edit_same, "events": ["busy", "idle"]}


READY = {"on_prompt": work_ready, "events": ["busy", "idle"]}
EMPTY = {"events": ["busy", "idle"], "assistant": ""}
ERROR = {"events": ["busy"], "error": {"name": "ProviderError", "message": "boom-42"}}


def _cfg(tmp_path, **over):
    kw = dict(context_memory_file=str(tmp_path / "context-memory.json"),
              context_limit_fallback=WINDOW, max_continues_per_attempt=3,
              max_sessions_per_attempt=2, max_error_retries=0)
    kw.update(over)
    return make_config(["agent-a"], **kw)


def _go(tmp_path, scripts, cfg=None, summary_tokens=3_000):
    sb = Sandbox(tmp_path)
    scripts = [dict(s, summary_tokens=summary_tokens) for s in scripts]
    with _PerSession(scripts) as fake:
        run = Harness(sb, fake, cfg or _cfg(tmp_path)).go()
    return sb, fake, run


def _posts(fake):
    return [r["path"] for r in fake.calls("POST")]


def _summary_prompts(fake):
    text = _prompt_text()
    return [(sid, t) for sid, t in _prompts(fake) if t.strip() == text.strip()]


def _summary_files(sb):
    return sorted(sb.out_dir.rglob("agent-a.summary.md"))


def _summary_file_text(sb):
    return "".join(p.read_text(encoding="utf-8") for p in _summary_files(sb))


def _rows(sb):
    return _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")


def _aborts(fake):
    return [p.split("/")[2] for p in _posts(fake) if p.endswith("/abort")]


# ── E: the edge ─────────────────────────────────────────────────────────────

def test_E1_dirty_tree_past_the_threshold_is_asked(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    assert len(_summary_prompts(fake)) == 1


def test_E2_clean_tree_at_the_same_fill_is_not_asked(tmp_path):
    """A commit under the branch: the rework is compacted as KC-67 does it."""
    scr = {"turns": [{"on_prompt": work_no_test, "events": ["busy", "idle"],
                      "message_info": _fill(HIGH)}, READY]}
    sb, fake, run = _go(tmp_path, [scr])
    assert run.state is AgentState.READY, run.last_error
    assert not _summary_prompts(fake)
    assert sum(p.endswith("/summarize") for p in _posts(fake)) == 1


def test_E3_second_crossing_in_the_same_attempt_is_not_asked(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), edit(HIGH), READY]}])
    assert run.state is AgentState.READY, run.last_error
    assert len(_summary_prompts(fake)) == 1
    assert sum(p.endswith("/summarize") for p in _posts(fake)) == 2


def test_E4_under_the_summary_threshold_only_compacts(tmp_path):
    """85 %: past `compact_at_percent`, under `summary_at_percent`."""
    sb, fake, run = _go(tmp_path, [{"turns": [edit(MID), READY]}])
    assert run.state is AgentState.READY, run.last_error
    assert not _summary_prompts(fake)
    assert sum(p.endswith("/summarize") for p in _posts(fake)) == 1


def test_E5_zero_turns_it_off(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), READY]}],
                        _cfg(tmp_path, summary_at_percent=0))
    assert run.state is AgentState.READY, run.last_error
    assert not _summary_prompts(fake)
    assert sum(p.endswith("/summarize") for p in _posts(fake)) == 1


def test_E6_an_unsized_model_is_not_asked(tmp_path):
    """No size at all (fallback 0): no fill, so neither a summary nor a compact."""
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), READY]}],
                        _cfg(tmp_path, context_limit_fallback=0))
    assert run.state is AgentState.READY, run.last_error
    assert not _summary_prompts(fake)
    assert not any(p.endswith("/summarize") for p in _posts(fake))


# ── P: the prompt, the copy, the records ────────────────────────────────────

def test_P1_the_prompt_is_the_tickets_question():
    text = _prompt_text().lower()
    assert "summary" in text and "commit" in text
    assert "do not modify" in text


def test_P2_summary_prompt_goes_before_the_compact_in_the_same_session(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    assert run.state is AgentState.READY, run.last_error
    (s1,) = fake.sessions()
    posts = _posts(fake)
    prompts = [i for i, p in enumerate(posts) if p == f"/session/{s1.id}/prompt_async"]
    summarize = posts.index(f"/session/{s1.id}/summarize")
    # initial, summary, continue — the summary prompt is the second one
    assert len(prompts) == 3
    assert _prompts(fake)[1][1].strip() == _prompt_text().strip()
    assert prompts[1] < summarize < prompts[2]
    assert len(_session_posts(fake)) == 1


def test_P3_reply_is_copied_to_the_summary_file(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    assert _summary_files(sb), "no agent-a.summary.md under out_dir"
    assert NOTE_1 in _summary_file_text(sb)


def test_P4_summary_file_sits_in_out_dir_itself(tmp_path):
    """§3: `<agent>.summary.md` under `out_dir`, next to `<agent>.session.json`."""
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    path = sb.out_dir / "agent-a.summary.md"
    assert path.is_file() and NOTE_1 in path.read_text(encoding="utf-8")


def test_P5_run_summary_goes_through_state_json(tmp_path):
    from tools.contest.runner import AgentRun
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    data = json.loads(json.dumps(run.to_dict()))
    assert NOTE_1 in json.dumps(data)
    back = AgentRun.from_dict(data)
    assert NOTE_1 in str(getattr(back, "summary", ""))


def test_P6_turns_jsonl_records_attempted_and_captured(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    rows = _rows(sb)
    hit = [r for r in rows if r.get("summary_attempted")]
    assert len(hit) == 1 and hit[0].get("summary_captured") is True
    assert all(r.get("summary_attempted") in (None, False) for r in rows if r is not hit[0])


def test_P7_the_prompt_after_a_capture_does_not_repeat_the_ticket(tmp_path):
    """§4: the next prompt of the same session carries one line, not the ticket."""
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    prompts = _prompts(fake)
    first, after = prompts[0][1], prompts[2][1]
    assert len(after) < len(first) / 2
    assert "summary" in after.lower()


def test_P8_summary_shown_in_the_summary_table(tmp_path):
    """§3 (KC-7): SUMMARY shows that the run has a summary."""
    from tools.contest.export import render_table
    from tools.contest.runner import AgentRun, RoundState
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), reply(NOTE_1), READY]}])
    back = AgentRun.from_dict(json.loads(json.dumps(run.to_dict())))
    state = RoundState(round_no=79, ticket="t", base_sha=sb.base_sha, started_at=0.0,
                       agents=[back])
    lines = render_table(state, [])
    text = "\n".join(lines) if not isinstance(lines, str) else lines
    assert "summary" in text.lower()


# ── F: the fallback and the carry ───────────────────────────────────────────

def _assert_reset(fake, run):
    assert run.state is AgentState.READY, run.last_error
    s1, s2 = fake.sessions()[:2]
    assert s1.id in _aborts(fake)
    posts = _posts(fake)
    assert posts.index(f"/session/{s1.id}/abort") < [i for i, p in enumerate(posts)
                                                     if p == "/session"][1]
    first_s2 = next(t for sid, t in _prompts(fake) if sid == s2.id)
    assert "pkg/thing.py" in first_s2
    return first_s2


def test_F1_empty_reply_takes_the_fresh_session(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), EMPTY]}, {"turns": [READY]}])
    assert len(_summary_prompts(fake)) == 1
    assert len(_session_posts(fake)) == 2
    _assert_reset(fake, run)


def test_F2_error_reply_takes_the_fresh_session(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), ERROR]}, {"turns": [READY]}])
    assert len(_summary_prompts(fake)) == 1
    assert len(_session_posts(fake)) == 2
    _assert_reset(fake, run)


def test_F3_failed_summary_is_recorded_not_captured(tmp_path):
    sb, fake, run = _go(tmp_path, [{"turns": [edit(HIGH), EMPTY]}, {"turns": [READY]}])
    hit = [r for r in _rows(sb) if r.get("summary_attempted")]
    assert len(hit) == 1 and hit[0].get("summary_captured") is False
    assert NOTE_1 not in _summary_file_text(sb)


def test_F4_captured_summary_reaches_the_next_session(tmp_path):
    """A capture, then KC-39's reset on a repeating last continue: the new
    session's opening prompt carries the summary text, not only file names."""
    s1 = {"turns": [edit(HIGH), reply(NOTE_1), same(), same(), same()]}
    sb, fake, run = _go(tmp_path, [s1, {"turns": [READY]}],
                        _cfg(tmp_path, max_continues_per_attempt=2))
    assert len(_session_posts(fake)) == 2, _posts(fake)
    s2 = fake.sessions()[1]
    first_s2 = next(t for sid, t in _prompts(fake) if sid == s2.id)
    assert NOTE_1 in first_s2
    assert "pkg/thing.py" in first_s2


def test_F5_summary_file_accumulates_across_sessions(tmp_path):
    """Two sessions of the same attempt each capture one: the file holds both.
    Three sessions allowed, so the second still has a fresh one to fall back on."""
    s1 = {"turns": [edit(HIGH), reply(NOTE_1), same(), same(), same()]}
    s2 = {"turns": [edit(HIGH), reply(NOTE_2), READY]}
    sb, fake, run = _go(tmp_path, [s1, s2, {"turns": [READY]}],
                        _cfg(tmp_path, max_continues_per_attempt=2,
                             max_sessions_per_attempt=3))
    text = _summary_file_text(sb)
    assert NOTE_1 in text and NOTE_2 in text
    assert text.index(NOTE_1) < text.index(NOTE_2)


def test_F6_summary_file_is_appended_not_rewritten(tmp_path):
    """A file already there (a previous run of the round) is kept."""
    sb = Sandbox(tmp_path)
    (sb.out_dir / "agent-a.summary.md").write_text("OLD-RUN-NOTE\n", encoding="utf-8")
    scripts = [dict({"turns": [edit(HIGH), reply(NOTE_1), READY]}, summary_tokens=3_000)]
    with _PerSession(scripts) as fake:
        Harness(sb, fake, _cfg(tmp_path)).go()
    text = _summary_file_text(sb)
    assert NOTE_1 in text and "OLD-RUN-NOTE" in text


# ── C: the config ───────────────────────────────────────────────────────────

def test_C1_contest_ini_names_the_key_above_the_compact():
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    summary = cp["contest"].getfloat("summary_at_percent")
    compact = cp["contest"].getfloat("compact_at_percent")
    assert summary is not None and summary > compact


def test_C2_the_roster_reads_the_key(tmp_path):
    from tools.contest.roster import load_roster
    ini = tmp_path / "contest.ini"
    ini.write_text("[contest]\nsummary_at_percent = 55\ngate_llm_profile = gate\n"
                   "[gate]\nbase_url = https://example.invalid/v1\napi_key = test-key\n"
                   "model = test/model\n[contest.agent.laguna]\nmodel = bynara/laguna-s-2-1\n",
                   encoding="utf-8")
    assert float(load_roster(ini).summary_at_percent) == 55.0
