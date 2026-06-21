"""tests/test_cr24_1_bible_verify.py — AUTO-CR-24-1: verify extracted facts
against the source chapter before they enter the bible.

Four tests from the spec:
- test_unsupported_fact_dropped
- test_supported_facts_pass_through
- test_verify_error_falls_back_to_raw
- test_disabled_skips_verification
"""
from __future__ import annotations

from pathlib import Path

from tools.auto.story_bible import StoryBible


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_llm(reply: str):
    """Stub llm_call used for StoryBible.extract — always returns *reply*."""
    def _call(system: str, user: str) -> str:  # noqa: ANN001
        return reply
    return _call


class _StubFidelity:
    """Drop-in replacement for SummaryFidelityVerifier with a scripted behaviour."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls: list[tuple[str, str]] = []

    def verify_and_fix(self, chapter_text: str, summary: str) -> str:
        self.calls.append((chapter_text, summary))
        if self._behaviour == "raise":
            raise RuntimeError("verifier exploded")
        return self._behaviour(chapter_text, summary)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestUnsupportedFactDropped:
    """test_unsupported_fact_dropped: corrected fact replaces the bad one."""

    def test_corrected_fact_is_merged(self, tmp_path: Path) -> None:
        extract_reply = "• Аиша во втором классе\n"
        bible = StoryBible(
            _extract_llm(extract_reply),
            base_dir=tmp_path,
            verify=True,
        )
        bible._fidelity = _StubFidelity(
            lambda chapter, summary: "• Аиша в первом классе\n"
        )

        bible.update("Аиша учится в первом классе.")

        content = bible.load()
        assert "Аиша в первом классе" in content
        assert "Аиша во втором классе" not in content


class TestSupportedFactsPassThrough:
    """test_supported_facts_pass_through: verifier confirms — facts unchanged."""

    def test_unchanged_when_supported(self, tmp_path: Path) -> None:
        extract_reply = "• Zara is twelve years old\n"
        bible = StoryBible(
            _extract_llm(extract_reply),
            base_dir=tmp_path,
            verify=True,
        )
        bible._fidelity = _StubFidelity(
            lambda chapter, summary: summary  # confirmed as-is
        )

        bible.update("Zara, twelve years old, walked into the room.")

        content = bible.load()
        assert "Zara is twelve years old" in content


class TestVerifyErrorFallsBackToRaw:
    """test_verify_error_falls_back_to_raw: verifier raises → fail-open."""

    def test_raw_facts_kept_on_exception(self, tmp_path: Path) -> None:
        extract_reply = "• Hero is named Zara\n"
        bible = StoryBible(
            _extract_llm(extract_reply),
            base_dir=tmp_path,
            verify=True,
        )
        bible._fidelity = _StubFidelity("raise")

        bible.update("Zara, the hero, looked around.")  # must not raise

        content = bible.load()
        assert "Hero is named Zara" in content


class TestDisabledSkipsVerification:
    """test_disabled_skips_verification: flag false → verifier never called."""

    def test_verifier_not_constructed_or_called(self, tmp_path: Path) -> None:
        extract_reply = "• Hero is named Zara\n"
        bible = StoryBible(
            _extract_llm(extract_reply),
            base_dir=tmp_path,
            verify=False,
        )
        assert bible._fidelity is None, (
            "verify=False must not construct a fidelity verifier"
        )

        bible.update("Zara, the hero, looked around.")

        content = bible.load()
        assert "Hero is named Zara" in content
