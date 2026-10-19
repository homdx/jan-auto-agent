"""tests/test_harden22_verdict_nothing_denials.py — HARDEN-22 A / B / C: a clean chapter went
back to be "fixed" with its own approval as the reason, one rejection shipped, and a directive
after a period was invisible.

A: HARDEN-14 A made a reply that opens with `NOT` / `NOTHING` a rejection unless a problem
word follows directly, so the adjectives and auxiliaries a real validator writes in between
were read as the verdict — "Nothing major was found.", "Nothing else to add.", "Nothing is
wrong.", "Nothing needs fixing." and "Not seeing any issues." all rejected, and the reason
handed to the Coder was the approval itself. The adjectives of `_EN_NO_PROBLEM_ADJ`, the
problem words of `_EN_NOT_PROBLEM_WORDS` and the actions of `_EN_NOTHING_OK` now decide the
whole family in one place; no second word list sits beside them.

B: a `REVISE:` on the approval's own line was not seen at all — only a later **line** was
scanned — so "No contradictions. REVISE: fix the timeline." approved the chapter. The scan
now starts at a sentence start (after `.`, `!`, `?`, `;` or a line start) and reads one word
list, `_EN_REJECT_HEAD`, so `REJECTED:` / `Rejected -` / `Needs revision:` are found the way
`REVISE:` already was.

C: `Incorrect:` and a bare `Nope` have no verdict token, so they were passed on fail-open as
approvals. They reject now, the reason being what follows, or the reply itself for `Nope`.

Two judgement calls are pinned here rather than guessed at: `Not sure` stays a rejection
(a hedged verdict is not an approval, and the continuity gate cannot re-ask), and
`Incorrectly formatted but fine` stays a fail-open approval (`INCORRECTLY` is not the token
`INCORRECT`, and the objection is followed by the word that withdraws it).
"""

from __future__ import annotations

import configparser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.auto.continuity_validator import ContinuityValidator
from tools.auto.inner_loop import LLMGate2Validator, _parse_verdict_soft


# ── A: the NOTHING / NOT denials a validator writes for a clean chapter ────────

#: Every line of the ticket's table, plus the same shapes spelled differently. The reason is
#: empty or at least never opens with the comma the old `NO` token left behind.
NOTHING_DENIALS = [
    "Nothing major was found.",
    "Nothing serious detected.",
    "Nothing else to add.",
    "Nothing to revise.",
    "Nothing is wrong.",
    "Nothing needs fixing.",
    "Not seeing any issues.",
    "Nothing is out of character.",
    "NOTHING IS WRONG HERE.",
    "NOTHING IS INCORRECT.",
    "NOTHING SEEMS WRONG.",
    "NOTHING IS PROBLEMATIC.",
    "Nothing looks inconsistent.",
    "Nothing appears wrong.",
    "Nothing seems problematic.",
    "Nothing is amiss.",
    "Nothing looks amiss.",
    "Nothing notable was found.",
    "Nothing else to fix.",
    "Nothing more to add.",
    "Nothing else to change.",
    "Nothing else needs fixing.",
    "Nothing needs to be fixed.",
    "Nothing to be corrected.",
    "Nothing else needs work.",
    "NOTHING TO REVISE.",
    "Nothing is out of line.",
    "Nothing is out of context.",
]

#: The one the ticket calls out by name for its mangled reason: the leading `No,` is the
#: filler, not the verdict.
NO_COMMA_DENIALS = [
    "No, nothing is wrong with it.",
    "No, nothing else to add.",
    "No, not seeing any issues.",
]


@pytest.mark.parametrize("reply", NOTHING_DENIALS + NO_COMMA_DENIALS)
def test_a_nothing_denial_is_an_approval(reply):
    approved, reason, _unparseable = _parse_verdict_soft(reply)
    assert approved is True, reply
    # the approval itself is never handed back as something to fix
    assert reason.strip() != reply.strip(), (reply, reason)
    # "No, nothing is wrong" used to keep the comma of the filler as its reason
    assert not reason.lstrip().startswith(","), (reply, reason)


# ── A: the HARDEN-14 A rejections that must stay rejections ───────────────────

#: HARDEN-14 A's own table: a quality adjective or an approval word after the copula is the
#: complaint, not a denied problem.
HARDEN14_REJECTIONS = [
    "Nothing matches",
    "NOT APPROVED",
    "Not ready",
    "Not coherent",
    "NOTHING IS RIGHT",
    "NOTHING IS PERFECT",
    "Nothing works",
    "Nothing matches the bible",
    "Nothing is correct",
    "Nothing is fine",
    "Nothing is good",
    "Nothing looks right",
    "Nothing appears consistent",
    "NOTHING SEEMS PERFECT",
]


@pytest.mark.parametrize("reply", HARDEN14_REJECTIONS)
def test_a_copula_before_an_approval_word_still_rejects(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert reason.strip() == reply.strip(), (reply, reason)


@pytest.mark.parametrize("reply, quoted", [
    ("Nothing is wrong, but the ring is stolen.", "the ring is stolen"),
    ("Nothing is wrong. However, the timeline breaks.", "the timeline breaks"),
    ("Nothing is wrong; however the timeline breaks.", "the timeline breaks"),
    ("Nothing else to add, but the ending is rushed.", "the ending is rushed"),
    ("Nothing is wrong, except one: the date changes.", "the date changes"),
])
def test_a_denial_that_goes_on_with_a_contrast_rejects(reply, quoted):
    """Part B's caveat rule applies to the denials too: they are not parsed approvals."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert quoted in reason, (reply, reason)


# ── B: a directive after a stop on the approval's own line ─────────────────────

#: Each with the words of the objection: the reason is the directive's sentence, not the
#: approval that preceded it.
DIRECTIVES_AFTER_A_STOP = [
    ("No contradictions. REVISE: fix the timeline.", "fix the timeline"),
    ("No errors. REJECT: wrong character arc.", "wrong character arc"),
    ("No issues; REVISE: fix X.", "fix X"),
    ("No problems? REVISE: check chapter 2.", "check chapter 2"),
    ("No issues. Rejected: the date breaks.", "the date breaks"),
    ("No issues. REJECTED: the date breaks.", "the date breaks"),
    ("No issues. Rejected - the date breaks.", "the date breaks"),
    ("No issues. Needs revision: the date breaks.", "the date breaks"),
    ("No issues. Needs work: pacing.", "pacing"),
    ("No issues. Rewrite: scene 3.", "scene 3"),
    ("No issues. Failed: the ring is missing.", "the ring is missing"),
    ("No issues. DISAPPROVED: the ring is stolen.", "the ring is stolen"),
    ("No issues found.\nREJECTED: the date breaks", "the date breaks"),
    ("No issues found.\nRejected - the date breaks", "the date breaks"),
    ("No issues found.\nNeeds revision: the date breaks", "the date breaks"),
]


@pytest.mark.parametrize("reply, quoted", DIRECTIVES_AFTER_A_STOP)
def test_a_directive_after_a_stop_is_the_verdict(reply, quoted):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    # parsed, not failed open: the fail-open default is what approved the chapter here
    assert unparseable is False, reply
    assert quoted in reason, (reply, reason)
    # the approval sentence is not something to fix
    assert "no contradictions" not in reason.lower(), (reply, reason)


@pytest.mark.parametrize("reply", [
    "No issues found; everything matches the bible.",
    "No contradictions found. The chapter only uses facts from the bible.",
    "No issues. Yet another consistent chapter.",
    "No issues found.\nThe chapter matches the bible.",
    "No issues. The chapter keeps every fact of the bible.",
    "No problems. The chapter keeps every fact of the bible.",
    "No discrepancies found.",
    "No factual errors found.",
    "No issues found.",
])
def test_a_clean_approval_that_goes_on_still_approves(reply):
    """A later sentence that adds nothing adverse is not a directive."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True, reply
    assert reason == "", (reply, reason)
    assert unparseable is False, reply


# ── C: the words that failed open as approvals ────────────────────────────────

@pytest.mark.parametrize("reply, quoted", [
    ("Incorrect: the date is wrong.", "the date is wrong"),
    ("INCORRECT: the date is wrong.", "the date is wrong"),
    ("Incorrect, the date is wrong.", "the date is wrong"),
    ("Incorrect. The date is wrong.", "The date is wrong"),
    ("Nope", "Nope"),
    ("Nope.", "Nope"),
    ("nope", "nope"),
    ("NOPE", "NOPE"),
])
def test_a_word_that_failed_open_is_a_rejection(reply, quoted):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)


def test_nope_in_all_caps_rejects_too():
    """HARDEN-1 A1 only forbade reading NOPE as a NO token with the tail `PE`; the word itself rejects."""
    approved, reason, unparseable = _parse_verdict_soft("NOPE")
    assert approved is False and unparseable is False
    assert reason == "NOPE"


def test_not_sure_stays_a_rejection():
    """Judgement call, pinned: a hedged verdict is not an approval, and the continuity gate
    has no second chance to ask again."""
    approved, reason, unparseable = _parse_verdict_soft("Not sure")
    assert approved is False
    assert unparseable is False
    assert reason == "Not sure"


def test_incorrectly_formatted_but_fine_stays_an_approval():
    """Judgement call, pinned: `INCORRECTLY` is not the token `INCORRECT`, and the objection
    is followed by the word that withdraws it."""
    approved, _reason, unparseable = _parse_verdict_soft("Incorrectly formatted but fine")
    assert approved is True
    assert unparseable is True


# ── Layer 2: the gates that have no second chance ─────────────────────────────

@pytest.mark.parametrize("reply", [
    "Nothing is wrong.",
    "Nothing else to add.",
    "Not seeing any issues.",
])
def test_the_continuity_gate_approves_a_clean_denial(reply):
    v = ContinuityValidator(lambda system, user: reply)
    verdict = v.check("Anna is 30 in chapter 1.", "Anna is 25 in chapter 3.")
    assert verdict.approved is True, reply
    assert verdict.unparseable is False, reply


@pytest.mark.parametrize("reply", [
    "No contradictions. REVISE: fix the timeline.",
    "Incorrect: the date is wrong.",
    "Nope",
])
def test_the_continuity_gate_rejects_without_a_second_chance(reply):
    """The continuity gate re-asks nothing, so an unparseable reply shipped the chapter."""
    v = ContinuityValidator(lambda system, user: reply)
    verdict = v.check("Anna is 30 in chapter 1.", "Anna is 25 in chapter 3.")
    assert verdict.approved is False, reply
    assert verdict.unparseable is False, reply
    assert verdict.feedback(), reply


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
    task = {"id": "harden22", "instruction": "Write chapter 3.", "target_files": ["chapter_03.md"]}
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


@pytest.mark.parametrize("reply", ["Nothing is wrong.", "Nothing else to add.", "Approved"])
def test_a_creative_run_ships_a_clean_denial(reply):
    """The ticket's Layer 2: the validator says there is nothing wrong, so the chapter ships."""
    ok, feedback = _creative_gate(reply)
    assert ok is True, reply
    assert feedback == "", (reply, feedback)


@pytest.mark.parametrize("reply", [
    "No contradictions. REVISE: fix the timeline.",
    "Incorrect: the date is wrong.",
    "Nope",
])
def test_a_creative_run_returns_for_a_revision_instead_of_shipping(reply):
    ok, feedback = _creative_gate(reply)
    assert ok is False, reply
    assert "unavailable" not in feedback.lower(), (reply, feedback)
    assert feedback, reply


@pytest.mark.parametrize("reply", [
    "Nothing needs to change.",
    "Nothing remains to be fixed.",
    "Not finding any issues.",
    "Nothing is missing.",
    "Nothing is lacking.",
    "Nothing is off.",
])
def test_more_ways_to_say_there_is_nothing_to_fix_approve(reply):
    """Review find on top of the entries: the same denials with a `TO`, `REMAINS TO BE`, FIND or a gap word."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True and reason == "" and unparseable is False, (reply, reason)


@pytest.mark.parametrize("reply", [
    "No issues. Not approved.",
    "No issues.\nNOT APPROVED: wrong date",
    "No contradictions. Please revise the ending.",
    "Nothing is wrong. Please reject the second scene.",
])
def test_an_approval_followed_by_not_approved_or_please_revise_rejects(reply):
    """Review find: `Not approved` and `Please revise` after an approval phrase are the verdict."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and reason.strip() and unparseable is False, (reply, reason)


@pytest.mark.parametrize("reply", ["Nope, works", "Nope, the date is wrong."])
def test_a_nope_with_words_after_it_stays_prose(reply):
    """HARDEN-10 C's pin: "Nope, works" may answer "any problems?", so only the bare word rejects."""
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True and unparseable is True
