"""tests/test_harden27_meta_marker_word_boundary.py — HARDEN-27.

``_strip_meta_parentheticals`` decided whether a parenthetical was the model's
self-commentary with ``any(k in inner for k in _META_PAREN_MARKERS)`` — a plain
substring search over prefix families. A family root therefore fired on any
ordinary word that merely began with the same letters: "should" in *shoulder*,
"correct" in *correctional*, "bullet" in *bulletin*. The bullet that owned the
fact was then deleted from synopsis.md, the story's only long-term memory, and
nothing logged the loss.

A marker now matches a whole word of the parenthetical and only through the
word forms the commentary actually uses; the two-word markers match as phrases
on word boundaries. The marker list itself is unchanged — only the boundary
rule moved.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tools.auto.summary_memory import (
    _META_PAREN_MARKERS,
    SummaryMemory,
    _clean_bullet_list,
    _strip_meta_parentheticals,
)

STRIP = _strip_meta_parentheticals

# Parentheticals that are story facts and must survive, each containing a word
# that begins with a marker of the family list.
STORY_PARENS = (
    "(his left shoulder, just below the collarbone)",
    "(a correctional facility upstate)",
    "(read the bulletin board)",
    "(his shoulders were bare)",
    "(the originality of the plan)",
    "(пунктуация была безупречна)",
)

# Parentheticals that are the model's commentary on its own bullet.
COMMENTARY_PARENS = (
    "(this bullet should be removed because it's not relevant)",
    "(it shouldn't be here)",
    "(this is incorrect)",
    "(corrected below)",
    "(irrelevant detail)",
    "(the original says otherwise)",
    "(does not mention the cargo)",
    "(not explicitly stated)",
    "(следует удалить этот пункт)",
    "(ошибочный пункт)",
    "(подразумевается)",
    "(grammatical slip)",
)


class TestStoryFactsKept:
    @pytest.mark.parametrize("paren", STORY_PARENS)
    def test_story_parenthetical_survives(self, paren: str) -> None:
        assert STRIP(f"fact {paren}") == f"fact {paren}"

    def test_marker_inside_word_never_matches(self) -> None:
        # "should" in shoulder, "correct" in correctional, "bullet" in
        # bulletin, "remove" in removal, "original" in originality,
        # "пункт" in пунктуация — the reported regressions.
        assert "shoulder" in STRIP("struck him in the shoulder (his left shoulder)")
        assert "correctional" in STRIP("moved to (a correctional facility upstate)")
        assert "bulletin" in STRIP("read the (bulletin board)")
        assert "removal" in STRIP("the (removal of the old fence) is planned")
        assert "originality" in STRIP("the (originality of the plan) impressed her")
        assert "пунктуация" in STRIP("его (пунктуация была безупречна)")


class TestCommentaryStillRemoved:
    @pytest.mark.parametrize("paren", COMMENTARY_PARENS)
    def test_commentary_parenthetical_removed(self, paren: str) -> None:
        assert STRIP(f"fact {paren}") == "fact"

    def test_apostrophe_variant_still_matches(self) -> None:
        # “shouldn’t” with the typographic apostrophe the models actually emit.
        assert STRIP("fact (it shouldn’t be here)") == "fact"

    def test_clean_bullet_list_still_drops_meta_tail(self) -> None:
        reply = ("• The crew is seasoned (this bullet is partially correct; it "
                 "doesn't mention the cargo)")
        out = _clean_bullet_list(reply)
        assert "• The crew is seasoned" in out
        assert "partially correct" not in out

    def test_clean_bullet_list_keeps_story_parenthetical(self) -> None:
        out = _clean_bullet_list(
            "• The arrow struck him in the shoulder "
            "(his left shoulder, just below the collarbone)"
        )
        assert "(his left shoulder, just below the collarbone)" in out


class TestBoundaryBehaviour:
    def test_two_parentheticals_lose_only_the_commentary(self) -> None:
        out = STRIP(
            "He met her (a correctional facility upstate) "
            "(this bullet should be removed)"
        )
        assert out == "He met her (a correctional facility upstate)"
        assert "this bullet" not in out

    def test_phrase_markers_need_word_boundaries(self) -> None:
        assert STRIP("fact (does not)") == "fact"
        assert STRIP("fact (not explicitly)") == "fact"
        # The same letters glued into one word are not the phrase.
        assert STRIP("fact (doesnot)") == "fact (doesnot)"
        assert STRIP("fact (notexplicitly)") == "fact (notexplicitly)"
        # And a longer word on either side is not the phrase either.
        assert STRIP("fact (does nots say)") == "fact (does nots say)"
        assert STRIP("fact (not explicitlyly)") == "fact (not explicitlyly)"

    def test_marker_family_reaches_only_its_own_forms(self) -> None:
        # Same family root, ordinary word: kept. Commentary form: removed.
        for keep, drop in (
            ("shoulder", "should"),
            ("shouldered the load", "should"),
            ("correctness of the plan", "correct"),
            ("correctional facility", "corrected below"),
            ("bulletin", "bullet"),
            ("removal of the fence", "remove"),
            ("originality", "original"),
            ("relevance of the deal", "relevant"),
            ("пунктуация", "пункт"),
            ("коррекция плана", "correct"),
        ):
            assert keep in STRIP(f"fact ({keep})"), keep
            assert STRIP(f"fact ({drop})") == "fact", drop

    def test_marker_list_unchanged(self) -> None:
        # HARDEN-27 touched only the boundary rule, never the list itself.
        assert _META_PAREN_MARKERS == (
            "bullet", "remove", "correct", "original", "accurate", "partially",
            "should", "relevant", "mention", "grammat", "implies", "does not",
            "doesn't", "not explicitly", "пункт", "удалить", "ориги", "ошибоч",
            "следует", "граммат", "подразум", "уточня", "добавляет",
        )


class TestFailOpen:
    @pytest.mark.parametrize(
        "input_",
        [
            "",
            "no parentheses at all",
            "unbalanced ( left",
            "trailing ) paren",
            "(",
            ")(",
            "((nested parens))",
            "cyrillic (ошибка) and a marker (this bullet should be removed)",
        ],
    )
    def test_never_raises_and_returns_a_string(self, input_: str) -> None:
        out = STRIP(input_)
        assert isinstance(out, str)


class TestLinearTime:
    def test_long_parenthetical_cleaned_in_linear_time(self) -> None:
        # One long word list inside a single parenthetical: a quadratic scan
        # would show up as 10x the size costing far more than 10x the time.
        small_chars, big_chars, repeats = 10_000, 100_000, 5

        def run(chars: int) -> str:
            return STRIP("fact (" + ("a " * chars) + "shoulder here.)")

        def fastest(chars: int) -> tuple[float, str]:
            out = ""
            best = float("inf")
            for _ in range(repeats):
                start = time.perf_counter()
                out = run(chars)
                best = min(best, time.perf_counter() - start)
            return best, out

        STRIP("fact (" + ("a " * 2_000) + "shoulder here.)")   # warm up
        (fastest_small, _), (fastest_big, out_big) = fastest(small_chars), fastest(big_chars)

        assert out_big.startswith("fact (")
        assert "shoulder here.)" in out_big
        assert fastest_small > 0
        assert fastest_big < fastest_small * 30, (
            f"cleaning is not linear in the parenthetical size: "
            f"10x input took {fastest_big / fastest_small:.0f}x the time"
        )



class TestSynopsisKeepsStoryFacts:
    def test_synopsis_writes_the_story_parenthetical(self, tmp_path: Path) -> None:
        bullets = (
            "- The arrow struck him in the shoulder "
            "(his left shoulder, just below the collarbone)\n"
            "- Bob was sent to prison (he was later moved to a correctional "
            "facility upstate)"
        )

        def llm_call(system: str, user: str) -> str:
            return "OK" if "SOURCE:" in user else bullets

        mem = SummaryMemory(
            llm_call, max_compression_passes=1, max_fidelity_rounds=1,
            base_dir=tmp_path,
        )
        chapter = "chapter_07.md"
        (tmp_path / chapter).write_text(
            "The arrow struck him in the shoulder. Bob was sent to prison.",
            encoding="utf-8",
        )
        mem.update(chapter)

        synopsis = (tmp_path / "synopsis.md").read_text(encoding="utf-8")
        assert "(his left shoulder, just below the collarbone)" in synopsis
        assert "(he was later moved to a correctional facility upstate)" in synopsis
        assert f"<!-- BEGIN {chapter} -->" in synopsis
        assert f"<!-- END {chapter} -->" in synopsis


def test_inflected_russian_and_unoriginal_commentary_still_goes():
    """HARDEN-27 follow-up: the plural and dative of пункт, a passive of удалить and `unoriginal` are commentary."""
    from tools.auto.summary_memory import _strip_meta_parentheticals as strip
    for inner in ("пункты 3 и 4", "по пункту 2", "пунктам не соответствует", "удалён как лишний", "удалены повторы", "unoriginal"):
        assert strip("Факт (%s) дальше" % inner) == "Факт дальше", inner


def test_possessive_of_a_marker_word_is_commentary():
    """HARDEN-27 follow-up: "(this bullet's numbers are wrong)" is commentary; a possessive of a story noun stays."""
    from tools.auto.summary_memory import _strip_meta_parentheticals as strip
    assert strip("The crew is seasoned (this bullet's numbers are wrong)") == "The crew is seasoned"
    assert strip("He rode on (the original's owner was dead) and sang") == "He rode on and sang"
    assert "(the shoulder's wound)" in strip("He bled (the shoulder's wound) badly")
