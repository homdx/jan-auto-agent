"""tests_bugfix/test_harden10_verdict_not_family_308.py — HARDEN-10 C: a NOT is a rejection
only when the word it negates is a verdict on the negative side.

HARDEN-5 A took the whole family: every reply that began with "NOT" or "NOTHING" was a
rejection except three approvals, so "Nothing to flag", "Nothing found", "Nothing
contradicts the story bible", "Not a problem", "Not contradictory" and "NOT REJECTED" all
went back to the Coder as a fix request. The reason behind them was the word "NOT" was
negating: "NOT APPROVED" and "NOT GOOD" negate an approval, while "NOT REJECTED" and
"NOTHING FOUND" negate a problem — the same way "NOT BAD" reads as an approval.

A separate old gap: "No contradictions found", "No errors", "No inconsistencies" and
"No conflicts" were read as a standalone NO rejection whose reason was the problem word it
negated ("contradictions found."), which is what the Coder was told to fix.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.inner_loop import LLMGate2Validator, _parse_verdict_soft  # noqa: E402


class TestAProblemNegatedIsAnApproval:
    """HARDEN-10 C: `NOT <problem>` and `NOTHING <problem>` approve."""

    @pytest.mark.parametrize("reply", [
        "Nothing to flag",
        "Nothing found",
        "Nothing contradicts the story bible",
        "Nothing to report",
        "Not a problem",
        "Not a contradiction",
        "Not an issue",
        "Not contradictory",
        "Not rejected",
        "NOT REJECTED",
        "NOTHING TO FLAG",
        "NOTHING FOUND",
        "NOTHING CONTRADICTS the story bible",
        "NOTHING WRONG",
        "NOT WRONG",
        "Nothing to correct",
    ])
    def test_it_is_an_approval(self, reply):
        approved, reason, _ = _parse_verdict_soft(reply)
        assert approved is True
        # a rejection's reason is the whole reply: never that on an approval
        assert reason.strip() != reply.strip()

    @pytest.mark.parametrize("reply", [
        "Nothing to flag",
        "Nothing found",
        "Not a problem",
        "Not contradictory",
        "NOT REJECTED",
    ])
    def test_a_rejection_would_be_a_wasted_iteration(self, reply):
        # On 2c5bd52 every one of these was a rejection, and a creative-mode rejection goes
        # back to the Coder as a fix request.
        assert _parse_verdict_soft(reply)[0] is True


class TestANoProblemIsAnApproval:
    """HARDEN-10 C: `No <contradiction|error|inconsistency|conflict|…>` approves.

    The standalone NO token used to take the first problem word away as the reason, so the
    Coder was told to fix "contradictions found" on a chapter with none."""

    @pytest.mark.parametrize("reply", [
        "No contradictions found",
        "No errors",
        "No errors found",
        "No inconsistencies",
        "No conflicts",
        "No violations",
        "No objections",
        "No issues",
        "No problems",
        "No flaws",
        "No omissions",
        "No ambiguities",
        "No defects",
        "No contradictions remain",
        "NO CONTRADICTIONS FOUND",
    ])
    def test_it_is_an_explicit_approval(self, reply):
        approved, reason, unparseable = _parse_verdict_soft(reply)
        assert approved is True
        assert reason == ""
        # matched, not failed open: an unparseable reply approves by accident
        assert unparseable is False

    @pytest.mark.parametrize("reply, word", [
        ("No contradictions found", "contradictions"),
        ("No errors", "errors"),
        ("No inconsistencies", "inconsistencies"),
        ("No conflicts", "conflicts"),
    ])
    def test_the_problem_word_is_not_the_reason(self, reply, word):
        # The old reason was the negated word itself, which read as a request to fix it.
        assert word not in _parse_verdict_soft(reply)[1]


class TestTheRejectionsOfHARDEN5AStay:
    """A NOT that negates a verdict on the approval side still rejects."""

    @pytest.mark.parametrize("reply", [
        "NOT APPROVED",
        "NOT OK",
        "NOT GOOD",
        "NOT ACCEPTABLE",
        "NOT VALID",
        "NOT CONSISTENT",
        "NOT CORRECT",
        "NOT COMPLETE",
        "Not good enough: tighten the prose",
        "Not complete",
        "Not fixed",
        "NOT RECOMMENDED",
        "Nothing works",
    ])
    def test_still_a_rejection(self, reply):
        approved, reason, unparseable = _parse_verdict_soft(reply)
        assert approved is False
        assert unparseable is False
        # the whole reply is the reason, never the word after NOT: "NOT APPROVED" ->
        # "APPROVED" would tell the Coder to fix an approval
        assert reason.strip() == reply.strip()

    def test_the_rejection_of_a_multi_word_reply_keeps_the_critique(self):
        reply = "Not good enough: tighten the prose"
        approved, reason, _ = _parse_verdict_soft(reply)
        assert approved is False
        assert "tighten the prose" in reason


class TestTheApprovalsOfHARDEN5AStay:
    """The three NOT-family approvals HARDEN-5 A kept are untouched."""

    @pytest.mark.parametrize("reply", [
        "NOT BAD",
        "NOTHING TO ADD",
        "NOTHING TO CHANGE",
        "NOTHING TO CORRECT",
    ])
    def test_still_an_approval(self, reply):
        assert _parse_verdict_soft(reply)[0] is True

    def test_a_note_is_prose(self):
        approved, _, unparseable = _parse_verdict_soft("NOTE: this is a note")
        assert approved is True
        assert unparseable is True

    @pytest.mark.parametrize("reply", ["Normally fine", "NOPE", "Nope, works"])
    def test_a_word_that_only_begins_with_no_is_prose(self, reply):
        assert _parse_verdict_soft(reply)[0] is True


class TestTheNoProblemApprovalOfTheRussianBranchIsUnchanged:
    @pytest.mark.parametrize("reply", [
        "нет противоречий",
        "противоречий нет",
        "не противоречит",
        "не вижу противоречий",
        "не против",
    ])
    def test_the_russian_no_problem_is_still_an_approval(self, reply):
        assert _parse_verdict_soft(reply)[0] is True


class TestTheRussianRejectionsAreUnchanged:
    @pytest.mark.parametrize("reply", [
        "есть противоречия",
        "ошибка",
        "не согласен",
        "против",
        "нужно исправить",
    ])
    def test_still_a_rejection(self, reply):
        assert _parse_verdict_soft(reply)[0] is False


class TestAVerdictBehindAPreamble:
    @pytest.mark.parametrize("reply, expected", [
        ("Let me think.\nNothing to flag", True),
        ("Looking over it...\nNothing found", True),
        ("Here is my read.\nNo contradictions found", True),
        ("Here is my read.\nNot contradictory", True),
        ("Let me think.\nNOT APPROVED", False),
        ("Let me think.\nNothing works", False),
    ])
    def test_found_on_a_later_line(self, reply, expected):
        assert _parse_verdict_soft(reply)[0] is expected


def _make_validator() -> LLMGate2Validator:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:11434",
            "api_key": "test",
            "model": "llama3.1:8b",
            "api_format": "ollama",
            "num_ctx": "4096",
        },
        "validator_agent": {"temperature": "0.1", "max_hints": "3",
                            "max_tokens": "256"},
        "coder": {"context_probe": "false"},
        "loop": {"timeout_seconds": "30"},
    })
    return LLMGate2Validator(
        base_url="http://localhost:11434",
        model="llama3.1:8b",
        api_key="test",
        api_format="ollama",
        temperature=0.1,
        timeout=30,
        max_hints=3,
        task_mode="creative",
        config=cfg,
    )


_TASK = {"id": "cr-not-family", "instruction": "Write chapter 3.",
         "target_files": ["chapter_03.md"]}
_EXEC = SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="")
_CODER = SimpleNamespace(succeeded=True, files_written=["chapter_03.md"])


def _approve_with_raw(raw_reply: str) -> tuple[bool, str]:
    """The creative Gate 2 against a model stubbed to reply *raw_reply*."""
    validator = _make_validator()
    with (
        patch("tools.llm_stream.request_completion", return_value=raw_reply),
        patch.object(validator, "_read_changed_content", return_value="(prose)"),
    ):
        return validator.approve(_TASK, _EXEC, _CODER, base_dir="/tmp")


class TestTheGateDoesNotWasteAnIteration:
    """End to end: the creative Gate 2 approves these, so the Coder is not called again."""

    @pytest.mark.parametrize("reply", [
        "Nothing to flag",
        "Nothing contradicts the story bible",
        "Not a problem",
        "NOT REJECTED",
        "No contradictions found",
        "No errors",
        "No inconsistencies",
    ])
    def test_the_gate_approves_it(self, reply):
        ok, _ = _approve_with_raw(reply)
        assert ok is True

    @pytest.mark.parametrize("reply", ["NOT APPROVED", "Nothing works"])
    def test_the_gate_still_rejects(self, reply):
        ok, _ = _approve_with_raw(reply)
        assert ok is False
