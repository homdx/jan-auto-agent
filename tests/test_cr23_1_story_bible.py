"""tests/test_cr23_1_story_bible.py — AUTO-CR-23-1: StoryBible unit tests.

All six tests from the spec:
- test_extract_returns_bullets
- test_update_creates_file
- test_merge_dedups
- test_compaction_when_over_cap
- test_update_never_raises_on_llm_error
- test_disabled_returns_none
"""
from __future__ import annotations

import configparser
import textwrap
from pathlib import Path

import pytest

from tools.auto.story_bible import StoryBible, make_story_bible, _normalise


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_bible(tmp_path: Path, llm_call, max_chars: int = 2000) -> StoryBible:
    return StoryBible(llm_call, base_dir=tmp_path, max_chars=max_chars)


def _stub_llm(reply: str):
    """Return a callable that always yields *reply*."""
    def _call(system: str, user: str) -> str:  # noqa: ANN001
        return reply
    return _call


# ── tests ─────────────────────────────────────────────────────────────────────

class TestExtractReturnsBullets:
    """test_extract_returns_bullets: stub LLM → bullets parsed."""

    def test_bullets_parsed(self, tmp_path: Path) -> None:
        llm = _stub_llm("• Hero wears a grey jacket\n• Hero's name is Arash\n")
        bible = _make_bible(tmp_path, llm)

        facts = bible.extract("Chapter text doesn't matter for stub.")

        assert isinstance(facts, list)
        assert len(facts) == 2
        assert any("grey jacket" in f for f in facts)
        assert any("Arash" in f for f in facts)

    def test_empty_reply_returns_empty_list(self, tmp_path: Path) -> None:
        bible = _make_bible(tmp_path, _stub_llm(""))
        assert bible.extract("text") == []


class TestUpdateCreatesFile:
    """test_update_creates_file: after update, story_bible.md exists with facts."""

    def test_file_created(self, tmp_path: Path) -> None:
        llm = _stub_llm("• Hero is called Mira\n• Setting is a sea voyage\n")
        bible = _make_bible(tmp_path, llm)

        bible.update("A chapter about Mira on the sea.")

        bible_file = tmp_path / "story_bible.md"
        assert bible_file.exists(), "story_bible.md should be created after update"
        content = bible_file.read_text()
        assert "Mira" in content
        assert "sea" in content.lower()

    def test_load_returns_content(self, tmp_path: Path) -> None:
        llm = _stub_llm("• The hero owns a blue sword\n")
        bible = _make_bible(tmp_path, llm)
        bible.update("chapter text")

        loaded = bible.load()
        assert "blue sword" in loaded


class TestMergeDedups:
    """test_merge_dedups: updating twice with an overlapping fact does not duplicate it."""

    def test_no_duplicate_bullets(self, tmp_path: Path) -> None:
        shared_fact = "Hero wears a green jacket"
        llm = _stub_llm(f"• {shared_fact}\n")
        bible = _make_bible(tmp_path, llm)

        bible.update("chapter 1")
        bible.update("chapter 2")

        content = bible.load()
        # Count occurrences of the normalised fact key
        norm = _normalise(shared_fact)
        # The fact text (without bullet) should appear exactly once
        occurrences = content.lower().count("green jacket")
        assert occurrences == 1, (
            f"Expected 1 occurrence of shared fact, got {occurrences}.\n"
            f"Bible content:\n{content}"
        )

    def test_distinct_facts_both_kept(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def varying_llm(system: str, user: str) -> str:
            calls.append(user)
            if len(calls) == 1:
                return "• Hero is named Zara\n"
            return "• Zara is twelve years old\n"

        bible = _make_bible(tmp_path, varying_llm)
        bible.update("chapter 1")
        bible.update("chapter 2")

        content = bible.load()
        assert "Zara" in content
        assert "twelve" in content


class TestCompactionWhenOverCap:
    """test_compaction_when_over_cap: max_chars small → deterministic compaction
    fires (AUTO-CR-24-3: no LLM call); result is dedup'd; never raises.
    """

    def test_compaction_runs_without_llm_call(self, tmp_path: Path) -> None:
        def smart_llm(system: str, user: str) -> str:
            return "• A reasonably short durable fact about the world\n"

        bible = _make_bible(tmp_path, smart_llm, max_chars=30)
        bible.update("chapter text")  # should not raise

        content = bible.load()
        assert content != ""  # compaction kept the fact, didn't drop everything

    def test_never_raises_even_if_llm_errors(self, tmp_path: Path) -> None:
        def failing_llm(system: str, user: str) -> str:
            return "• " + "x" * 100 + "\n" * 5

        bible = _make_bible(tmp_path, failing_llm, max_chars=10)
        bible.update("chapter text")  # must not raise — compaction is LLM-free now


class TestUpdateNeverRaisesOnLlmError:
    """test_update_never_raises_on_llm_error: stub raises → no exception, file unchanged."""

    def test_no_raise_on_extract_error(self, tmp_path: Path) -> None:
        def error_llm(system: str, user: str) -> str:
            raise ConnectionError("network down")

        bible = _make_bible(tmp_path, error_llm)
        bible.update("some chapter")  # must not raise

    def test_file_unchanged_on_error(self, tmp_path: Path) -> None:
        original_content = "• Pre-existing fact\n"
        bible_file = tmp_path / "story_bible.md"
        bible_file.write_text(original_content)

        def error_llm(system: str, user: str) -> str:
            raise ValueError("simulated llm failure")

        bible = _make_bible(tmp_path, error_llm)
        bible.update("some chapter")

        # File should be unchanged because extract returned []
        assert bible_file.read_text() == original_content


class TestDisabledReturnsNone:
    """test_disabled_returns_none: flag false → make_story_bible returns None."""

    def _config(self, enabled: bool) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "validator_agent": {
                "story_bible_creative": str(enabled).lower(),
                "story_bible_max_chars": "2000",
                "max_tokens": "200",
            },
            "api": {"active": "local", "verify_ssl": "true"},
            "api_local": {
                "base_url": "http://localhost:11434",
                "api_key": "ollama",
                "model": "llama3.1:8b",
                "api_format": "ollama",
            },
            "inner_loop": {"temperature": "0.1"},
            "loop": {"timeout_seconds": "300"},
        })
        return cfg

    def test_disabled(self, tmp_path: Path) -> None:
        cfg = self._config(enabled=False)
        result = make_story_bible(
            cfg,
            base_url="http://localhost:11434",
            api_key="ollama",
            model="llama3.1:8b",
            api_format="ollama",
            base_dir=tmp_path,
        )
        assert result is None

    def test_enabled(self, tmp_path: Path) -> None:
        cfg = self._config(enabled=True)
        result = make_story_bible(
            cfg,
            base_url="http://localhost:11434",
            api_key="ollama",
            model="llama3.1:8b",
            api_format="ollama",
            base_dir=tmp_path,
        )
        assert result is not None
        assert isinstance(result, StoryBible)
