"""tests/test_harden25_verdict_nothing_denial_and_no_directives.py — HARDEN-25 A / B / C:
the three HARDEN-22 leftovers.

A: "Nothing here contradicts the outline." went back to the Coder as the thing to fix. The
NOTHING denial knew adjectives, auxiliaries and gap words between "NOTHING" and the finding,
but not a place, so the place phrase was read as the verdict word. A place phrase before a
verb of conflict is a denial now, and the same sentence with an affirming subject
("It contradicts the outline.") is the complaint it always was.

B: after the stop, the directive head was taken as the verdict and its object as the reason,
so "No issues. Reject nothing." rejected with the reason "nothing.", and "LGTM. Fix nothing."
/ "Looks good. Revise nothing." approved with the fail-open note instead of an empty reason.
A directive whose object is "nothing" / "none" / "no <noun>" is the withdrawal of the
directive, and the scan now runs on a praise head too, not only on a "No <problem>" one.

C: "Nope, the date is wrong." was prose, so the fail-open rule approved the chapter the
validator had thrown out. A "Nope," followed by a clause that names a problem rejects with
that clause; "Nope, works" and "Nope, looks fine" keep the HARDEN-10 C reading.

Both judgement calls the HARDEN-10 / HARDEN-14 pins rest on are re-checked here: "Nope,
works" stays prose, and "Nothing here fits the established characters." stays a rejection.
"""

from __future__ import annotations

import configparser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.auto.continuity_validator import ContinuityValidator
from tools.auto.inner_loop import LLMGate2Validator, _parse_verdict_soft


# ── A: a place phrase does not end the NOTHING denial ─────────────────────────

#: The denial with a place between "NOTHING" and the verb of conflict, and the same denial
#: with "NO <noun>" instead of "NOTHING". Each approves with an empty reason.
NOTHING_CONTRADICTION_DENIALS = [
    "Nothing here contradicts the outline.",
    "Nothing here conflicts with the bible.",
    "Nothing here breaks the outline.",
    "Nothing here violates the bible.",
    "Nothing here disagrees with the outline.",
    "Nothing here clashes with the tone.",
    "Nothing here undermines the pacing.",
    "Nothing anywhere contradicts the outline.",
    "Nothing in the draft contradicts the outline.",
    "Nothing in the plan breaks the bible.",
    "Nothing in the chapter contradicts the outline.",
    "Nothing contradicts the outline.",
    "Nothing contradicts the story bible",
    "No part contradicts the outline.",
    "No section breaks the bible.",
    "No, nothing here contradicts the outline.",
    "NOTHING HERE CONTRADICTS THE OUTLINE.",
]


@pytest.mark.parametrize("reply", NOTHING_CONTRADICTION_DENIALS)
def test_a_place_phrase_before_a_conflict_verb_is_a_denial(reply):
    """HARDEN-25 A: the denial approves, and it is parsed — not failed open."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True, reply
    assert reason == "", (reply, reason)
    assert unparseable is False, reply


@pytest.mark.parametrize("reply", [
    "Nothing here contradicts the outline, but the date is wrong.",
    "Nothing here contradicts the outline, but the ring is stolen.",
    "Nothing here contradicts the outline; however the ring is stolen.",
    "Nothing in the plan, but the date breaks it.",
    "Nothing anywhere breaks it, one problem: the date changes.",
])
def test_a_denial_that_goes_on_with_a_contrast_still_rejects(reply):
    """The contrast opens a clause, so it is the verdict and not the denial."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False and reason, (reply, reason)


@pytest.mark.parametrize("reply", [
    "It contradicts the outline.",
    "This contradicts the outline.",
    "It breaks the timeline.",
    "The chapter contradicts the bible.",
    "That conflicts with the outline.",
])
def test_a_conflict_verb_with_an_affirming_subject_rejects(reply):
    """HARDEN-25 A: the denial needs the denying subject; anything else is the complaint."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False, (reply, reason)
    assert reason.strip() == reply.strip(), (reply, reason)


@pytest.mark.parametrize("reply", [
    # HARDEN-14 A's pin: "fits" is not a verb of conflict, and a copula is not one either
    "Nothing here fits the established characters.",
    "Nothing here is right about this scene.",
])
def test_a_place_phrase_before_a_non_conflict_verb_still_rejects(reply):
    """Only a verb of conflict is denied; "fits" and a copula are not."""
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False, reply


# ── B: a directive whose object denies is no directive ────────────────────────

#: A denial after a directive head: the line stays an approval, with an empty reason.
DIRECTIVES_WITH_A_DENIAL_OBJECT = [
    "No issues. Reject nothing.",
    "No issues. Reject none.",
    "No issues. Reject no issues.",
    "No issues. Nothing to reject.",
    "Looks good. Revise nothing.",
    "LGTM. Fix nothing.",
    "LGTM. Fix no issues.",
    "No problems. Change nothing.",
    "No issues. Change no problems.",
]


@pytest.mark.parametrize("reply", DIRECTIVES_WITH_A_DENIAL_OBJECT)
def test_a_directive_with_a_denial_object_is_an_approval(reply):
    """HARDEN-25 B: the directive is withdrawn, so the line stays an approval."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True, reply
    assert reason == "", (reply, reason)
    assert unparseable is False, reply


@pytest.mark.parametrize("reply, quoted", [
    ("No issues. Reject this.", "this."),
    ("Looks good. Fix the date.", "the date."),
    ("Looks good. Revise the ending.", "the ending."),
    ("No issues. Change the date.", "the date."),
    ("No issues. Reject the second scene.", "the second scene."),
    ("No issues. Revise: the timeline breaks.", "the timeline breaks."),
])
def test_a_directive_with_a_real_object_still_rejects(reply, quoted):
    """HARDEN-25 B: only the denied object is read as a withdrawal."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False, reply
    assert quoted in reason, (reply, reason)
    # the approval before the stop is not something to fix
    assert "no issues" not in reason.lower(), (reply, reason)


# ── C: a "Nope," that names a problem is the rejection it opens ───────────────

NOPE_WITH_A_NAMED_PROBLEM = [
    "Nope, the date is wrong.",
    "Nope, it breaks the build.",
    "nope - this is missing a test",
    "Nope, the timeline is inconsistent.",
    "Nope, out of character.",
    "Nope. The timeline breaks.",
    "Nope, the chapter is incorrect.",
    "Nope, this is bad.",
]


@pytest.mark.parametrize("reply, quoted", [
    ("Nope, the date is wrong.", "the date is wrong"),
    ("Nope, it breaks the build.", "it breaks the build"),
    ("nope - this is missing a test", "this is missing a test"),
    ("Nope, out of character.", "out of character"),
])
def test_a_nope_with_a_named_problem_rejects(reply, quoted):
    """HARDEN-25 C: the clause is the reason — not a verdict word, not the praise."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    assert unparseable is False, reply
    assert quoted.lower() in reason.lower(), (reply, reason)


@pytest.mark.parametrize("reply", ["Nope, works", "Nope, looks fine", "Nope, no issues",
                                   "Nope, nothing to flag", "Nope, all good"])
def test_a_nope_that_names_no_problem_stays_prose(reply):
    """HARDEN-10 C's pin: "Nope, works" may answer "any problems?", so it stays prose."""
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True and unparseable is True, reply


@pytest.mark.parametrize("reply", ["Nope", "Nope.", "nope", "NOPE"])
def test_a_bare_nope_still_rejects(reply):
    """HARDEN-22 C: the bare word rejects, the named problem now does too."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False, reply
    assert reason.strip().rstrip(".").lower() == "nope", (reply, reason)


# ── Layer 2: the gates that have no second chance ─────────────────────────────

@pytest.mark.parametrize("reply", [
    "Nothing here contradicts the outline.",
    "Nothing anywhere breaks the outline.",
])
def test_the_continuity_gate_approves_a_denial_with_a_place(reply):
    v = ContinuityValidator(lambda system, user: reply)
    verdict = v.check("Anna is 30 in chapter 1.", "Anna is 25 in chapter 3.")
    assert verdict.approved is True, reply
    assert verdict.unparseable is False, reply


@pytest.mark.parametrize("reply", [
    "It contradicts the outline.",
    "Nope, the date is wrong.",
    "Looks good. Fix the date.",
])
def test_the_continuity_gate_rejects_without_a_second_chance(reply):
    """The continuity gate re-asks nothing, so a fail-open approval shipped the chapter."""
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
    task = {"id": "harden25", "instruction": "Write chapter 3.", "target_files": ["chapter_03.md"]}
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
    "Nothing here contradicts the outline.",
    "No issues. Reject nothing.",
    "Looks good. Revise nothing.",
    "Nope, works",
])
def test_a_creative_run_ships_a_clean_reply(reply):
    """The ticket's Layer 2: a clean reply ends the self-check loop instead of looping."""
    ok, feedback = _creative_gate(reply)
    assert ok is True, reply
    assert feedback == "", (reply, feedback)


@pytest.mark.parametrize("reply", [
    "It contradicts the outline.",
    "Nope, the date is wrong.",
    "Looks good. Fix the date.",
    "Nope",
])
def test_a_creative_run_returns_for_a_revision_instead_of_shipping(reply):
    """A rejection that says why comes back for another revision, never shipped."""
    ok, feedback = _creative_gate(reply)
    assert ok is False, reply
    assert "unavailable" not in feedback.lower(), (reply, feedback)
    assert feedback, reply
