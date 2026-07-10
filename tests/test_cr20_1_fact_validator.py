"""tests/test_cr20_1_fact_validator.py — AUTO-CR-20-1 acceptance tests.

Covers:
  * contradiction in TEXT → REVISE → verdict.approved is False, reason non-empty
  * consistent text → APPROVED → verdict.approved is True
  * omission (missing detail) must NOT trigger REVISE (intended behaviour)
  * unparseable LLM reply → fail-open (approved=True, unparseable=True)
  * LLM raises an exception → fail-open (approved=True, never raises)
"""

from __future__ import annotations


from tools.auto.fact_validator import FactValidator, FactVerdict


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_validator(llm_stub, *, max_rev: int = 1) -> FactValidator:
    return FactValidator(llm_stub, max_fact_revisions=max_rev)


def _task(instruction: str, goal: str = "") -> dict:
    return {"instruction": instruction, "goal": goal}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_contradiction_revises():
    """TASK 'мама не работает' + TEXT saying she works → REVISE."""
    def llm(system, user):
        return "REVISE: TASK states мама не работает but TEXT says работает учителем"

    v = _make_validator(llm)
    task = _task("Describe the mother. She does not work (мама не работает).")
    text = "Мама работает учителем в школе и каждый день уходит рано утром."

    verdict = v.check(task, text)

    assert verdict.approved is False
    assert verdict.reason  # non-empty reason naming the contradiction
    assert verdict.unparseable is False


def test_no_contradiction_approves():
    """TEXT consistent with TASK facts → APPROVED."""
    def llm(system, user):
        return "APPROVED"

    v = _make_validator(llm)
    task = _task("Describe the mother. She does not work.")
    text = "Мама сидит дома и ухаживает за детьми."

    verdict = v.check(task, text)

    assert verdict.approved is True
    assert verdict.unparseable is False


def test_omission_does_not_revise():
    """A text that merely *omits* a task detail must NOT be flagged.

    The LLM is stubbed to return APPROVED (as it should for an omission), and
    we assert the verdict is approved.  This documents the intended behaviour:
    Gate-3 is a contradiction checker, not a completeness checker.
    """
    def llm(system, user):
        # Correct model behaviour: an omission → APPROVED
        return "APPROVED"

    v = _make_validator(llm)
    task = _task(
        "Write a stanza about Aisha. She is in first grade. She likes cats."
    )
    # TEXT mentions first grade but says nothing about cats — omission, not contradiction
    text = "Аиша пошла в первый класс, с ранцем за плечами."

    verdict = v.check(task, text)

    assert verdict.approved is True


def test_failopen_on_garbage():
    """Rambling / non-verdict LLM reply → approved=True, unparseable=True."""
    def llm(system, user):
        return (
            "Well, let me think about this carefully. The text seems mostly fine "
            "but there could be some issues with the metaphor in line three..."
        )

    v = _make_validator(llm)
    task = _task("Describe the home.")
    text = "Дом был тихим и уютным."

    verdict = v.check(task, text)

    assert verdict.approved is True
    assert verdict.unparseable is True


def test_check_never_raises_on_llm_error():
    """If the LLM callable raises, check() must not propagate — fail-open."""
    def llm(system, user):
        raise ConnectionError("network down")

    v = _make_validator(llm)
    task = _task("Write about the school.")
    text = "Школа была далеко от дома."

    # Must not raise
    verdict = v.check(task, text)

    assert verdict.approved is True


# ── FactVerdict.feedback() ────────────────────────────────────────────────────

def test_feedback_empty_when_approved():
    v = FactVerdict(approved=True, reason="", unparseable=False)
    assert v.feedback() == ""


def test_feedback_contains_reason_when_rejected():
    reason = "TASK says first grade but TEXT says second grade"
    v = FactVerdict(approved=False, reason=reason, unparseable=False)
    fb = v.feedback()
    assert "FACT CONFLICT" in fb
    assert reason in fb


# ── max_fact_revisions attribute ──────────────────────────────────────────────

def test_max_fact_revisions_stored():
    v = FactValidator(lambda s, u: "APPROVED", max_fact_revisions=3)
    assert v.max_fact_revisions == 3


def test_max_fact_revisions_default():
    v = FactValidator(lambda s, u: "APPROVED")
    assert v.max_fact_revisions == 1


# ── Goal field is included in the user message ────────────────────────────────

def test_goal_included_in_prompt():
    """When task contains a 'goal', it should appear in the user message sent
    to the LLM so the checker has the full context of what was intended."""
    captured = {}

    def llm(system, user):
        captured["user"] = user
        return "APPROVED"

    v = _make_validator(llm)
    task = {
        "instruction": "Write about Aisha.",
        "goal": "Aisha is in first grade and lives at home.",
    }
    v.check(task, "Аиша живёт дома.")

    assert "GOAL" in captured["user"]
    assert "first grade" in captured["user"]
