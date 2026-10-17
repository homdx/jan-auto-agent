"""tests/test_harden17_verdict_approval_and_reject_words.py — HARDEN-17 A / A2: a reply that
rejects, or that approves and then objects, was approved, and a clean approval went back to be
"fixed".

A: the parser returned on `APPROVED` and `OK` and failed open for everything else, so
"Looks good, but the ring is stolen" was approved on its first clause alone. `REJECT` matched
as a whole word only, so "REJECTED", "REJECTED - Anna is dead" and a bare "Rejected" matched
nothing at all, and "NEEDS REVISION" / "NEEDS WORK" have no token. The fail-open default is
approval, so each of these approved a chapter the validator had thrown out — and the
continuity, fact and theme gates re-ask nothing, so that approval was final.

A2: on the other side, the caveat rule took any `;`, `ONLY` or `YET` anywhere after the phrase
for a caveat, so a clean approval went back to the Coder with the approval sentence as the
reason to fix, and "No discrepancies found" / "No factual errors found" were rejected with the
problem word as the reason.

"NO <problem>" keeps one vocabulary: `_EN_PROBLEM_NOUNS` builds both the approval phrase and
the "the rest of the reply names a problem" signal, so the approval rule and the caveat rule
cannot disagree about what a problem is.

`No issues found, but all minor.` is a judgement call and it stays a rejection — the call is
pinned here, because the objection ("all minor") is the part after the conjunction and the
continuity gate cannot re-ask.
"""

from __future__ import annotations

import configparser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.auto.continuity_validator import ContinuityValidator
from tools.auto.inner_loop import LLMGate2Validator, _parse_verdict_soft


# ── A: a reply that opens with a rejection word is not approved ───────────────

#: Each with the words of the objection, when the reply carries any. The reason is the reply's
#: own words: a reason of "the ring is stolen" is what the Coder can act on, and a reason of
#: "Rejected" is not.
REJECTS_WITH_REASON = [
    ("Rejected: the timeline breaks.", "the timeline breaks"),
    ("REJECTED - Anna is dead", "Anna is dead"),
    ("Rejected. Anna is dead.", "Anna is dead"),
    ("Rejected, the timeline breaks.", "the timeline breaks"),
    ("Rejected — Anna is dead", "Anna is dead"),
    ("Needs revision: Anna is dead.", "Anna is dead"),
    ("Needs work: pacing.", "pacing"),
    ("Needs changes: chapter 2 contradicts the bible.", "chapter 2 contradicts the bible"),
    ("Needs a revision: the ring is stolen.", "the ring is stolen"),
    ("Needs a rewrite: the ring is stolen.", "the ring is stolen"),
    ("Needs to be rewritten: the ring is stolen.", "the ring is stolen"),
    ("Needs more work on the second act.", "the second act"),
    ("Rewrite: scene 3.", "scene 3"),
    ("Rewrote: scene 3.", "scene 3"),
    ("Failed: the chapter contradicts the bible.", "the chapter contradicts the bible"),
    ("FAILED. The chapter contradicts the bible.", "The chapter contradicts the bible"),
    ("DISAPPROVED: the ring is stolen.", "the ring is stolen"),
    ("reject: the ring is stolen.", "the ring is stolen"),
    ("revise: scene 3.", "scene 3"),
]

#: A rejection word with no reason of its own: the whole reply is the reason, as the NOT family
#: already does, so the Coder is never handed a verdict word.
BARE_REJECTS = [
    "Rejected", "REJECTED", "rejected.", "Rejected!", "Needs revision", "Needs work",
    "Failed", "Rewrite",
]


@pytest.mark.parametrize("reply,quoted", REJECTS_WITH_REASON)
def test_a_reply_that_opens_with_a_rejection_word_is_not_approved(reply, quoted):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    # parsed, not failed open: the fail-open default is what approved the chapter here
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)


@pytest.mark.parametrize("reply", BARE_REJECTS)
def test_a_bare_rejection_word_is_not_approved(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    # a verdict word with nothing after it has the reason of a bare REVISE
    assert reason == "validator rejected (no reason given)", (reply, reason)


# ── A: an approval that turns on a contrast is not approved ───────────────────

#: The approval phrase is not the verdict: the part after the conjunction decides, and the
#: objection is the reason. Handing the Coder the praise would send it to fix the good parts.
APPROVALS_THAT_TURN = [
    ("Looks good, but the ring is stolen.", "the ring is stolen."),
    ("Looks good. However, the ring is stolen.", "the ring is stolen."),
    ("Looks good; however the ring is stolen.", "the ring is stolen."),
    ("Looks fine, although the timeline breaks.", "the timeline breaks."),
    ("Looks good, though the timeline breaks.", "the timeline breaks."),
    ("Looks good, yet the timeline breaks.", "the timeline breaks."),
    ("Good, but the ring is stolen.", "the ring is stolen."),
    ("All good, but the ring is stolen.", "the ring is stolen."),
    ("APPROVED, but the ring is stolen.", "the ring is stolen."),
    ("OK, but the ring is stolen.", "the ring is stolen."),
]


@pytest.mark.parametrize("reply,quoted", APPROVALS_THAT_TURN)
def test_an_approval_that_turns_on_a_contrast_is_not_approved(reply, quoted):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)
    # the objection, never the praise: "Looks good" is not something to fix
    assert "looks good" not in reason.lower(), (reply, reason)


def test_a_contrast_that_only_names_the_severity_stays_a_rejection():
    """`No issues found, but all minor.` is a not-approved with the reply's own words kept.

    "all minor" is the objection, and the continuity gate has no second chance to ask again."""
    approved, reason, unparseable = _parse_verdict_soft("No issues found, but all minor.")
    assert approved is False
    assert unparseable is False
    assert reason == "No issues found, but all minor."


# ── A: a contrast after the phrase is still a caveat ──────────────────────────

CONTRASTS_STILL_REJECT = [
    "No issues, but the ring is stolen.",
    "No issues. However, the timeline breaks.",
    "No issues; however the timeline breaks.",
    "No issues found. Yet the timeline breaks.",
    "No issues found; one problem: the timeline breaks.",
    "No issues found, except one: the timeline breaks.",
    "No issues.\nREVISE: the date of the wedding is wrong.",
    "No, it's good, but the ending is rushed.",
]


@pytest.mark.parametrize("reply", CONTRASTS_STILL_REJECT)
def test_a_contrast_that_opens_a_clause_is_still_a_caveat(reply):
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply


# ── A2: a clean approval is an approval ───────────────────────────────────────

CLEAN_APPROVALS = [
    "No issues found; everything matches the bible.",
    "No contradictions found; the timeline is intact.",
    "No contradictions found. The chapter only uses facts from the bible.",
    "No issues. Yet another consistent chapter.",
    "No discrepancies found.",
    "No factual errors found.",
    "No continuity issues found.",
    "No plot errors found.",
    "No issues found.",
    "No problems.",
    "Approved",
    "APPROVED: everything matches.",
    "No issues found; the chapter reads cleanly.",
    "No issues. The chapter is consistent throughout.",
    "No problems. The chapter keeps every fact of the bible.",
]


@pytest.mark.parametrize("reply", CLEAN_APPROVALS)
def test_a_clean_approval_is_an_approval(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True, reply
    # matched, not failed open: fail-open approves by accident
    assert unparseable is False, reply
    # no reason to fix: the approval sentence is not a defect
    assert reason == "", (reply, reason)


@pytest.mark.parametrize("reply", ["Looks good.", "Looks good to me."])
def test_a_plain_approval_still_approves(reply):
    """These read as praise and nothing else, so the fail-open rule still approves them."""
    assert _parse_verdict_soft(reply)[0] is True, reply


# ── Layer 2: the gates that have no second chance ─────────────────────────────

#: The first, fourth and seventh lines of the ticket's table.
CONTINUITY_REJECTIONS = [
    "No issues found, but the ring is stolen in ch 3.",
    "Rejected: the timeline breaks.",
    "Needs revision: Anna is dead.",
]


@pytest.mark.parametrize("reply", CONTINUITY_REJECTIONS)
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
    task = {"id": "harden17", "instruction": "Write chapter 3.", "target_files": ["chapter_03.md"]}
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


@pytest.mark.parametrize("reply", CONTINUITY_REJECTIONS + ["REJECTED - Anna is dead", "Needs work: pacing."])
def test_a_creative_run_returns_for_a_revision_instead_of_shipping(reply):
    """The chapter is not committed: the verdict comes back for another revision."""
    ok, feedback = _creative_gate(reply)
    assert ok is False, reply
    assert "unavailable" not in feedback.lower(), (reply, feedback)
    assert feedback, reply


@pytest.mark.parametrize("reply", ["No issues found; everything matches the bible.", "Approved"])
def test_a_creative_run_ships_a_clean_approval(reply):
    ok, feedback = _creative_gate(reply)
    assert ok is True, reply
    assert feedback == "", (reply, feedback)
