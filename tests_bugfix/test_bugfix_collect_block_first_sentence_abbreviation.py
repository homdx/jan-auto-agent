"""tests_bugfix/test_bugfix_collect_block_first_sentence_abbreviation.py — V5 row.

The bug
-------
`_first_sentence` decided a sentence boundary from the *word that follows* the
terminator:

    if ch in _SENTENCE_ENDINGS and i + 1 < len(flat) and flat[i + 1] == " ":
        nxt = flat[i + 2] if i + 2 < len(flat) else ""
        if nxt and not nxt.isupper():   # an abbreviation, not a sentence end
            continue
        return flat[: i + 1].strip()

That only recognises an abbreviation followed by a *lowercase* word
(`e.g. diff`). An abbreviation followed by an uppercase word — `e.g. X`, the
common shape, since an LLM summary tends to capitalise the next word — reads
to the rule like a real sentence boundary. So the `neighbours` row rendered:

    neighbours: pkg/x.py — e.g. (llm)

A dangling fragment: it costs almost nothing in budget, it asserts nothing, and
it sits on a line already labelled `(llm)`, so a reader has no way to tell the
prose was truncated. The docstring already claimed this guard existed to prevent
exactly that, for the `(e.g. diff)` shape it had measured — the uppercase
variant slipped through it.

The fix
-------
Look at the token the terminator closes instead of the word that follows it:
a `.` ending a dotted token (`e.g`, `i.e`, `c.f`, `U.S.A`, `1.2.3`) closes an
abbreviation, not a sentence. `!` and `?` are never treated as abbreviations, so
the rule cannot widen the abbreviation class past periods.

Failing open: the new check only ever turns a cut into a *longer* cut, and the
ceiling still wins, so a purpose that reaches no boundary degrades to the char
cut exactly as before.

Only structural dataclasses are built here — no git repo, no artifact on disk,
no LLM.
"""

from __future__ import annotations

import pytest

from tools.auto.context_assembler import (
    _NEIGHBOURS_PREFIX,
    _NEIGHBOURS_PURPOSE_MAX_CHARS,
    _first_sentence,
    _is_dotted_abbreviation,
    _row_neighbours,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import LLMSummary, ModuleRecord


TARGET = "pkg/hub.py"
CALLER = "pkg/caller.py"
LIMIT = _NEIGHBOURS_PURPOSE_MAX_CHARS


# ── the defect, at the function level ──────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # an abbreviation followed by an uppercase word is not a sentence end
        ("e.g. X does the thing. And then more.", "e.g. X does the thing."),
        ("i.e. X is set. Then Y runs.", "i.e. X is set."),
        ("c.f. Y is used. Z works.", "c.f. Y is used."),
        ("U.S.A. has shipped. It works.", "U.S.A. has shipped."),
        ("Ver 1.2.3. Released today. It works.", "Ver 1.2.3. Released today."),
        # and an abbreviation at the end of the purpose
        ("e.g. X", "e.g. X"),
        ("e.g.", "e.g."),
    ],
)
def test_an_abbreviation_is_not_a_sentence_end_when_the_next_word_is_uppercase(text, want):
    got = _first_sentence(text, LIMIT)
    assert got == want
    # the cut must still be a prefix of the flattened input, never a fragment
    # reassembled from elsewhere in the string
    assert " ".join(text.split()).startswith(got)


@pytest.mark.parametrize(
    "text",
    [
        "e.g. X does the thing. And then more.",
        "Classifies accesses (e.g. `if not x:`) as guarded. Then it reports the verdict.",
        "Checks (i.e. diff) are enforced first. Then the harness runs the "
        "acceptance script and reports the verdict for every gate.",
    ],
)
def test_the_purpose_is_not_truncated_to_just_the_abbreviation(text):
    """The user-visible defect: the row rendered the abbreviation alone."""
    got = _first_sentence(text, LIMIT)
    assert got not in ("e.g.", "i.e.", "c.f."), f"{text!r} rendered as {got!r}"


# ── the pre-existing abbreviation rule is untouched ─────────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # lowercase next word: the original rule, unchanged
        ("Checks (e.g. diff) are enforced first. Then the harness runs the "
         "acceptance script and reports the verdict for every gate.",
         "Checks (e.g. diff) are enforced first."),
        ("Classifies accesses (e.g. `if not x:`) as guarded.",
         "Classifies accesses (e.g. `if not x:`) as guarded."),
        # a real sentence end still cuts: uppercase next word and no dots
        ("First sentence. Second sentence follows.", "First sentence."),
        ("One short sentence.", "One short sentence."),
        ("First sentence! A command. Then a question?", "First sentence!"),
        ("First? Next one.", "First?"),
        # a dotted token with an uppercase follower also skips, further along
        ("Ver 1.2. Next step follows. It is fast.", "Ver 1.2. Next step follows."),
    ],
)
def test_the_pre_existing_boundary_rules_are_unchanged(text, want):
    assert _first_sentence(text, LIMIT) == want


def test_only_a_period_can_close_an_abbreviation():
    """`!` and `?` never do, so the fix cannot widen the abbreviation class
    past periods and start swallowing exclamation marks."""
    assert _is_dotted_abbreviation("e.g. X", 3) is True
    assert _is_dotted_abbreviation("First sentence. X", 13) is False
    for terminator in "!?":
        text = f"First word{terminator} Next word"
        i = text.index(terminator)
        assert _is_dotted_abbreviation(text, i) is False
        assert _first_sentence(text, LIMIT) == f"First word{terminator}"


def test_a_dotted_token_at_the_start_of_the_string_is_safe():
    """The token walk must not underflow on a terminator near index 0."""
    assert _is_dotted_abbreviation(". X", 0) is False
    assert _is_dotted_abbreviation("e.g. X", 1) is False
    assert _is_dotted_abbreviation("e.g. X", 3) is True


def test_a_version_number_is_treated_as_an_abbreviation():
    """The one deliberate false negative: the error is to take the *longer* cut,
    which is the safe direction for a row that labels its own prose weak."""
    assert _is_dotted_abbreviation("Ver 1.2. Next", 7) is True
    assert _first_sentence("Ver 1.2. Next step is the build.", LIMIT) == (
        "Ver 1.2. Next step is the build."
    )


# ── the ceiling still wins ─────────────────────────────────────────────────


@pytest.mark.parametrize("limit", list(range(1, 121)))
def test_the_ceiling_still_holds_when_the_cut_is_an_abbreviation(limit):
    """The new check only ever skips a boundary, so the ceiling must still cap
    the row. A skipped boundary must never let a purpose outgrow its limit."""
    for text in (
        "e.g. X does the thing. And then a great deal more prose follows here.",
        "i.e. the whole module is about parsing and then rendering it again.",
        "Checks (e.g. diff) are enforced first. Then the harness runs the "
        "acceptance script and reports the verdict for every gate.",
    ):
        got = _first_sentence(text, limit)
        assert len(got) <= limit, (limit, got)


@pytest.mark.parametrize("limit", list(range(5, 41)))
def test_the_ceiling_never_splits_a_word_behind_an_abbreviation(limit):
    """Same guarantee as the char-cut path: back up to the last boundary.
    Below 5 the ceiling lands inside the abbreviation itself, which is the
    pinned hard-cut behaviour, not a boundary to back up from."""
    text = "e.g. X is parsed. Then memory is mapped and the render pass runs twice."
    flat = " ".join(text.split())
    got = _first_sentence(text, limit)
    assert len(got) <= limit
    assert not got.startswith(" ") and not got.endswith(" ")
    assert flat.startswith(got)
    if len(got) < len(flat):
        assert flat[len(got)] == " ", got


# ── the row level: what the coder actually sees ────────────────────────────


def _model_with_purpose(purpose: str, path: str = CALLER) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH,
        modules=(
            ModuleRecord(path=TARGET),
            ModuleRecord(path=path, summary=LLMSummary(purpose=purpose)),
        ),
        imported_by={TARGET: (path,)},
    )


def _neighbours_lines(block: str) -> list[str]:
    return [line for line in block.split("\n") if line.startswith(_NEIGHBOURS_PREFIX)]


@pytest.mark.parametrize(
    "purpose",
    [
        "e.g. X does the thing. And then more.",
        "i.e. X is set. Then Y runs.",
        "c.f. Y is used. Z works.",
    ],
)
def test_the_row_never_renders_a_bare_abbreviation(purpose):
    """The defect as it reached the prompt: one labelled line saying `e.g.`."""
    model = _model_with_purpose(purpose)
    row = _row_neighbours(model, TARGET, None)

    assert row, "the fixture must render a neighbour"
    line = row.split("\n")[0]
    prose = line[len(_NEIGHBOURS_PREFIX):].rsplit(" (llm)", 1)[0]
    assert prose.split(" — ", 1)[1] not in ("e.g.", "i.e.", "c.f."), line
    assert line.endswith(" (llm)")


def test_the_row_keeps_the_sentence_it_used_to_drop():
    model = _model_with_purpose("e.g. X does the thing. And then more.")
    block = build_collect_context_block(model, TARGET)

    lines = _neighbours_lines(block)
    assert len(lines) == 1
    assert "e.g. X does the thing." in lines[0], lines[0]
    assert "And then more." not in lines[0], "the cut must still stop at the real boundary"


@pytest.mark.parametrize(
    "purpose",
    [
        "e.g. X does the thing. And then more.",
        "A purpose with no terminator at all and it keeps going well past the "
        "ceiling of one hundred and ten characters.",
        "",
    ],
)
def test_the_row_is_still_bounded_by_the_ceiling(purpose):
    """The fix makes the cut longer; it must not make it unbounded."""
    model = _model_with_purpose(purpose)
    row = _row_neighbours(model, TARGET, None)
    if not row:
        return  # an empty purpose is skipped, not rendered blank
    for line in row.split("\n"):
        assert line.startswith(_NEIGHBOURS_PREFIX)
        assert line.endswith(" (llm)")
        assert len(line) <= (
            len(_NEIGHBOURS_PREFIX) + len(CALLER) + 3 + LIMIT + len(" (llm)")
        )


def test_the_row_still_drops_a_purpose_that_reaches_no_boundary():
    """Fail-open: a run of prose with no sentence end takes the char cut, as
    before the fix."""
    model = _model_with_purpose("no terminator anywhere and it keeps going")
    row = _row_neighbours(model, TARGET, None)
    assert row, "a terminator-free purpose still renders"
    assert len(row.split("\n")) == 1


def test_a_neighbour_with_a_bare_abbreviation_purpose_still_renders():
    """`e.g.` alone is not a fact about the neighbour; with the fix the cut
    returns the whole (short) purpose, so the line says something true rather
    than nothing, and it is still one labelled line."""
    model = _model_with_purpose("e.g.")
    row = _row_neighbours(model, TARGET, None)
    assert row == f"{_NEIGHBOURS_PREFIX}{CALLER} — e.g. (llm)"
    assert row.count("(llm)") == 1
