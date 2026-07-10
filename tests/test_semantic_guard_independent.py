"""Regression test: story_bible_semantic_guard must work independently of
story_bible_immutable_guard.

Bug: ``_do_update`` gated the semantic-conflict-map computation AND its
lookup on ``self._immutable_guard`` alone, so setting
``story_bible_immutable_guard = false`` silently disabled the semantic
(LLM-backed) gate too, regardless of ``story_bible_semantic_guard``. The two
are documented as independent config flags — this locks in that they behave
that way.
"""
from __future__ import annotations

from tools.auto.story_bible import StoryBible


def _make_bible(tmp_path, llm, *, immutable_guard: bool, semantic_guard: bool) -> StoryBible:
    return StoryBible(
        llm,
        base_dir=tmp_path,
        path="story_bible.md",
        max_chars=2000,
        immutable_guard=immutable_guard,
        semantic_guard=semantic_guard,
    )


def _dispatching_llm(new_fact_bullet: str, conflict_reply: str):
    """One LLM stub that answers extract() and _semantic_conflicts() differently,
    the way a real model would (different question, different answer) — matches
    the shape _do_update actually calls, not just _semantic_conflicts in isolation.
    """
    def llm(system: str, user: str) -> str:
        if "NEW CANDIDATE FACTS" in user:
            return conflict_reply
        return new_fact_bullet
    return llm


def test_semantic_guard_fires_with_immutable_guard_disabled(tmp_path, caplog):
    """The exact bug scenario: immutable_guard OFF, semantic_guard ON — the
    semantic gate must still drop a contradicting fact instead of silently
    appending it (which is what happened before the fix)."""
    bible_path = tmp_path / "story_bible.md"
    bible_path.write_text(
        "• Долг за три месяца аренды числится за Олей.", encoding="utf-8",
    )
    llm = _dispatching_llm(
        new_fact_bullet="• Долг полностью погашен.",
        conflict_reply="CONFLICT N1 E1",
    )
    bible = _make_bible(tmp_path, llm, immutable_guard=False, semantic_guard=True)
    bible.update("some chapter text")

    text = bible.load()
    assert "числится за Олей" in text
    assert "полностью погашен" not in text


def test_semantic_guard_off_appends_even_with_conflict(tmp_path):
    """Sanity check in the other direction: with semantic_guard OFF (and
    immutable_guard OFF), a contradicting fact is appended as before — the
    gate must not fire when explicitly disabled."""
    bible_path = tmp_path / "story_bible.md"
    bible_path.write_text(
        "• Долг за три месяца аренды числится за Олей.", encoding="utf-8",
    )
    llm = _dispatching_llm(
        new_fact_bullet="• Долг полностью погашен.",
        conflict_reply="CONFLICT N1 E1",  # would flag a conflict if asked
    )
    bible = _make_bible(tmp_path, llm, immutable_guard=False, semantic_guard=False)
    bible.update("some chapter text")

    text = bible.load()
    assert "полностью погашен" in text  # appended — no gate was active


def test_semantic_guard_disabled_makes_no_extra_llm_call(tmp_path):
    """With semantic_guard off, _do_update must not spend a second LLM call
    asking about conflicts at all (cost/latency regression guard)."""
    calls = {"conflict_checks": 0}

    def llm(system: str, user: str) -> str:
        if "NEW CANDIDATE FACTS" in user:
            calls["conflict_checks"] += 1
            return "NONE"
        return "• Новый факт."

    bible = _make_bible(tmp_path, llm, immutable_guard=False, semantic_guard=False)
    bible.update("chapter text")
    assert calls["conflict_checks"] == 0
