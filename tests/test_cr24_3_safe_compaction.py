"""tests/test_cr24_3_safe_compaction.py — AUTO-CR-24-3: deterministic
compaction (no silent fact loss, no LLM call).

Four tests from the spec:
- test_dedup_merges_substrings
- test_over_cap_keeps_all_and_warns
- test_hard_ceiling_drops_oldest_with_log
- test_no_llm_call_in_compaction
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tools.auto.story_bible import StoryBible, _dedup_substrings


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_bible(tmp_path: Path, llm_call, max_chars: int = 2000) -> StoryBible:
    return StoryBible(llm_call, base_dir=tmp_path, max_chars=max_chars)


def _bullets_text(bullets: list[str]) -> str:
    return "\n".join(f"• {b}" for b in bullets)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestDedupMergesSubstrings:
    """test_dedup_merges_substrings: a bullet that is a substring of another
    is removed."""

    def test_substring_bullet_dropped(self) -> None:
        bullets = [
            "Zara is twelve years old",
            "Zara is twelve",  # substring of the bullet above
            "Аделина — КМС по плаванию",
        ]
        result = _dedup_substrings(bullets)

        assert "Zara is twelve years old" in result
        assert "Zara is twelve" not in result
        assert "Аделина — КМС по плаванию" in result

    def test_exact_duplicate_dropped(self) -> None:
        bullets = ["Hero is named Zara", "Hero is named Zara"]
        result = _dedup_substrings(bullets)
        assert result == ["Hero is named Zara"]


class TestOverCapKeepsAllAndWarns:
    """test_over_cap_keeps_all_and_warns: deduped text over max_chars but
    under the hard ceiling → all bullets kept, WARNING logged."""

    def test_all_facts_kept_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Distinct, non-substring facts whose combined length lands between
        # max_chars and the hard ceiling (2x max_chars).
        facts = [f"Distinct durable fact number {i} about the world" for i in range(4)]

        def extract_llm(system: str, user: str) -> str:
            return _bullets_text(facts)

        bible = _make_bible(tmp_path, extract_llm, max_chars=80)

        with caplog.at_level(logging.WARNING, logger="tools.auto.story_bible"):
            bible.update("chapter text")

        content = bible.load()
        for fact in facts:
            assert fact in content, f"fact lost: {fact}"

        assert any(
            "consider raising story_bible_max_chars" in record.message
            for record in caplog.records
        ), "expected a WARNING nudging the operator to raise the cap"


class TestHardCeilingDropsOldestWithLog:
    """test_hard_ceiling_drops_oldest_with_log: above the hard ceiling →
    oldest dropped, dropped bullets logged."""

    def test_oldest_dropped_when_ceiling_exceeded(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        max_chars = 20
        # Each fact is long/distinct enough that dedup can't help, and the
        # total comfortably exceeds the hard ceiling (2x max_chars = 40).
        facts = [f"Unique durable fact {i} that is fairly long indeed" for i in range(6)]

        def extract_llm(system: str, user: str) -> str:
            return _bullets_text(facts)

        bible = _make_bible(tmp_path, extract_llm, max_chars=max_chars)

        with caplog.at_level(logging.WARNING, logger="tools.auto.story_bible"):
            bible.update("chapter text")

        content = bible.load()

        # The newest (last) fact must survive; the oldest (first) must be gone.
        assert facts[-1] in content
        assert facts[0] not in content

        assert any(
            "hard ceiling" in record.message and "dropped" in record.message
            for record in caplog.records
        ), "expected a WARNING naming the dropped oldest fact(s)"


class TestNoLlmCallInCompaction:
    """test_no_llm_call_in_compaction: monkeypatch the llm_call to raise →
    _compact still works (proves it's LLM-free)."""

    def test_compact_never_touches_llm(self, tmp_path: Path) -> None:
        calls = {"count": 0}

        def exploding_llm(system: str, user: str) -> str:
            calls["count"] += 1
            # First call is the extraction call (must succeed so we have
            # facts to compact); any call beyond that would be the old
            # compaction LLM call, which AUTO-CR-24-3 must never make.
            if calls["count"] == 1:
                return _bullets_text(
                    [f"Durable fact {i} about the established world" for i in range(5)]
                )
            raise AssertionError("compaction must not call the LLM (AUTO-CR-24-3)")

        bible = _make_bible(tmp_path, exploding_llm, max_chars=20)
        bible.update("chapter text")  # must not raise

        assert calls["count"] == 1, "only the extraction call should have hit the LLM"
        assert bible.load() != ""
