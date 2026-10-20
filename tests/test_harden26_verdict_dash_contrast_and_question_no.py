"""tests/test_harden26_verdict_dash_contrast_and_question_no.py — HARDEN-26 A / B: a dash before
the contrast and a `No` that answers an approval head are no clause boundaries for the HARDEN-17
A reader, so both were confident approvals with `unparseable` `False` — not even the fail-open
warning was logged.

A: `_EN_CLAUSE_CAVEAT` took a contrast word that opens a clause after a stop, a comma, a
semicolon, a colon, `!`, `?` or a newline, and `Approved but …` with no mark at all. A dash is
how the GPT-family reviewers write that turn, so "APPROVED — but the date is wrong." and "OK — but
the seed entry is gone." read as `Approved` on the first clause, and the objection after the dash
was never read. An opening parenthesis is the other boundary — "APPROVED (no issues)" approves.
A dash that does not open a contrast keeps the approval.

B: "Approved? No — the test is wrong." and "OK? Not really, the date is wrong." were read on their
first word. The answer to the question is the verdict, so `No`, `Nope` and `Not really` after the
`?` reject with the clause after them as the reason; `Approved? Yes.` and `OK? Sure.` keep the
head's approval.
"""

from __future__ import annotations

import configparser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.auto.inner_loop import LLMGate2Validator, _parse_verdict_soft


def _verdict(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    return approved, reason, unparseable


# ── Part A: a dash or a parenthesis before the contrast ─────────────────────

#: Each with the clause after the contrast as the reason — the objection, never the praise:
#: handing the Coder "APPROVED" or "OK" would send it to fix the good parts.
DASH_OR_PAREN_CONTRASTS = [
    ("APPROVED — but the date is wrong.", "the date is wrong."),
    ("APPROVED - but the date is wrong.", "the date is wrong."),
    ("APPROVED—but the date is wrong.", "the date is wrong."),
    ("APPROVED – but the date is wrong.", "the date is wrong."),
    ("APPROVED — however, the date is wrong.", "the date is wrong."),
    ("APPROVED — except the date is wrong.", "the date is wrong."),
    ("OK — but the seed entry is gone.", "the seed entry is gone."),
    ("OK - but the seed entry is gone.", "the seed entry is gone."),
    ("Looks good — but the ring is stolen.", "the ring is stolen."),
    ("Looks good - although the timeline breaks.", "the timeline breaks."),
    ("Approved — except the wedding is in the wrong year.", "the wedding is in the wrong year."),
    ("APPROVED (but the date is wrong)", "the date is wrong"),
    ("Looks good (though the timeline breaks)", "the timeline breaks"),
    ("No issues — but the ring is stolen.", "No issues — but the ring is stolen."),
    ("Nothing here contradicts the outline — but the ending fails.",
     "Nothing here contradicts the outline — but the ending fails."),
]


@pytest.mark.parametrize("reply,quoted", DASH_OR_PAREN_CONTRASTS)
def test_a_contrast_after_a_dash_or_parenthesis_is_a_rejection(reply, quoted):
    approved, reason, unparseable = _verdict(reply)
    assert approved is False, reply
    # parsed, not failed open: the fail-open default is what approved the chapter here
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)
    # the objection, never the praise
    assert "looks good" not in reason.lower(), (reply, reason)
    assert reason != "APPROVED", (reply, reason)


#: A dash or a parenthesis that does not open a contrast keeps the approval.
DASHES_WITHOUT_A_CONTRAST = [
    "Approved — looks good.",
    "APPROVED — no issues.",
    "OK - fine",
    "APPROVED (no issues)",
    "Approved — Anna is alive in chapter three.",
    "APPROVED — everything matches the bible.",
    "OK — the chapter reads well.",
    "APPROVED-no issues.",
]


@pytest.mark.parametrize("reply", DASHES_WITHOUT_A_CONTRAST)
def test_a_dash_that_does_not_open_a_contrast_keeps_the_approval(reply):
    approved, reason, unparseable = _verdict(reply)
    assert approved is True, reply
    assert unparseable is False, reply
    assert reason == "", (reply, reason)


# ── Part B: an approval head answered by `No` after a `?` ───────────────────

#: The answer to the question decides, and the clause after it is the reason.
QUESTION_NO_ANSWERS = [
    ("OK?? No, the date is wrong.", "the date is wrong."),
    ("Approved? No — the test is wrong.", "the test is wrong."),
    ("Approved? No, the test is wrong.", "the test is wrong."),
    ("APPROVED? No: the date is wrong.", "the date is wrong."),
    ("OK? No, the date is wrong.", "the date is wrong."),
    ("OK? Not really, the date is wrong.", "the date is wrong."),
    ("OK? Nope, the seed entry is gone.", "the seed entry is gone."),
    ("Looks good? No, the ring is stolen.", "the ring is stolen."),
    ("Approved? No.", "Approved? No."),
]


@pytest.mark.parametrize("reply,quoted", QUESTION_NO_ANSWERS)
def test_an_approval_question_answered_in_the_negative_rejects(reply, quoted):
    approved, reason, unparseable = _verdict(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)


#: A positive answer keeps the head's approval.
QUESTION_YES_ANSWERS = [
    "Approved? Yes.",
    "OK? Sure.",
    "Approved? Yes, the timeline is intact.",
]


@pytest.mark.parametrize("reply", QUESTION_YES_ANSWERS)
def test_an_approval_question_answered_in_the_affirmative_approves(reply):
    approved, reason, unparseable = _verdict(reply)
    assert approved is True, reply
    assert unparseable is False, reply
    assert reason == "", (reply, reason)


# ── Layer 2: the operator ───────────────────────────────────────────────────

def _creative_gate(reply: str) -> tuple[bool, str]:
    """Creative Gate 2 against a validator stubbed to reply *reply*."""
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
        "validator_agent": {"temperature": "0.1", "max_hints": "3", "max_tokens": "256"},
        "coder": {"context_probe": "false"},
        "loop": {"timeout_seconds": "30"},
    })
    validator = LLMGate2Validator(
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
    task = {"id": "harden26", "instruction": "Write chapter 3.", "target_files": ["chapter_03.md"]}
    with (
        patch("tools.llm_stream.request_completion", return_value=reply),
        patch.object(validator, "_read_changed_content", return_value="(prose content)"),
    ):
        return validator.approve(
            task,
            SimpleNamespace(passed=True, exit_code=0, stdout="", stderr=""),
            SimpleNamespace(succeeded=True, files_written=["chapter_03.md"]),
            base_dir="/tmp",
        )


@pytest.mark.parametrize("reply", [
    "APPROVED — but the date is wrong.",
    "Approved? No — the test is wrong.",
])
def test_a_creative_run_returns_for_a_revision_on_a_dash_or_question_rejection(reply):
    """The chapter is not committed: the objection comes back for another revision."""
    ok, feedback = _creative_gate(reply)
    assert ok is False, reply
    assert "unavailable" not in feedback.lower(), (reply, feedback)
    assert feedback, reply


@pytest.mark.parametrize("reply", [
    "APPROVED — no issues.",
    "Approved? Yes.",
])
def test_a_creative_run_ships_a_clean_approval(reply):
    ok, feedback = _creative_gate(reply)
    assert ok is True, reply
    assert feedback == "", (reply, feedback)


def test_a_long_run_of_dashes_and_parentheses_is_read_in_linear_time():
    """A dash / parenthesis boundary must not backtrack: a thousand of them parse at once."""
    import time

    for reply in ("OK " + "- " * 3000 + "ok", "Approved " + "— " * 2000 + "but x", "Approved (" * 500):
        t0 = time.monotonic()
        _parse_verdict_soft(reply)
        assert time.monotonic() - t0 < 2.0
