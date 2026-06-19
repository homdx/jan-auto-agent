"""tests/test_cr24_2_immutable_only.py — AUTO-CR-24-2: bible holds immutable
facts only; mutable/transient state is excluded from the extract prompt.

Two tests from the spec:
- test_prompt_excludes_state
- test_state_like_facts_not_kept (stubbed / documentation-style)
"""
from __future__ import annotations

from pathlib import Path

from tools.auto.story_bible import StoryBible, _BIBLE_SYSTEM


# ── tests ─────────────────────────────────────────────────────────────────────

class TestPromptExcludesState:
    """test_prompt_excludes_state: _BIBLE_SYSTEM contains the exclusion clause.

    Cheap guard so the prompt change isn't silently reverted in a future edit.
    """

    def test_exclusion_clause_present(self) -> None:
        assert "Do NOT record where characters currently are" in _BIBLE_SYSTEM

    def test_only_immutable_categories_requested(self) -> None:
        # Spec categories that should still be requested.
        for term in ("character names", "relationships", "fixed attributes"):
            assert term in _BIBLE_SYSTEM

    def test_transient_state_named_as_excluded(self) -> None:
        # The clause should explicitly call out scene-to-scene state.
        assert "what they are doing or wearing" in _BIBLE_SYSTEM
        assert "changes scene-to-scene" in _BIBLE_SYSTEM


class TestStateLikeFactsNotKept:
    """test_state_like_facts_not_kept (stubbed): documents intent only.

    This is a model-behaviour expectation, not something a stub LLM call can
    prove — the extraction model is what decides whether to follow the prompt.
    We assert on the mechanical part we control: whatever the model returns is
    parsed and merged verbatim (no code-side filtering of "stateful-looking"
    bullets), so correctness here lives entirely in the prompt tested above.
    """

    def test_mixed_extract_reply_is_merged_as_returned(self, tmp_path: Path) -> None:
        # Simulates a (non-compliant) model mixing one immutable fact with one
        # stateful one, to document current behaviour: StoryBible does not
        # post-filter — it relies on the prompt (tested above) to keep state
        # out in the first place.
        mixed_reply = (
            "• Аделина — КМС по плаванию\n"
            "• Аделина сейчас стоит на мостике\n"
        )

        def stub_llm(system: str, user: str) -> str:
            return mixed_reply

        bible = StoryBible(stub_llm, base_dir=tmp_path)
        bible.update("chapter text")

        content = bible.load()
        # Documented current behaviour: both lines persist because filtering
        # is the prompt's job, not the code's. If this ever changes (e.g. a
        # deterministic state-detector is added), update this test alongside
        # the spec change.
        assert "Аделина — КМС по плаванию" in content
        assert "Аделина сейчас стоит на мостике" in content
