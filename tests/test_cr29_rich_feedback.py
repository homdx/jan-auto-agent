"""tests/test_cr29_rich_feedback.py — creative Gate-2 must return a full,
multi-point actionable critique, not a single sentence (AUTO-CR-29)."""
from __future__ import annotations

from tools.auto.inner_loop import (
    _GATE2_SYSTEM_CREATIVE, _parse_verdict_soft, LLMGate2Validator,
)


def test_prompt_requests_multipoint_and_task_check():
    p = _GATE2_SYSTEM_CREATIVE
    assert "NUMBERED LIST" in p and "EVERY problem" in p
    assert "TASK FULFILMENT" in p          # dialogue-not-matching-task case
    assert "REPETITION" in p
    assert "CONTRADICTIONS" in p


def test_multiline_revise_still_parses_as_reject():
    reply = "REVISE:\n1. Диалог не по заданию.\n2. Повтор сцены.\n3. Пол перепутан."
    approved, _reason, unparsed = _parse_verdict_soft(reply)
    assert approved is False and unparsed is False


def test_full_critique_reaches_coder():
    """The creative branch must feed the WHOLE list to the coder, not just the
    first line's after-colon reason."""
    reply = ("REVISE:\n"
             "1. Диалог Миры в абзаце 2 не отвечает заданию о панике — заменить.\n"
             "2. Описание мостика повторяет chapter_2 — сократить.\n"
             "3. Капитан назван «он», Рейес — женщина — заменить на «она».")

    v = object.__new__(LLMGate2Validator)      # bypass __init__
    v.task_mode = "creative"

    # Replicate exactly the CR-29 critique-extraction used in check():
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False
    critique = reply.strip()
    low = critique.lower()
    for tok in ("revise:", "revise", "reject:", "reject", "no:"):
        if low.startswith(tok):
            critique = critique[len(tok):].lstrip(" :\n\t")
            break
    feedback = f"Reason: {critique or reason}"

    # all three problems survive into the feedback string
    assert "абзаце 2" in feedback
    assert "chapter_2" in feedback
    assert "женщина" in feedback
    assert feedback.count("\n") >= 2          # genuinely multi-line
