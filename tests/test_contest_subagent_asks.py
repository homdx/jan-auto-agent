"""KC-71 — a subagent's permission and question are answered, and its work is not silence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_kilo_client as tk  # noqa: E402
from tools.contest.kilo_client import _child_session  # noqa: E402


#: round 87's ask, as mimo-v2-5's subagent made it at 301 s
TOOL_OUTPUT_ASK = {
    "permission": "external_directory",
    "patterns": ["/home/renat/.local/share/kilo/tool-output/*"],
    "metadata": {"filepath": "/home/renat/.local/share/kilo/tool-output/tool_x",
                 "parentDir": "/home/renat/.local/share/kilo/tool-output"},
}


def _wait(h, *, silence=None, timeout=60.0, on_permission=tk._reject, seen=None):
    def answer(event):
        if seen is not None:
            seen.append(event)
        return on_permission(event)

    h.client.prompt(h.session, "use the task tool")
    return h.client.wait_idle(h.tap, h.session, timeout, idle_event_timeout=silence,
                              on_permission=answer,
                              on_question=lambda event: None)


def _child_id(h) -> str:
    (child,) = [e["properties"]["sessionID"] for e in h.fake.events
                if e.get("type") == "session.created"
                and (e["properties"].get("info") or {}).get("parentID") == h.session.id]
    return child


def test_a_subagent_s_permission_is_answered_and_the_turn_ends_idle(tmp_path):
    """Round 87's mimo: without an answer the subagent waits for ever, and the
    agent waits on it — here the fake would hold the ask for its whole reply
    timeout and record it in `unanswered`."""
    scenario = {"turns": [{"events": ["busy"], "subagent": {"permission": TOOL_OUTPUT_ASK},
                           "assistant": "done"}]}
    seen: list = []
    with tk._probe(tmp_path, scenario, reply_timeout=20) as h:
        res = _wait(h, seen=seen)
        child = _child_id(h)
        replied = [e for e in h.fake.events if e.get("type") == "permission.replied"]

    assert res.status == "idle"
    assert h.fake.unanswered == []
    assert len(seen) == 1 and seen[0]["properties"]["sessionID"] == child
    assert seen[0]["properties"]["patterns"] == TOOL_OUTPUT_ASK["patterns"]
    assert [e["properties"]["reply"] for e in replied] == ["reject"]
    assert len(res.permissions) == 1, "the ask is the turn's, counted like the agent's own"


def test_the_reply_goes_to_the_subagent_s_session_on_the_legacy_route(tmp_path):
    scenario = {"permission_endpoint_404": True,
                "turns": [{"events": ["busy"], "subagent": {"permission": TOOL_OUTPUT_ASK},
                           "assistant": "done"}]}
    with tk._probe(tmp_path, scenario, reply_timeout=20) as h:
        res = _wait(h)
        child = _child_id(h)
        legacy = [r["path"] for r in h.fake.calls("POST") if "/permissions/" in r["path"]]

    assert res.status == "idle"
    assert len(legacy) == 1 and legacy[0].startswith(f"/session/{child}/permissions/")


def test_a_subagent_s_idle_does_not_end_the_agent_s_turn(tmp_path):
    """The child's own `session.idle` is the subagent done, not the turn: the
    wait goes on to the parent's idle and reads the parent's reply."""
    scenario = {"turns": [{"events": ["busy"], "subagent": {}, "delay": 0.5,
                           "assistant": "done"}]}
    with tk._probe(tmp_path, scenario) as h:
        res = _wait(h)
        texts = h.client.last_assistant_text(h.session)

    assert res.status == "idle" and texts == "done"


def test_a_subagent_s_question_is_rejected_like_the_agent_s_own(tmp_path):
    scenario = {"turns": [{"events": ["busy"],
                           "subagent": {"question": {"question": "which file?"}},
                           "assistant": "done"}]}
    with tk._probe(tmp_path, scenario, reply_timeout=20) as h:
        res = _wait(h)
        rejected = [r["path"] for r in h.fake.calls("POST") if r["path"].startswith("/question/")]

    assert res.status == "idle"
    assert h.fake.unanswered == []
    assert len(res.questions) == 1 and len(rejected) == 1


def test_a_working_subagent_keeps_the_silence_clock_from_firing(tmp_path):
    """The parent is silent for 3 s while its subagent beats `busy`: a 1 s
    silence clock that only read the parent would call that a stall."""
    scenario = {"turns": [{"events": ["busy"], "subagent": {"work_sec": 3},
                           "assistant": "done"}]}
    with tk._probe(tmp_path, scenario) as h:
        res = _wait(h, silence=1.0)

    assert res.status == "idle", res


def test_an_unrelated_session_s_ask_is_still_not_answered(tmp_path):
    """Only the agent's own subagents: a session that is not its child — a
    neighbour on the same server — is none of this wait's business."""
    scenario = {"turns": [{"events": ["busy"], "delay": 1.0, "assistant": "done"}]}
    with tk._probe(tmp_path, scenario) as h:
        seen: list = []
        h.fake._emit({"type": "session.created",
                      "properties": {"sessionID": "ses_other",
                                     "info": {"id": "ses_other", "parentID": "ses_nobody"}}})
        h.fake._emit({"type": "permission.asked",
                      "properties": {"id": "per_other", "sessionID": "ses_other",
                                     "permission": "bash", "patterns": ["ls"]}})
        res = _wait(h, seen=seen)

    assert res.status == "idle" and seen == []


def test_a_grandchild_is_the_agent_s_too():
    parents = {"ses_agent"}
    child = _child_session({"type": "session.created", "properties": {
        "sessionID": "ses_c", "info": {"id": "ses_c", "parentID": "ses_agent"}}}, parents)
    assert child == "ses_c"
    parents.add(child)
    assert _child_session({"type": "session.updated", "properties": {
        "info": {"id": "ses_g", "parentID": "ses_c"}}}, parents) == "ses_g"


@pytest.mark.parametrize("event", [
    {"type": "session.idle", "properties": {"sessionID": "ses_c"}},
    {"type": "session.created", "properties": {"sessionID": "ses_c"}},
    {"type": "session.created", "properties": {"info": {"id": "ses_c"}}},
    {"type": "session.created", "properties": {"info": {"id": "", "parentID": "ses_agent"}}},
    {"type": "session.created", "properties": {"info": {"id": "ses_c", "parentID": "ses_x"}}},
    {"type": "session.created", "properties": {"info": "not a dict"}},
    {"type": "session.created"},
])
def test_what_is_not_a_child(event):
    assert _child_session(event, {"ses_agent"}) is None
