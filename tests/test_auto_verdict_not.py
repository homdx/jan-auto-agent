"""tests/test_auto_verdict_not.py — HARDEN-5 A: a reply that begins with
"NOT …" is a rejection, not an unparseable reply that fails open to an approval.

HARDEN-1 A1 made the English verdict tokens match as whole words, which took the
rejection away from **every** reply beginning with "NOT": "NOT APPROVED",
"Not good enough" and "Nothing works" matched nothing, were unparseable — and
unparseable means approve — so a chapter the model rejected went through the
Gate. The NOT / NOTHING / NONSENSE rejections are back, and three explicit
NOT-family approvals (NOT BAD, NOTHING TO ADD, NOTE:) are matched first, so
they keep failing open to the later rules as prose.
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

from tools.auto.inner_loop import (  # noqa: E402
    LLMGate2Validator,
    _parse_verdict_soft,
)


# ── The ticket's table, row by row ───────────────────────────────────────────

# The first eight rows are the regressions A1 introduced; the next three are the
# NOT-family approvals the same fix must not take away; the last six are
# HARDEN-1 A1's whole-word rows, which the change must leave untouched.
TABLE = [
    # the regressions: a rejection that became an approval
    ("NOT APPROVED", False),
    ("Not good enough: pacing drags", False),
    ("NOT OK", False),
    ("Not complete", False),
    ("Not fixed", False),
    ("NOT RECOMMENDED", False),
    ("Nothing works", False),
    ("Nonsense", False),
    # the NOT-family approvals the fix must keep
    ("NOT BAD", True),
    ("Nothing to add", True),
    ("NOTE: fine", True),
    # HARDEN-1 A1's whole-word rows, unchanged
    ("NO: broken", False),
    ("No - bad", False),
    ("REVISE: x", False),
    ("Normally", True),
    ("NOPE", True),
]


@pytest.mark.parametrize("reply, expected", TABLE)
def test_the_ticket_table_row_by_row(reply, expected):
    approved, _, _ = _parse_verdict_soft(reply)
    assert approved is expected, reply


@pytest.mark.parametrize("reply", [reply for reply, exp in TABLE if not exp])
def test_a_rejection_is_not_unparseable(reply):
    # A recognised rejection must be parsed, not passed on fail-open: fail-open is
    # for rambling, and "unparseable" is exactly what approved the chapter here.
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False
    assert unparseable is False
    assert reason  # a rejection always carries its reason


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("NOT BAD", True),
        ("NOTHING TO ADD", True),
        ("Nothing to fix", True),
        ("NOTHING TO CHANGE", True),
        ("Nothing to improve", True),
        ("NOTHING WRONG", True),
        ("NOTHING TO CORRECT", True),
        ("Nothing to add, looks good", True),
    ],
)
def test_the_not_family_approvals_are_matches_before_the_rejection(reply, expected):
    # Matched before the NOT family: "NOT BAD" is an approval whose first token
    # is NOT, and it must not be read as "NOT" plus a bad reason.
    approved, _, unparseable = _parse_verdict_soft(reply)
    assert approved is expected
    # none of them is a parsed rejection: they are prose, so they fall through to
    # the fail-open rules, exactly as HARDEN-1 A1 made them do.
    if approved:
        assert unparseable is True


def test_the_reason_of_a_not_rejection_is_the_whole_reply():
    # The reason must not be a clipped word: "NOT APPROVED" → "APPROVED" would
    # hand the Coder the very thing the model said was fine.
    raw = "Not good enough: pacing drags in the second act"
    approved, reason, _ = _parse_verdict_soft(raw)
    assert approved is False
    assert reason == raw.strip()
    assert reason != "good enough: pacing drags in the second act"
    assert "Not good enough" in reason


def test_a_note_is_not_a_verdict():
    # "NOTE:" is a note, not a verdict: it is prose and fails open to approval,
    # and it must never be read as a NOT rejection.
    for reply in ("NOTE: fine", "Note: the pacing is a little slow"):
        approved, reason, unparseable = _parse_verdict_soft(reply)
        assert approved is True
        assert unparseable is True
        assert "NOTE" not in reason.upper()[:3]


# ── The verdict on a later line, behind a preamble ────────────────────────────


@pytest.mark.parametrize("reply, expected", [
    ("Some preamble\nNOT APPROVED", False),
    ("Looking over it...\nNot good enough: pacing drags", False),
    ("Let me think.\nNothing works", False),
    ("Here is my read.\nNonsense", False),
    # the NOT-family approvals are prose, so they fail open (unparseable=True)
    ("Fine.\nNOT BAD", True),
    ("Looking at it.\nNothing to add", True),
])
def test_a_verdict_on_a_later_line_behind_a_preamble(reply, expected):
    approved, _, unparseable = _parse_verdict_soft(reply)
    assert approved is expected
    # a parsed rejection is not unparseable; the approvals are prose, so they are
    if expected:
        assert unparseable is True
    else:
        assert unparseable is False


# ── End to end: the creative Gate 2, the model stubbed ────────────────────────


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


_TASK = {"id": "cr-not", "instruction": "Write chapter 3.",
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


def test_the_ticket_case_not_approved_is_a_rework_not_a_pass():
    # The ticket's end-to-end case: on 1fa8599 this returned approved=True, so the
    # chapter the model rejected went straight through Gate 2.
    reply = "Not approved: the chapter contradicts the story bible"
    ok, feedback = _approve_with_raw(reply)
    assert ok is False
    # the full critique is kept, not clipped to a word
    assert "contradicts" in feedback
    assert "story bible" in feedback


@pytest.mark.parametrize("reply", [
    "NOT APPROVED",
    "Not fixed",
    "Nothing works",
    "Nonsense",
])
def test_the_gate_rejects_every_form_of_the_regression(reply):
    ok, _ = _approve_with_raw(reply)
    assert ok is False


@pytest.mark.parametrize("reply", ["NOT BAD", "Nothing to add"])
def test_the_gate_still_approves_the_not_family_approvals(reply):
    ok, _ = _approve_with_raw(reply)
    assert ok is True


def test_the_gate_still_rejects_a_plain_no():
    ok, feedback = _approve_with_raw("NO: missing the confrontation scene")
    assert ok is False
    assert "confrontation" in feedback
