"""tools/contest/policy.py: a file ask names its file relative to the worktree, and a gate reply cut by max_tokens is not a lost verdict.

Round 46 (KC-7) sent every `edit` of `AGENTS.md` to the gate: Kilo asks with
`patterns: ["AGENTS.md"]` and the absolute path only in `metadata.filepath`,
and `_pathlike` — the rule that keeps a bare command word like `reboot` from
reading as a file in the worktree — dropped both, so geometry saw no path at
all. The gate (`hy3:free`, `max_tokens = 256`) then spent the whole budget
thinking and answered `""` or a `{"verdict": "allow", "reason": "…` cut
mid-string, and the edit was refused: 17 of the round's 19 gate asks, one
entry shipped without its `AGENTS.md` line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(TESTS_DIR.parent), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_policy import (  # noqa: E402
    ALLOW,
    NO_SLEEP,
    FakeClock,
    StubGate,
    decide,
    make_config,
)
from tools.contest import policy as policy_mod  # noqa: E402
from tools.contest.policy import GATE_RETRY_MIN_TOKENS, Policy  # noqa: E402


class NoGate(StubGate):
    """Geometry must settle it: any call to the gate fails the test."""

    def __init__(self):
        super().__init__(AssertionError("the gate was asked"))


class SeqGate(StubGate):
    """One reply per call, in order."""

    def __init__(self, *replies):
        super().__init__(None)
        self.replies = list(replies)

    def __call__(self, url, headers, payload, timeout, **kwargs):
        self.reply = self.replies[len(self.calls)]
        return super().__call__(url, headers, dict(payload), timeout, **kwargs)


def file_event(worktree: Path, pattern: str, *, permission="edit", filepath=True,
               directories=None) -> dict:
    """The ask as round 46 recorded it: relative pattern, absolute filepath."""
    meta = {"configProtected": True, "disableAlways": True}
    if filepath is True:
        meta["filepath"] = str(worktree / pattern)
    elif filepath:
        meta["filepath"] = filepath
    if directories is not None:
        meta["directories"] = directories
    return {"type": "permission.asked", "properties": {
        "id": "per_kc46", "sessionID": "ses_kc46", "permission": permission,
        "patterns": [pattern], "always": ["*"], "metadata": meta,
        "tool": {"messageID": "msg_kc46", "callID": "call_kc46"}}}


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "rounds" / "46-agent"
    (worktree / "tools" / "contest").mkdir(parents=True)
    (worktree / "AGENTS.md").write_text("# repo\n")
    return worktree


# ── the file ask is geometry's ────────────────────────────────────────────

@pytest.mark.parametrize("pattern", ["AGENTS.md", "tools/contest/cli.py", "tests/new_file.py"])
def test_an_edit_inside_the_worktree_is_mechanical(tmp_path, pattern):
    worktree = _worktree(tmp_path)
    policy = Policy(make_config(), completion_fn=NoGate(), clock=FakeClock())

    decision = decide(policy, file_event(worktree, pattern), worktree)

    assert (decision.reply, decision.layer) == ("once", "mechanical")


def test_the_relative_pattern_alone_is_enough(tmp_path):
    """No `metadata.filepath`: the pattern is joined to the worktree."""
    worktree = _worktree(tmp_path)
    policy = Policy(make_config(), completion_fn=NoGate(), clock=FakeClock())

    decision = decide(policy, file_event(worktree, "AGENTS.md", filepath=False), worktree)

    assert (decision.reply, decision.layer) == ("once", "mechanical")


def test_the_openrouter_backends_write_is_mechanical(tmp_path):
    """`backend.py` asks `write` with the model's relative path in both places."""
    worktree = _worktree(tmp_path)
    policy = Policy(make_config(), completion_fn=NoGate(), clock=FakeClock())
    event = file_event(worktree, "pkg/x.py", permission="write", filepath=False,
                       directories=["pkg/x.py"])

    decision = decide(policy, event, worktree)

    assert (decision.reply, decision.layer) == ("once", "mechanical")


def test_a_relative_pattern_that_climbs_out_goes_to_the_gate(tmp_path):
    worktree = _worktree(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, file_event(worktree, "tools/../../other/x.py", filepath=False),
                      worktree)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1


def test_a_filepath_outside_the_worktree_is_not_excused_by_its_pattern(tmp_path):
    worktree = _worktree(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = file_event(worktree, "AGENTS.md", filepath=str(tmp_path / "elsewhere" / "AGENTS.md"))

    decision = decide(policy, event, worktree)

    assert decision.layer == "gate"


def test_a_forbidden_filepath_is_still_refused(tmp_path):
    worktree = _worktree(tmp_path)
    policy = Policy(make_config(), completion_fn=NoGate(), clock=FakeClock())
    event = file_event(worktree, "id_rsa", filepath=str(Path.home() / ".ssh" / "id_rsa"))

    decision = decide(policy, event, worktree)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "forbidden" in decision.reason


@pytest.mark.parametrize("pattern", ["kilo.json", "opencode.jsonc", "sub/kilo.jsonc",
                                     ".kilo/agent/code.md", ".kilocode/rules.md"])
def test_kilos_own_config_in_the_worktree_stays_the_gates(tmp_path, pattern):
    """What Kilo marks configProtected, AGENTS.md aside, can change what Kilo allows."""
    worktree = _worktree(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, file_event(worktree, pattern), worktree)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1


@pytest.mark.parametrize("permission,pattern", [("task", "general"), ("webfetch", "example.com"),
                                                ("bash", "reboot")])
def test_a_bare_word_of_any_other_ask_is_still_no_path(tmp_path, permission, pattern):
    worktree = _worktree(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = file_event(worktree, pattern, permission=permission, filepath=False)

    decision = decide(policy, event, worktree)

    assert decision.layer != "mechanical" or "inside worktree" not in decision.reason


# ── the gate's reply ──────────────────────────────────────────────────────

def _gate_ask(tmp_path, gate):
    worktree = _worktree(tmp_path)
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock(), sleep=NO_SLEEP)
    event = file_event(worktree, "x", filepath=str(tmp_path / "outside" / "x"))
    return decide(policy, event, worktree)


def test_a_verdict_cut_mid_reason_is_a_verdict(tmp_path):
    cut = '{"verdict": "allow", "reason": "AGENTS.md is inside the agent\'s worktree and is'
    gate = StubGate(cut)

    decision = _gate_ask(tmp_path, gate)

    assert (decision.reply, decision.layer) == ("once", "gate")
    assert "inside the agent's worktree and is … (cut)" in decision.reason
    assert len(gate.calls) == 1


def test_a_reject_cut_before_its_reason_is_a_reject(tmp_path):
    decision = _gate_ask(tmp_path, StubGate('{"verdict": "reject",'))

    assert (decision.reply, decision.layer) == ("reject", "gate")


@pytest.mark.parametrize("cut", ['{"verdict": "', '{"verdict": "allo', '{"reason": "fine", "verd',
                                 'I think {"verdict"'])
def test_no_whole_verdict_is_asked_again(tmp_path, cut):
    gate = SeqGate(cut, json.dumps(ALLOW))

    decision = _gate_ask(tmp_path, gate)

    assert (decision.reply, decision.layer) == ("once", "gate")
    assert len(gate.calls) == 2


def test_the_retry_has_room_to_finish(tmp_path):
    """round 46: 256 tokens came back empty twice; the retry gets 2048."""
    gate = SeqGate("", json.dumps(ALLOW))

    decision = _gate_ask(tmp_path, gate)

    assert decision.reply == "once"
    assert gate.calls[0]["payload"]["max_tokens"] == 256
    assert gate.calls[1]["payload"]["max_tokens"] == GATE_RETRY_MIN_TOKENS == 2048


def test_the_retry_budget_widens_ollamas_num_predict_and_never_shrinks():
    ollama = {"options": {"num_predict": 256, "temperature": 0.0}}
    big = {"max_tokens": 4096}
    none = {"model": "m"}

    for payload in (ollama, big, none):
        policy_mod._roomier(payload)

    assert ollama["options"]["num_predict"] == 2048
    assert big["max_tokens"] == 8192
    assert none == {"model": "m"}
