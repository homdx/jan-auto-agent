"""KC-9 (round 48) acceptance bench — dropped into each entry's tests/ and run there."""
import json
from pathlib import Path

import pytest

from test_contest_runner import (  # noqa: F401
    _run_one, _prompts, _jsonl, make_config, work_ready, work_edit_no_commit, AgentState,
)
from tools.contest import runner as R


def _cfg(**over):
    kw = dict(error_retry_backoff_sec=0, max_rework=0)
    kw.update(over)
    return make_config(["agent-a"], **kw)


RUNNING = [{"tool": "bash", "status": "running", "input": {"command": "pytest"}}]
DONE = {"on_prompt": work_ready, "events": ["busy", "idle"], "assistant": "done"}


def _cont(fake):
    ps = _prompts(fake)
    return ps, [t for _, t in ps[1:]]


def test_names_exist():
    assert {k.name for k in R.IdleKind} >= {"FINISHED", "CUT", "SILENT"}
    assert "Continue exactly" in R.CONTINUE_PROMPT and "append_task.py" in R.CONTINUE_PROMPT


def test_cut_running_tool_continues_same_session_then_ready(tmp_path):
    sc = {"turns": [{"events": ["busy", "idle"], "tool_parts": RUNNING, "assistant": ""}, DONE]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    ps, rest = _cont(fake)
    assert run.state is AgentState.READY
    assert len(ps) == 2 and ps[0][0] == ps[1][0]
    assert rest[0] == R.CONTINUE_PROMPT
    assert run.attempt == 0
    assert getattr(run, "continues", None) == 1
    assert run.turns[0].get("idle_kind") in ("CUT", "cut")


def test_silent_idle_continues(tmp_path):
    sc = {"turns": [{"events": ["busy", "idle"], "assistant": ""}, DONE]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    ps, rest = _cont(fake)
    assert run.state is AgentState.READY and run.attempt == 0
    assert rest and rest[0] == R.CONTINUE_PROMPT
    assert str(run.turns[0].get("idle_kind")).upper() == "SILENT"


def test_info_error_is_cut(tmp_path):
    sc = {"turns": [{"events": ["busy", "idle"],
                     "assistant": "",
                     "message_info": {"error": {"name": "APIError", "data": {"message": "x"}}}},
                    DONE]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    ps, rest = _cont(fake)
    assert run.state is AgentState.READY
    assert rest and rest[0] == R.CONTINUE_PROMPT
    assert str(run.turns[0].get("idle_kind")).upper() == "CUT"


def test_finished_turn_sends_no_continue(tmp_path):
    sc = {"turns": [{"events": ["busy", "idle"], "assistant": "I am done."}]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    ps, rest = _cont(fake)
    assert R.CONTINUE_PROMPT not in rest
    assert run.state is AgentState.GAVE_UP
    assert "harvest" in run.turns[0]


def test_kc22_dirty_finished_turn_still_nudged(tmp_path):
    """Regression guard: text reply + uncommitted edits is KC-22's continue, not a harvest."""
    sc = {"turns": [{"on_prompt": work_edit_no_commit, "events": ["busy", "idle"],
                     "assistant": "edited"}, DONE]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    assert run.state is AgentState.READY and run.attempt == 0
    assert "harvest" not in run.turns[0]
    assert run.turns[1]["kind"] == "continue"


def test_budget_one_two_cuts_third_idle_harvested(tmp_path):
    cut = {"events": ["busy", "idle"], "tool_parts": RUNNING, "assistant": ""}
    sc = {"turns": [cut, cut, cut]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg(max_continues_per_attempt=1))
    ps, rest = _cont(fake)
    assert rest.count(R.CONTINUE_PROMPT) == 1
    assert run.turns[-1].get("continues_exhausted") is True
    assert "harvest" in run.turns[-1]


def test_silence_under_deadline_continues(tmp_path):
    sc = {"turns": [{"pause_before_idle_sec": 8}, DONE]}
    cfg = _cfg(idle_event_timeout_sec=3, turn_timeout_sec=300)
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, cfg)
    ps, rest = _cont(fake)
    assert run.state is AgentState.READY and run.attempt == 0
    assert len(ps) == 2 and ps[0][0] == ps[1][0]
    assert rest[0] == R.CONTINUE_PROMPT
    assert run.continues == 1
    t0 = run.turns[0]
    assert t0["idle_status"] == "stalled" and str(t0.get("idle_kind")).upper() == "SILENT"


def test_silence_twice_budget_spent_stalled(tmp_path):
    p = {"pause_before_idle_sec": 8}
    sc = {"turns": [p, p, p]}
    cfg = _cfg(idle_event_timeout_sec=3, max_continues_per_attempt=1)
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, cfg)
    ps, rest = _cont(fake)
    assert run.state is AgentState.STALLED
    assert rest.count(R.CONTINUE_PROMPT) == 1
    assert run.turns[-1].get("continues_exhausted") is True


def test_silence_spent_with_commit_harvested(tmp_path):
    p = {"pause_before_idle_sec": 8}
    cfg = _cfg(idle_event_timeout_sec=3, max_continues_per_attempt=0)
    sb, fake, _h, run, _ = _run_one(tmp_path, {"turns": [p]}, cfg,
                                    prepare=lambda d: work_ready(d, ""))
    assert "harvest" in run.turns[-1]


def test_turn_timeout_is_not_a_continue(tmp_path):
    sc = {"turns": [{"pause_before_idle_sec": 8}, DONE]}
    cfg = _cfg(idle_event_timeout_sec=0, turn_timeout_sec=3)
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, cfg)
    ps, rest = _cont(fake)
    assert run.state is AgentState.STALLED
    assert R.CONTINUE_PROMPT not in rest


def test_records_turns_and_state(tmp_path):
    sc = {"turns": [{"events": ["busy", "idle"], "tool_parts": RUNNING, "assistant": ""}, DONE]}
    sb, fake, _h, run, _ = _run_one(tmp_path, sc, _cfg())
    lines = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert all("idle_kind" in l for l in lines if l.get("idle_status") == "idle")
    assert any("continues" in l for l in lines)
    d = run.to_dict() if hasattr(run, "to_dict") else None
    if d is not None:
        assert d.get("continues") == 1
        assert R.AgentRun.from_dict(d).continues == 1


class _B:
    def __init__(self, msgs):
        self.msgs = msgs

    def messages(self, session):
        return self.msgs

    def last_assistant_text(self, session):
        for m in reversed(self.msgs):
            if m["info"].get("role") == "assistant":
                return "".join(p.get("text", "") for p in m["parts"] if p.get("type") == "text")
        return ""


def _msg(parts, **info):
    return [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]},
            {"info": {"role": "assistant", **info}, "parts": parts}]


def _tool(status):
    return {"type": "tool", "tool": "bash", "state": {"status": status}}


@pytest.mark.parametrize("parts,info,want", [
    ([{"type": "text", "text": "done"}], {}, "FINISHED"),
    ([_tool("running")], {}, "CUT"),
    ([_tool("pending")], {}, "CUT"),
    ([], {"error": {"name": "MessageOutputLengthError"}}, "CUT"),
    ([], {"finish": "length"}, "CUT"),
    ([], {}, "SILENT"),
])
def test_classify_idle_unit(parts, info, want):
    got = R.classify_idle(_B(_msg(parts, **info)), object(), 0.0)
    assert got.name == want
