"""tests/test_cr26_2_immutable_guard.py — AUTO-CR-26-2: bible write-once
guard for immutable facts (gender, age).

Six tests from the spec:
- test_gender_flip_dropped
- test_age_conflict_dropped
- test_non_conflicting_fact_kept
- test_first_write_sets_canon
- test_disabled_guard_appends
- test_no_llm_in_guard
"""
from __future__ import annotations

import logging

from tools.auto.story_bible import StoryBible


def _make_bible(tmp_path, llm, *, immutable_guard: bool = True) -> StoryBible:
    return StoryBible(
        llm,
        base_dir=tmp_path,
        path="story_bible.md",
        max_chars=2000,
        immutable_guard=immutable_guard,
    )


# ── test_gender_flip_dropped ────────────────────────────────────────────────

class TestGenderFlipDropped:
    def test_gender_flip_dropped(self, tmp_path, caplog) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Капитан Рейес — женщина", encoding="utf-8")

        def llm(system: str, user: str) -> str:
            return "• Капитан Рейес — мужчина"

        bible = _make_bible(tmp_path, llm)
        with caplog.at_level(logging.WARNING):
            bible.update("some chapter text")

        text = bible.load()
        assert "женщина" in text
        assert "мужчина" not in text
        assert any("dropped contradicting immutable fact" in r.message for r in caplog.records)


# ── test_age_conflict_dropped ───────────────────────────────────────────────

class TestAgeConflictDropped:
    def test_age_conflict_dropped_different_number(self, tmp_path) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Рейес — сорок лет", encoding="utf-8")

        def llm(system: str, user: str) -> str:
            return "• Рейес — 50 лет"

        bible = _make_bible(tmp_path, llm)
        bible.update("chapter text")

        text = bible.load()
        assert "сорок лет" in text
        assert "50 лет" not in text

    def test_age_conflict_dropped_unknown(self, tmp_path) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Рейес — 40 лет", encoding="utf-8")

        def llm(system: str, user: str) -> str:
            return "• возраст Рейес не указан"

        bible = _make_bible(tmp_path, llm)
        bible.update("chapter text")

        text = bible.load()
        assert "40 лет" in text
        assert "не указан" not in text


# ── test_non_conflicting_fact_kept ──────────────────────────────────────────

class TestNonConflictingFactKept:
    def test_different_entity_kept(self, tmp_path) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Капитан Рейес — женщина", encoding="utf-8")

        def llm(system: str, user: str) -> str:
            return "• Мира — женщина"

        bible = _make_bible(tmp_path, llm)
        bible.update("chapter text")

        text = bible.load()
        assert "Рейес — женщина" in text
        assert "Мира — женщина" in text


# ── test_first_write_sets_canon ─────────────────────────────────────────────

class TestFirstWriteSetsCanon:
    def test_first_write_sets_canon(self, tmp_path) -> None:
        def llm(system: str, user: str) -> str:
            return "• Рейес — женщина"

        bible = _make_bible(tmp_path, llm)
        bible.update("chapter text")

        text = bible.load()
        assert "Рейес — женщина" in text


# ── test_disabled_guard_appends ─────────────────────────────────────────────

class TestDisabledGuardAppends:
    def test_disabled_guard_appends_contradiction(self, tmp_path) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Капитан Рейес — женщина", encoding="utf-8")

        def llm(system: str, user: str) -> str:
            return "• Капитан Рейес — мужчина"

        bible = _make_bible(tmp_path, llm, immutable_guard=False)
        bible.update("chapter text")

        text = bible.load()
        assert "женщина" in text
        assert "мужчина" in text


# ── test_no_llm_in_guard ─────────────────────────────────────────────────────

class TestNoLlmInGuard:
    def test_guard_is_llm_free(self, tmp_path) -> None:
        bible_path = tmp_path / "story_bible.md"
        bible_path.write_text("• Капитан Рейес — женщина", encoding="utf-8")

        bible = _make_bible(tmp_path, lambda system, user: "unused")

        def boom(*args, **kwargs):
            raise AssertionError("guard must not call the LLM")

        called = bible._conflicts_with_established(
            "Капитан Рейес — мужчина", ["Капитан Рейес — женщина"]
        )
        assert called is True
        # Sanity: the guard method itself never touches self._llm.
        bible._llm = boom
        called_again = bible._conflicts_with_established(
            "Капитан Рейес — мужчина", ["Капитан Рейес — женщина"]
        )
        assert called_again is True
