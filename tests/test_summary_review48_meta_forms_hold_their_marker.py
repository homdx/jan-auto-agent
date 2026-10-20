"""tests/test_summary_review48_meta_forms_hold_their_marker.py — CC review fixes 48: a leftover of HARDEN-27.

Reproduced on f42084f: the closed table of word forms HARDEN-27 gave `_strip_meta_parentheticals`
listed forms that do not hold their family's marker — "добавил", "удалена", "следовало",
"уточнения", "removing", "grammar", "implied" — so a story parenthetical the old substring test
kept, "(он добавил соль в суп)" or "(removing his helmet)", was cut from synopsis.md: the very
loss the ticket was about, moved to other words.
"""

from __future__ import annotations

import pytest

from tools.auto.summary_memory import (
    _META_MARKER_WORDS,
    _META_PAREN_MARKERS,
    _strip_meta_parentheticals,
)

STRIP = _strip_meta_parentheticals

#: The HARDEN-27 follow-up pins "удалён как лишний" and "удалены повторы" as commentary;
#: "удален" is the same word written with е. No other form reaches past its marker.
_PINNED_BEYOND_THE_MARKER = frozenset({"удалён", "удален", "удалены"})


def _old_substring_test(inner: str) -> bool:
    """The reader before HARDEN-27: a marker anywhere in the parenthetical."""
    return any(k in inner.lower() for k in _META_PAREN_MARKERS)


@pytest.mark.parametrize("paren", [
    "(деревня удалена от города на двадцать вёрст)",
    "(удаленный монастырь в горах)",
    "(он добавил соль в суп)",
    "(она добавила, что вернётся к ужину)",
    "(в зелье добавлены травы)",
    "(ему следовало бежать)",
    "(капитан потребовал уточнения)",
    "(уточнить у старосты он не успел)",
    "(removing his helmet)",
    "(he went to grammar school in Leeds)",
    "(as his tone implied)",
    "(implying he knew)",
])
def test_a_story_parenthetical_the_substring_test_kept_stays(paren):
    """Each one was cut on f42084f; none holds a marker, so the old reader kept it too."""
    assert not _old_substring_test(paren)
    assert STRIP(f"Fact {paren}.") == f"Fact {paren}."


def test_every_commentary_word_holds_a_marker():
    """Whatever word the reader calls commentary, the substring test called commentary too."""
    beyond = sorted(w for w in _META_MARKER_WORDS
                    if not _old_substring_test(w) and w not in _PINNED_BEYOND_THE_MARKER)
    assert beyond == []


@pytest.mark.parametrize("paren", [
    "(удалён как лишний)",
    "(удален как повтор)",
    "(удалены повторы)",
    "(следует удалить этот пункт)",
    "(уточняется)",
    "(добавляет автор)",
    "(не по оригиналу)",
    "(грамматическая ошибка)",
    "(this bullet should be removed)",
    "(removed as a duplicate)",
    "(grammatical slip)",
    "(implies too much)",
])
def test_the_commentary_still_goes(paren):
    assert STRIP(f"Fact {paren}.") == "Fact."
