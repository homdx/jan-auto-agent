"""tests/test_cr25_1_extract_new_only.py — AUTO-CR-25-1: extract only NEW
facts (known bible fed back into extraction) + don't distort.

Four tests from the spec:
- test_known_facts_passed_to_prompt
- test_no_known_facts_unchanged
- test_prompt_has_no_scene_attribute_clause / test_prompt_has_negation_clause
- test_dedup_still_backstops (stubbed)
"""
from __future__ import annotations

from pathlib import Path

from tools.auto.story_bible import StoryBible, _BIBLE_SYSTEM


# ── test_known_facts_passed_to_prompt ──────────────────────────────────────────

class TestKnownFactsPassedToPrompt:
    """When known_facts is non-empty, the known-facts block is prepended to
    the user message sent to the LLM."""

    def test_known_facts_block_present(self, tmp_path: Path) -> None:
        captured: dict = {}

        def stub_llm(system: str, user: str) -> str:
            captured["system"] = system
            captured["user"] = user
            return "• Новый факт"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.extract("Текст главы.", known_facts="• Капитан Рейес — женщина")

        assert "KNOWN FACTS" in captured["user"]
        assert "do NOT repeat these" in captured["user"]
        assert "Капитан Рейес — женщина" in captured["user"]
        assert "CHAPTER:\nТекст главы." in captured["user"]

    def test_known_facts_precede_chapter(self, tmp_path: Path) -> None:
        captured: dict = {}

        def stub_llm(system: str, user: str) -> str:
            captured["user"] = user
            return "• fact"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.extract("Chapter body here.", known_facts="• Known one")

        known_idx = captured["user"].index("Known one")
        chapter_idx = captured["user"].index("Chapter body here.")
        assert known_idx < chapter_idx


# ── test_no_known_facts_unchanged ──────────────────────────────────────────────

class TestNoKnownFactsUnchanged:
    """Empty bible (no known_facts arg, or empty string) → prompt as today."""

    def test_default_arg_matches_empty_string(self, tmp_path: Path) -> None:
        captured: dict = {}

        def stub_llm(system: str, user: str) -> str:
            captured["user"] = user
            return "• fact"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.extract("Chapter text.")

        assert captured["user"] == "CHAPTER:\nChapter text."
        assert "KNOWN FACTS" not in captured["user"]

    def test_explicit_empty_known_facts_unchanged(self, tmp_path: Path) -> None:
        captured: dict = {}

        def stub_llm(system: str, user: str) -> str:
            captured["user"] = user
            return "• fact"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.extract("Chapter text.", known_facts="")

        assert captured["user"] == "CHAPTER:\nChapter text."

    def test_do_update_passes_empty_bible_on_first_run(self, tmp_path: Path) -> None:
        # No bible file exists yet → load() returns "" → extract() prompt
        # is unchanged (regression for the very first chapter).
        captured: dict = {}

        def stub_llm(system: str, user: str) -> str:
            captured["user"] = user
            return "• Капитан — Рейес"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.update("Chapter one text.")

        assert captured["user"] == "CHAPTER:\nChapter one text."


# ── test_prompt_has_no_scene_attribute_clause / negation_clause ───────────────

class TestPromptStricterClauses:
    """_BIBLE_SYSTEM gained the scene-attribute and negation-preservation
    clauses requested by AUTO-CR-25-1."""

    def test_prompt_has_no_scene_attribute_clause(self) -> None:
        assert "PERMANENT attributes" in _BIBLE_SYSTEM
        assert "momentary description" in _BIBLE_SYSTEM
        assert "seemed darker in the sunset" in _BIBLE_SYSTEM

    def test_prompt_has_negation_clause(self) -> None:
        assert "Preserve negations and qualifiers exactly" in _BIBLE_SYSTEM
        assert "secret cargo" in _BIBLE_SYSTEM


# ── test_dedup_still_backstops (stubbed) ───────────────────────────────────────

class TestDedupStillBackstops:
    """Even if the stub repeats a known fact verbatim (non-compliant model),
    the deterministic merge in _do_update does not duplicate it."""

    def test_repeated_known_fact_not_duplicated(self, tmp_path: Path) -> None:
        bible = StoryBible(lambda system, user: "• Капитан — Рейес", base_dir=tmp_path)

        # First chapter establishes the fact.
        bible.update("Chapter one.")
        first_content = bible.load()
        assert first_content.count("Капитан — Рейес") == 1

        # Second chapter: stub (mis)behaves and repeats the same fact
        # verbatim despite the known-facts block being present in the prompt.
        bible.update("Chapter two.")
        second_content = bible.load()
        assert second_content.count("Капитан — Рейес") == 1

    def test_repeated_paraphrase_not_caught_by_string_dedup(self, tmp_path: Path) -> None:
        # Documents the honest limit: dedup is string-based, not semantic.
        # A paraphrase of an existing fact is NOT removed by the merge step —
        # only the prompt (tested above) discourages the model from emitting
        # it in the first place.
        calls = {"n": 0}

        def stub_llm(system: str, user: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return "• Капитан - Рейес"
            return "• Рейес профессия - капитан"

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.update("Chapter one.")
        bible.update("Chapter two.")

        content = bible.load()
        assert "Капитан - Рейес" in content
        assert "Рейес профессия - капитан" in content
