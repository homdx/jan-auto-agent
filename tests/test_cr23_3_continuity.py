"""tests/test_cr23_3_continuity.py — AUTO-CR-23-3 acceptance tests.

Covers:
  * contradiction → REVISE → verdict.approved is False; feedback() contains
    the concrete edit instruction verbatim
  * no contradiction → APPROVED → verdict.approved is True
  * a new event/scene that doesn't contradict a fact → APPROVED (documents
    the "events allowed" boundary)
  * unparseable LLM reply → fail-open (approved=True, unparseable=True)
  * LLM raises an exception → fail-open (approved=True, never raises)
  * find_previous_chapter_text / read_story_bible helpers
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.auto.continuity_validator import (
    ContinuityValidator,
    ContinuityVerdict,
    find_previous_chapter_text,
    read_story_bible,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_validator(llm_stub, *, max_rev: int = 1) -> ContinuityValidator:
    return ContinuityValidator(llm_stub, max_continuity_revisions=max_rev)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_contradiction_gives_actionable_feedback():
    """known facts 'герой в зелёной куртке', new text 'серая куртка' → REVISE
    with a concrete replacement; verdict.feedback() contains it verbatim."""
    def llm(system, user):
        return "REVISE: replace the hero's green jacket with a grey jacket"

    v = _make_validator(llm)
    known_facts = "• герой в зелёной куртке (the hero wears a green jacket)"
    new_text = "Герой надел серую куртку и вышел на улицу."

    verdict = v.check(known_facts, new_text)

    assert verdict.approved is False
    assert verdict.unparseable is False
    fb = verdict.feedback()
    assert "replace" in fb.lower()
    assert "green" in fb.lower()
    assert "grey" in fb.lower()


def test_no_contradiction_approves():
    """new text consistent with known facts → APPROVED."""
    def llm(system, user):
        return "APPROVED"

    v = _make_validator(llm)
    known_facts = "• The hero wears a green jacket\n• Aisha is the hero's sister"
    new_text = "The hero, still in his green jacket, walked into town."

    verdict = v.check(known_facts, new_text)

    assert verdict.approved is True
    assert verdict.unparseable is False


def test_new_event_not_flagged():
    """A new scene/location that doesn't contradict a fact → APPROVED.

    Documents the "events allowed" boundary: the continuity gate only
    catches contradictions of established attributes/state, not new plot.
    """
    def llm(system, user):
        # Correct model behaviour: a new (non-contradicting) event → APPROVED
        return "APPROVED"

    v = _make_validator(llm)
    known_facts = "• The hero wears a green jacket\n• The hero is currently in the village"
    new_text = "The hero, in his green jacket, set sail for a hidden island no one had mentioned before."

    verdict = v.check(known_facts, new_text)

    assert verdict.approved is True


def test_failopen_on_garbage():
    """Rambling / non-verdict LLM reply → approved=True, unparseable=True."""
    def llm(system, user):
        return (
            "Well, this is an interesting chapter. There might be some minor "
            "issues with pacing but overall it reads fine to me..."
        )

    v = _make_validator(llm)
    verdict = v.check("• Some fact", "Some new chapter text.")

    assert verdict.approved is True
    assert verdict.unparseable is True


def test_check_never_raises():
    """If the LLM callable raises, check() must not propagate — fail-open."""
    def llm(system, user):
        raise ConnectionError("network down")

    v = _make_validator(llm)
    verdict = v.check("• Some fact", "Some new chapter text.")

    assert verdict.approved is True


# ── ContinuityVerdict.feedback() ───────────────────────────────────────────────

def test_feedback_empty_when_approved():
    v = ContinuityVerdict(approved=True, reason="", unparseable=False)
    assert v.feedback() == ""


def test_feedback_is_reason_verbatim_when_rejected():
    """feedback() must return the concrete edit instruction verbatim — no
    extra wrapper text — so the coder gets 'replace X with Y', not a vague
    label like 'jacket wrong'."""
    reason = "change Aisha from second grade to first grade"
    v = ContinuityVerdict(approved=False, reason=reason, unparseable=False)
    assert v.feedback() == reason


# ── max_continuity_revisions attribute ────────────────────────────────────────

def test_max_continuity_revisions_stored():
    v = ContinuityValidator(lambda s, u: "APPROVED", max_continuity_revisions=3)
    assert v.max_continuity_revisions == 3


def test_max_continuity_revisions_default():
    v = ContinuityValidator(lambda s, u: "APPROVED")
    assert v.max_continuity_revisions == 1


# ── known_facts is included in the user message ───────────────────────────────

def test_known_facts_included_in_prompt():
    captured = {}

    def llm(system, user):
        captured["user"] = user
        return "APPROVED"

    v = _make_validator(llm)
    v.check("• The hero wears a green jacket", "Some new chapter text.")

    assert "KNOWN FACTS" in captured["user"]
    assert "green jacket" in captured["user"]
    assert "NEW CHAPTER" in captured["user"]


# ── find_previous_chapter_text ─────────────────────────────────────────────────

def test_find_previous_chapter_picks_highest_numbered(tmp_path: Path):
    (tmp_path / "chapter_01.md").write_text("Chapter one text.", encoding="utf-8")
    (tmp_path / "chapter_02.md").write_text("Chapter two text.", encoding="utf-8")
    (tmp_path / "chapter_03.md").write_text("Chapter three text — not yet written.", encoding="utf-8")

    text = find_previous_chapter_text("chapter_03.md", tmp_path)

    assert text == "Chapter two text."


def test_find_previous_chapter_no_predecessors(tmp_path: Path):
    (tmp_path / "chapter_01.md").write_text("Chapter one text.", encoding="utf-8")

    text = find_previous_chapter_text("chapter_01.md", tmp_path)

    assert text == ""


def test_find_previous_chapter_unrecognised_filename(tmp_path: Path):
    (tmp_path / "chapter_01.md").write_text("Chapter one text.", encoding="utf-8")

    text = find_previous_chapter_text("notes.md", tmp_path)

    assert text == ""


def test_find_previous_chapter_missing_dir():
    text = find_previous_chapter_text("chapter_02.md", "/nonexistent/path/xyz")
    assert text == ""


# ── read_story_bible ────────────────────────────────────────────────────────────

def test_read_story_bible_reads_file(tmp_path: Path):
    (tmp_path / "story_bible.md").write_text("• The hero wears a green jacket", encoding="utf-8")
    assert read_story_bible(tmp_path) == "• The hero wears a green jacket"


def test_read_story_bible_missing_file_returns_empty(tmp_path: Path):
    assert read_story_bible(tmp_path) == ""
