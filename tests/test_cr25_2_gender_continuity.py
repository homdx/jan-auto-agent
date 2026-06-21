"""tests/test_cr25_2_gender_continuity.py — AUTO-CR-25-2: gender / pronoun
continuity.

Four tests from the spec:
- test_gender_flip_flagged
- test_consistent_gender_passes
- test_bible_prompt_requests_gender
- test_continuity_prompt_checks_gender
"""
from __future__ import annotations

from tools.auto.continuity_validator import ContinuityValidator, _CONTINUITY_SYSTEM
from tools.auto.story_bible import _BIBLE_SYSTEM


# ── test_gender_flip_flagged ───────────────────────────────────────────────────

class TestGenderFlipFlagged:
    """known facts «Капитан Рейес — женщина» + new text «капитан стоял… он»
    → stub REVISE naming the pronoun fix; verdict.approved is False."""

    def test_flip_rejected_with_pronoun_fix(self) -> None:
        def llm(system: str, user: str) -> str:
            return "REVISE: change the captain from он/стоял to она/стояла"

        v = ContinuityValidator(llm)
        known_facts = "• Капитан Рейес — женщина"
        new_text = "Капитан Рейес стоял на мостике. Он спросил юнгу Миру."

        verdict = v.check(known_facts, new_text)

        assert verdict.approved is False
        assert verdict.unparseable is False
        fb = verdict.feedback().lower()
        assert "она" in fb or "стояла" in fb

    def test_flagged_check_sends_both_facts_and_text(self) -> None:
        captured: dict = {}

        def llm(system: str, user: str) -> str:
            captured["user"] = user
            return "REVISE: change the captain from он/стоял to она/стояла"

        v = ContinuityValidator(llm)
        known_facts = "• Капитан Рейес — женщина"
        new_text = "Капитан Рейес стоял… он спросил."

        v.check(known_facts, new_text)

        assert "Капитан Рейес — женщина" in captured["user"]
        assert "Капитан Рейес стоял" in captured["user"]


# ── test_consistent_gender_passes ──────────────────────────────────────────────

class TestConsistentGenderPasses:
    """female facts + female text → APPROVED."""

    def test_consistent_female_forms_approved(self) -> None:
        def llm(system: str, user: str) -> str:
            return "APPROVED"

        v = ContinuityValidator(llm)
        known_facts = "• Капитан Рейес — женщина"
        new_text = "Капитан Рейес стояла на мостике. Она спросила юнгу Миру."

        verdict = v.check(known_facts, new_text)

        assert verdict.approved is True
        assert verdict.unparseable is False

    def test_consistent_male_forms_approved(self) -> None:
        def llm(system: str, user: str) -> str:
            return "APPROVED"

        v = ContinuityValidator(llm)
        known_facts = "• Капитан Рейес — мужчина"
        new_text = "Капитан Рейес стоял на мостике. Он спросил юнгу Миру."

        verdict = v.check(known_facts, new_text)

        assert verdict.approved is True


# ── test_bible_prompt_requests_gender ──────────────────────────────────────────

class TestBiblePromptRequestsGender:
    """_BIBLE_SYSTEM contains the gender clause."""

    def test_gender_clause_present(self) -> None:
        assert "gender" in _BIBLE_SYSTEM
        # AUTO-CR-32: the example is now generic (no hardcoded character name)
        assert "женщина" in _BIBLE_SYSTEM and "мужчина" in _BIBLE_SYSTEM
        assert "Рейес" not in _BIBLE_SYSTEM   # no story-specific names leak in


# ── test_continuity_prompt_checks_gender ───────────────────────────────────────

class TestContinuityPromptChecksGender:
    """_CONTINUITY_SYSTEM contains the gender clause."""

    def test_gender_check_clause_present(self) -> None:
        assert "gender/pronoun consistency" in _CONTINUITY_SYSTEM
        assert "она" in _CONTINUITY_SYSTEM
        assert "он" in _CONTINUITY_SYSTEM

    def test_concrete_fix_example_present(self) -> None:
        assert "он/стоял to она/стояла" in _CONTINUITY_SYSTEM
