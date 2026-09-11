"""tools/auto/context_assembler.py — AUTO-CR-4: Budget-aware Context Assembler.

Implements the "continue from the short version" rule for creative mode: the
prompt for chapter *N* is built from the **compressed synopsis** of chapters
``1..N-1`` plus the **full text** of the immediately preceding chapter
``N-1``, fitted to the model's token budget (``num_ctx_creative`` /
``max_tokens_creative`` from AUTO-CR-3).

The model never re-reads every prior chapter in full; it reads the verified
synopsis (written by AUTO-CR-5's ``SummaryMemory``) plus the immediately
preceding chapter, which keeps voice/continuity intact without needing more
context than a small local model (``llama3.1:8b``) can hold.

AUTO-CR-23-2 adds a third, higher-priority block: the **story bible**
(durable, must-not-contradict facts written by AUTO-CR-23-1's
``StoryBible``). Its budget is reserved *before* the synopsis/previous-
chapter budget is computed, so the bible is always present in the assembled
context — even when the window is so tight that old synopsis sections get
dropped. If there is no bible file (or it is empty), this degrades to
exactly the pre-CR-23 behaviour.

Public surface
--------------
    from tools.auto.context_assembler import ContextAssembler

    assembler = ContextAssembler(num_ctx=8192, max_tokens=2048, base_dir=".")
    context = assembler.build_creative_context(
        target_file="chapter_07.md",
        all_chapter_files=["chapter_01.md", ..., "chapter_06.md"],
    )

``build_creative_context`` never raises: missing files, a missing/malformed
``synopsis.md``, a missing/malformed ``story_bible.md``, or an over-budget
chapter all degrade gracefully (fail-open), matching the rest of the creative
pipeline's error handling philosophy.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from tools.auto.utils import chars_per_token

logger = logging.getLogger(__name__)

# Matches "chapter_07", "chapter_7", "Chapter_07.md", etc. — the number is
# whatever digits follow "chapter_", case-insensitive.
_CHAPTER_RE = re.compile(r"chapter[_\-\s]?(\d+)", re.IGNORECASE)

# Fixed overhead reserved for the task framing injected around this context
# (TASK ID / TITLE / INSTRUCTION / CITED LOCATION / target file listing,
# plus the closing "produce the chapter now" instruction) in coder.py's
# user prompt template. Approximate, intentionally generous.
_INSTRUCTION_OVERHEAD_TOKENS = 300

# Markers used by AUTO-CR-5's SummaryMemory.update() to key sections in
# synopsis.md. Parsing here is read-only and independent of CR-5 — if
# synopsis.md does not exist yet (CR-5 not wired up / first run), this
# assembler still works using only the previous chapter's full text.
_SECTION_RE = re.compile(
    r"<!--\s*BEGIN\s+(?P<name>\S+)\s*-->(?P<body>.*?)<!--\s*END\s+(?P=name)\s*-->",
    re.DOTALL,
)

_DROP_MARKER = "… [older synopsis omitted]"

# AUTO-CR-23-2: default cap on how much of story_bible.md is injected per
# prompt. Small and fixed — the bible itself is already kept small by
# AUTO-CR-23-1 (story_bible_max_chars), this is just the per-prompt ceiling.
_DEFAULT_BIBLE_BUDGET_CHARS = 700

_BIBLE_HEADER = "STORY FACTS (must not contradict):"

# COLLECT-23: header for the opt-in collect-context injection block (see
# `build_collect_context_block` below). Kept distinct from the story-bible
# header above so a prompt log can tell the two apart at a glance.
_COLLECT_HEADER = "COLLECT MODEL (static facts, do not contradict):"

# PLAN-v2 V2.3: the announced remainder for a budget-cut symbol list. The old
# silent `[:20]` claimed to be a complete list; `CollectBridge.
# _format_module_block` already announces its own cut ("… and N more symbol(s)
# not listed"), and this matches that honesty.
_SYMBOLS_CUT_NOTE = " … (+{extra} more, cut for budget)"

# PLAN-v2 V3: the announcement the three neighbourhood rows share. `+N` counts
# entries of the list the row itself is showing, so the announced remainder
# always reconciles with the count the row leads with.
_CUT_NOTE = ", +{extra}"

# PLAN-v2 V3: per-row name caps. The count is the fact, the names are the
# courtesy — each row leads with the total and then shows a bounded number of
# names, so a list of 25 costs the same one line as a list of 2. Constants
# rather than config keys: there is no plausible second value for them.
_CALLERS_MAX_NAMES = 5
_CALLS_INTO_MAX_NAMES = 5
_TESTS_MAX_NAMES = 3

_CALLERS_PREFIX = "callers: "
_CALLS_INTO_PREFIX = "calls_into: "
_TESTS_PREFIX = "tests: "

# PLAN-v2 V5: the one row whose material comes from Pass B (the LLM
# summaries) rather than the AST. Up to three neighbours, and the `(llm)`
# label on every line — it is the only non-static row in the pack, so the
# label is what tells the model this prose is weaker evidence than the rows
# above it. COLLECT-1's provenance isolation is preserved by labelling, not
# by exclusion: `is_safe()` still reads only static facts and never touches
# `summary`.
_NEIGHBOURS_MAX_ENTRIES = 3
_NEIGHBOURS_PURPOSE_MAX_CHARS = 110
_NEIGHBOURS_PREFIX = "neighbours: "
_NEIGHBOURS_LLM_LABEL = " (llm)"

_SENTENCE_ENDINGS = ".!?"
_WHITESPACE_RE = re.compile(r"\s+")

# PLAN-v2 V3: the forms that replace an empty row. Each asserts an absence
# rather than printing "0" — the absence is the useful fact and costs nothing.
# The entry-point note has two shapes: a module nobody imports at all, and one
# whose only importers are test files (`main.py`: 13 importers, every one a
# test). The second keeps its count — "13 test files import this" is still a
# fact — but says plainly that shipped code is not among them, because
# "nothing imports this" would be false and a coder reading the pack would
# rightly stop trusting it.
_ENTRY_POINT_NOTE = "entry point — nothing imports this"
_ENTRY_POINT_TESTS_ONLY_NOTE = (
    "entry point — nothing in shipped code imports this; {count} test {noun} {verb}"
)
_NO_TESTS_NOTE = "no test covers this file"


def _coerce_budget(budget) -> "int | None":
    """`budget` as a positive int, or `None` for "no limit".

    A value that is not an int or a numeric string (a bool, a float, a list)
    means "no budget was configured", not a zero budget: a malformed config key
    degrades to the full pack rather than raising into a run or silently
    truncating it. `int()` handles the numeric-string case, because
    `configparser` hands back strings.
    """
    if budget is None or isinstance(budget, bool):
        return None
    if not isinstance(budget, (int, str)):
        return None
    try:
        value = int(budget)
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


def _row_contract(model, target_file: str, remaining: "int | None") -> str:
    """Today's contract lines, verbatim: one per contract citing the module or
    one of its public symbols, sorted by contract name. Empty string = the row
    is absent from this block."""
    record = model.module(target_file)
    if record is None:
        return ""
    contracts = list(model.contracts_for(target_file))
    # also surface contracts cited against a symbol defined in this file
    for sym in record.public_symbols:
        contracts.extend(c for c in model.contracts_for(sym.qualname) if c not in contracts)
    if not contracts:
        return ""
    return "\n".join(
        f"contract {c.name}: {c.description}" for c in sorted(contracts, key=lambda c: c.name)
    )


def _row_config_read(model, target_file: str, remaining: "int | None") -> str:
    """Today's `config_read` lines, verbatim: one per call site. `remaining` is
    unused here — the row is per-call-site fact, never summary prose."""
    record = model.module(target_file)
    if record is None or not record.config_reads:
        return ""
    lines = []
    for cr in record.config_reads:
        mode_note = " (mode-override)" if cr.has_mode_override else ""
        lines.append(f"config_read [{cr.section}] {cr.key}{mode_note} (fallback={cr.fallback!r})")
    return "\n".join(lines)


def _row_public_symbols(model, target_file: str, remaining: "int | None") -> str:
    """The module's public symbols — LAST in the pack, because it is the one
    row the target file's own source already makes redundant.

    `remaining is None` (no budget) renders the whole list: the old silent
    `[:20]` is gone, so the 28 modules above 20 symbols no longer drop 296
    symbols without saying so. With a budget the list is cut at a symbol
    boundary and the remainder is announced (`_SYMBOLS_CUT_NOTE`); if nothing
    at all fits, the row is absent rather than a bare announcement, so a tight
    budget is not spent on "nothing listed".
    """
    record = model.module(target_file)
    if record is None:
        return ""
    symbols = [s.qualname for s in record.public_symbols]
    if not symbols:
        return ""
    prefix = "public_symbols: "
    if remaining is None:
        return f"{prefix}{', '.join(symbols)}"
    if remaining <= 0:
        return ""

    listed: "list[str]" = []
    used = 0
    limit = remaining - len(prefix)
    for qualname in symbols:
        cost = len(qualname) + (2 if listed else 0)  # 2 = the ", " joiner
        if used + cost > limit:
            break
        listed.append(qualname)
        used += cost
    if not listed:
        return ""
    if len(listed) == len(symbols):
        return f"{prefix}{', '.join(listed)}"

    # The greedy pass above reserved no room for the announcement, and the note
    # gets longer as more symbols are cut, so trim again until the announced
    # line itself fits — otherwise a LARGER budget could render FEWER symbols
    # than a smaller one. If even a single symbol plus the note does not fit,
    # the row is absent rather than a bare "nothing listed" announcement.
    while listed:
        extra = len(symbols) - len(listed)
        line = f"{prefix}{', '.join(listed)}{_SYMBOLS_CUT_NOTE.format(extra=extra)}"
        if len(line) <= remaining:
            return line
        listed.pop()
    return ""


def _plural(count: int, noun: str) -> str:
    """`noun` pluralised for `count` — the V3 rows lead with a count, and
    "1 modules import this" reads as a bug in a block that asks to be trusted."""
    return noun if count == 1 else f"{noun}s"


def _capped_names(paths: "list[str] | tuple[str, ...]", limit: int) -> str:
    """`paths` comma-joined, capped at `limit`, the remainder announced.

    Shared by the three V3 rows. `+N` is the remainder of *this* list, never of
    some other count, so "25 modules import this (2 non-test): A, B" can never
    read as if ten more names were hidden.
    """
    shown = paths[:limit]
    extra = len(paths) - len(shown)
    if extra <= 0:
        return ", ".join(shown)
    return f"{', '.join(shown)}{_CUT_NOTE.format(extra=extra)}"


def _fitted_names(head: str, bare: str, names, cap: int, remaining: "int | None") -> str:
    """`head` + up to `cap` of `names` — the largest form that fits `remaining`.

    `remaining is None` (no budget) is byte-identical to the old call: `head`
    followed by `_capped_names(names, cap)`. With a budget the names are dropped
    from the END, one at a time, until the line fits; when the last one goes,
    `bare` is returned — the count alone. The count is the fact and the names
    are the courtesy, so under budget pressure a row keeps its count instead of
    vanishing whole and handing its budget to the row that comes after it.
    `""` only when the row has no names at all: a row that has a count always
    has a smallest form, which is `bare`, and it is returned even when `bare`
    does not fit `remaining` — the caller asks for a form to *measure*, and it
    is the budget check in `build_collect_context_block` that decides whether
    the row renders at all.

    `+N` is recomputed as the names go, so the announced remainder always
    reconciles with the list the row leads with at every level of the cut.
    Monotone in `remaining`: a larger allowance never yields fewer names, so a
    bigger budget cannot make a row look more cut than a smaller one.
    """
    if not names:
        return ""
    if remaining is None:
        return f"{head}{_capped_names(names, cap)}"
    keep = min(cap, len(names))
    while keep > 0:
        line = f"{head}{', '.join(names[:keep])}"
        extra = len(names) - keep
        if extra > 0:
            line += _CUT_NOTE.format(extra=extra)
        if len(line) <= remaining:
            return line
        keep -= 1
    return bare


def _row_callers(model, target_file: str, remaining: "int | None") -> str:
    """Who breaks if this file changes: the importer count first, then the
    non-test importers, up to five, remainder announced.

    The count spans every importer, test or not — that is the blast radius, and
    it is what a rename decision turns on. The names are the non-test ones only:
    test importers are already the `tests` row, and they are 23 of coder.py's
    25 importers, so naming them here would repeat a row and hide the two
    production callers the ticket exists to surface. The `(N non-test)`
    parenthetical is dropped when it would restate the same number.

    With a budget the row shrinks by dropping caller names from the end, down to
    the count alone — `_fitted_names` — because the count is the blast radius
    the row exists to state, and losing it to a shorter row would leave the
    pack saying who this module is without saying how many files break it.
    `remaining is None` renders the whole capped row, as before.

    The entry-point form is claimed only from the artifact's own recorded
    importer set: a module absent from `imported_by` (a pre-V1 artifact, which
    is where V1's whole point was) has an *unknown* set, not an empty one, so
    no row is emitted rather than an unverified "nothing imports this". A
    module whose importers are all test files gets the entry-point form too —
    the shipped-code blast radius is what the row is for — but with its test
    importer count kept, so the fact is stated, not rounded down to nothing.
    """
    if model.module(target_file) is None or target_file not in model.imported_by:
        return ""
    total = len(model.imported_by.get(target_file, ()))
    callers = model.callers_of(target_file)
    if not callers:
        if total == 0:
            return f"{_CALLERS_PREFIX}{_ENTRY_POINT_NOTE}"
        note = _ENTRY_POINT_TESTS_ONLY_NOTE.format(
            count=total, noun=_plural(total, "file"), verb="does" if total == 1 else "do",
        )
        return f"{_CALLERS_PREFIX}{note}"
    non_test = len(callers)
    paren = f" ({non_test} non-test)" if non_test != total else ""
    verb = "imports" if total == 1 else "import"
    fact = f"{_CALLERS_PREFIX}{total} {_plural(total, 'module')} {verb} this{paren}"
    return _fitted_names(f"{fact}: ", fact, callers, _CALLERS_MAX_NAMES, remaining)


def _row_calls_into(model, target_file: str, remaining: "int | None") -> str:
    """First-party imports, up to five, remainder announced.

    `calls_into` resolves against this model's module table, so a partial or
    hand-edited artifact cannot surface a phantom dependency.

    The names carry no count of their own, so the count is the one thing this
    row can still say when they do not fit: under budget the row shrinks down to
    `calls_into: N targets` (`_fitted_names` with `_CALLS_INTO_PREFIX` as its
    head, so the unbudgeted form is byte-identical to the old one). The full
    name list is never the whole point — the point is that this module has a
    first-party dependency surface at all.
    """
    if model.module(target_file) is None:
        return ""
    targets = model.calls_into(target_file)
    if not targets:
        return ""
    count = f"{_CALLS_INTO_PREFIX}{len(targets)} {_plural(len(targets), 'target')}"
    return _fitted_names(_CALLS_INTO_PREFIX, count, targets, _CALLS_INTO_MAX_NAMES, remaining)


def _row_tests(model, target_file: str, remaining: "int | None") -> str:
    """The covering test files — or the absence of them, which is the more
    useful fact and costs nothing.

    Up to three names plus the count. A module the artifact put in
    `zero_coverage` renders the no-tests form; so does a module that has a
    `test_map` entry with no covering file even when `zero_coverage` itself is
    missing from the artifact (an older producer), since the empty entry is the
    same fact read two ways. A module that is in neither renders no row at all:
    the artifact has no opinion about it, and that is 168 of the 469 modules.

    With a budget the row shrinks to the count alone — `tests: 8 files` — before
    the loop gives up on it: the number of covering files is the fact the
    reviewer checks, the names are a convenience, and a count row costs about
    half a symbol row. `remaining is None` renders the whole capped row.
    """
    if model.module(target_file) is None:
        return ""
    tests = [t for t in model.test_map.get(target_file, ()) if isinstance(t, str)]
    if not tests:
        covered_by_nothing = target_file in model.test_map or target_file in model.zero_coverage()
        if not covered_by_nothing:
            return ""
        return f"{_TESTS_PREFIX}{_NO_TESTS_NOTE}"
    count = f"{_TESTS_PREFIX}{len(tests)} {_plural(len(tests), 'file')}"
    return _fitted_names(f"{count}: ", count, tests, _TESTS_MAX_NAMES, remaining)


def _first_sentence(text: str, limit: int = _NEIGHBOURS_PURPOSE_MAX_CHARS) -> str:
    """`text` flattened to one line and cut at the first sentence, or at
    `limit` chars, whichever comes first.

    Whitespace is collapsed first: a summary is LLM prose and can carry a
    newline or a run of spaces, and the `neighbours` row is one line per
    neighbour. The sentence cut wins while it is shorter, because a whole
    sentence is the smallest unit of meaning that can stand alone in a
    prompt; the char cut is the ceiling, so a purpose that never reaches a
    sentence end cannot grow the row past its budget.

    A sentence boundary is a terminator followed by whitespace *and* by a word
    that starts uppercase — not a bare terminator. Measured against the
    artifact on disk (2026-09-10): 241 of 469 modules carry a purpose, 16 of
    those have a terminator inside 110 chars, and 2 of the 16 are `(e.g. diff)`
    abbreviations that a bare-terminator rule would cut into a dangling
    fragment. The other 225 never reach a sentence end before the ceiling, so
    the word-boundary cut below is the shape most rendered lines take.

    When the ceiling does the cutting it backs up to the last word boundary,
    so the cut never renders a broken fragment (`"in-memor"`) that would read
    as a bug in a line already labelled weak evidence. `""` for nothing left to
    say — an empty purpose is skipped by the caller rather than rendered blank.
    """
    flat = _WHITESPACE_RE.sub(" ", text if isinstance(text, str) else "").strip()
    if not flat:
        return ""

    end = limit
    for i, ch in enumerate(flat):
        if i + 1 > end:  # the ceiling has already won; a further boundary is past it
            break
        if ch in _SENTENCE_ENDINGS and i + 1 < len(flat) and flat[i + 1] == " ":
            nxt = flat[i + 2] if i + 2 < len(flat) else ""
            if nxt and not nxt.isupper():  # an abbreviation, not a sentence end
                continue
            return flat[: i + 1].strip()

    if len(flat) <= end:  # no sentence end inside the ceiling, and it fits as is
        return flat

    space = flat.rfind(" ", 0, end)
    return flat[: space if space > 0 else end].strip()


def _row_neighbours(model, target_file: str, remaining: "int | None") -> str:
    """The purpose of this module's *neighbours*, never of the module itself.

    The target's source is already in the prompt, so a paraphrase of it is
    noise; its callers' and callees' purposes are not in the prompt and
    cannot be derived from it. That is the design decision this row
    implements, and it is why Pass B's 483 LLM calls have an agent consumer
    at all: this is the first one.

    Candidates are `callers_of` then `calls_into`, in that order — the
    callers are the heaviest blast radius the model can actually break —
    capped at `_NEIGHBOURS_MAX_ENTRIES` *after* skipping neighbours with an
    empty purpose. Skipping rather than padding matters: 228 of this repo's
    469 modules have no purpose today, and a blank line would spend budget
    on the one thing a pack exists to avoid. Each surviving purpose is cut
    by `_first_sentence` and each line carries `(llm)`.

    L6: with a budget the row shrinks by dropping whole entries from the END
    until what is left fits `remaining` — three neighbours, then two, then one,
    then no row. Each line is a complete fact about one neighbour (its path,
    its purpose, its `(llm)` label), so a shorter row states fewer facts, never
    a fragment of one; and the entries the cut keeps are the ones the order
    ranks highest (callers first). There is no count form here: a neighbour
    count is not a fact the coder acts on, so when even one entry does not fit
    the row is absent. `remaining is None` renders every entry, as before.

    Fails open like every other row: an unknown module, a neighbour the
    module table does not know, or a stand-in model with no summary table
    all yield `""`, and a neighbour whose summary is not an `LLMSummary` is
    skipped rather than raising into a run.
    """
    if model.module(target_file) is None:
        return ""

    candidates: "list[str]" = []
    for path in tuple(model.callers_of(target_file)) + tuple(model.calls_into(target_file)):
        # A neighbour is not the target: this row exists because the target's
        # own source is already in the prompt. `callers_of`/`calls_into`
        # cannot return the target from a produced artifact (the graph drops
        # self-edges), but a hand-edited one can.
        if path and path != target_file and path not in candidates:
            candidates.append(path)

    lines: "list[str]" = []
    for path in candidates:
        if len(lines) >= _NEIGHBOURS_MAX_ENTRIES:
            break
        neighbour = model.module(path)
        if neighbour is None or neighbour.summary is None:
            continue
        # `getattr`, not attribute access: a hand-edited artifact can carry a
        # summary that is not an `LLMSummary`, and a malformed row degrades to
        # a skipped neighbour rather than a raise into a run.
        purpose = _first_sentence(getattr(neighbour.summary, "purpose", ""))
        if not purpose:
            continue
        lines.append(f"{_NEIGHBOURS_PREFIX}{path} — {purpose}{_NEIGHBOURS_LLM_LABEL}")
    if remaining is not None:
        while lines and len("\n".join(lines)) > remaining:
            lines.pop()
    return "\n".join(lines)


# PLAN-v2: the ordered row list that IS the per-task fact pack. Highest
# value first — "value" = how hard this fact is to get from the target file's
# own source, which the coder already has in full in the same prompt. The
# tuple order IS the priority, and cutting a row under budget pressure IS the
# `continue` in `build_collect_context_block` below. V3 ships the first three
# rows (the neighbourhood: who imports this, what this imports, what tests it);
# V5 puts `neighbours` next — the only row carrying LLM prose, so it is the
# first thing a budget cut drops. The last three rows are the ones V2 ported
# verbatim out of the old single pass, `public_symbols` still last because it is
# the row the target file's own source makes redundant.
#
# L6: the order is unchanged, but a row is no longer dropped whole. The three
# rows that carry a `+N` tail (`callers`, `calls_into`, `tests`) shrink by
# dropping names from the end down to their count alone before the loop gives
# up on them — the count is the fact, the names are the courtesy — and
# `public_symbols` is only ever rendered into whatever that leaves. `neighbours`
# shrinks too, by whole entries from the end — it has no count to fall back to,
# so it is not in `_SHRINKABLE_ROWS` (no floor is reserved for it) and simply
# takes as many of its lines as fit what the rows above left.
_PACK_ROWS = (
    ("callers", _row_callers),
    ("calls_into", _row_calls_into),
    ("tests", _row_tests),
    ("neighbours", _row_neighbours),
    ("contract", _row_contract),
    ("config_read", _row_config_read),
    ("public_symbols", _row_public_symbols),
)

# L6: the rows whose material is a count plus a courtesy list of names, so a
# budget cut can drop names from the end and keep the count. The set is by name
# rather than a third `_PACK_ROWS` field: the tuple order is pinned byte for
# byte by the V2/V3 tests, and it stays so. `public_symbols` is not here — it
# already cuts at a symbol boundary on its own and stays last, so it only ever
# takes the budget no row above it could use.
_SHRINKABLE_ROWS = frozenset({"callers", "calls_into", "tests"})


def build_collect_context_block(
    model,
    target_file: str,
    *,
    task_mode: str = "code",
    budget: "int | None" = None,
) -> str:
    """AUTO-CR-23/COLLECT-23, PLAN-v2 V2: the opt-in `collect`-derived context
    block for `target_file` — an ordered row list built by `_PACK_ROWS`.

    Purely additive and read-only: this never touches a file on disk, and
    returns `""` (no block at all) whenever there is nothing to say, so a
    caller that always calls this and concatenates the result loses nothing
    when the model is absent/stale-ignored or the target file is unknown to
    `collect` — the exact "context as today" regression COLLECT-23's AC
    requires when the feature is off or has nothing to contribute.

    `_COLLECT_HEADER`, the `module:` line and a `parse_error:` line stay
    outside the row loop: they are not ranked content, so they cannot be cut
    or reordered, and a block whose only content is a parse error is still a
    block worth showing.

    `budget=None` renders every row in full, so every existing caller keeps
    working until V6 wires `max_context_chars_auto` in. With a budget each row
    is asked for the largest form that fits what is left — the `+N` rows shrink
    by dropping names from the end down to their count, `public_symbols` by
    cutting the symbol list at a symbol boundary and announcing the remainder —
    and only a row whose smallest form still does not fit is skipped in favour
    of the next, shorter one. That is the L6 fix: a `tests` row no longer
    disappears whole and hands its budget to `public_symbols`, because the count
    it leads with is the fact the pack exists to carry.

    `task_mode` is threaded for V14's docs-mode tuple; it does not change the
    code-mode pack this ticket ships.

    `model` is a `tools.collect.loader.CollectModel` (or any absent stand-in
    with the same `.available`/`.module`/`.contracts_for` surface) — not
    imported by type here to avoid a hard dependency from `tools.auto` on
    `tools.collect` at import time; callers that don't use collect at all
    never pay for the import.
    """
    if model is None or not getattr(model, "available", False):
        return ""

    record = model.module(target_file)
    if record is None:
        return ""

    budget = _coerce_budget(budget)

    head = [_COLLECT_HEADER, f"module: {record.path}"]
    if record.parse_error:
        head.append(f"parse_error: {record.parse_error}")

    body: "list[str]" = []
    seen: "set[str]" = set(head)
    used = len("\n".join(head))

    def _render(name: str, render, allowance: "int | None") -> str:
        """One renderer call, fail-open: a broken row is no row, never no block."""
        try:
            return render(model, target_file, allowance)
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect block row %r failed for %s: %s", name, target_file, exc)
            return ""

    # L6 pass one: the smallest form of every shrinkable row — its count alone,
    # the one thing the row still says when its names do not fit.
    floors: "dict[str, int]" = {}
    if budget is not None:
        for name, render in _PACK_ROWS:
            if name not in _SHRINKABLE_ROWS:
                continue
            floor = _render(name, render, 0)
            if floor:
                floors[name] = len(floor)

    # L6 pass two. The room left after every shrinkable row has taken its
    # count, newlines included, split equally: each row gets its count plus an
    # equal share of that room. The allowance is then a function of the budget
    # alone — never of what the rows above happened to claim — so a larger
    # budget only adds names and never takes them away, and no row can grow at
    # the expense of a row below it. The share is floored, so the block never
    # spends more than the budget.
    share = 0
    if budget is not None and floors:
        room = budget - used - len(floors) - sum(floors.values())
        if room > 0:
            share = room // len(floors)

    for name, render in _PACK_ROWS:
        if budget is None:
            allowance: "int | None" = None
        elif name in floors:
            allowance = floors[name] + share
        else:
            allowance = budget - used - 1
        row = _render(name, render, allowance)
        if not row:
            continue

        # V2.4: drop lines an earlier row already rendered, and duplicates
        # inside this row — 24 of today's config_read lines are byte-identical
        # duplicates of one already shown (the same (section, key) read through
        # two code paths), and a block that repeats a fact spends budget on
        # nothing. L6: nothing goes into `seen` here; a row that the budget
        # check below then skips must not suppress an identical line in a later
        # row, so this row's own lines are tracked in `fresh_seen` instead.
        fresh: "list[str]" = []
        fresh_seen: "set[str]" = set()
        for line in row.split("\n"):
            if line in seen or line in fresh_seen:
                continue
            fresh_seen.add(line)
            fresh.append(line)
        if not fresh:
            continue

        if budget is not None and used + len("\n".join(fresh)) + 1 > budget:
            # V2.5: not enough budget left for this row — try the next, shorter
            # one. L6: `seen` is left untouched, so a skipped row leaves no
            # trace in the block.
            continue

        seen.update(fresh)
        body.extend(fresh)
        used += len("\n".join(fresh)) + 1

    if not body and len(head) == 2:
        # Only the header + bare module line — nothing substantive to add.
        return ""

    return "\n".join(head + body)


def _chapter_number(filename: str) -> "int | None":
    """Extract the integer chapter number from a filename, or ``None`` if it
    doesn't match the ``chapter_<N>`` pattern."""
    m = _CHAPTER_RE.search(Path(filename).name)
    return int(m.group(1)) if m else None


def _order_chapters(all_chapter_files: "list[str]") -> "list[tuple[int, str]]":
    """Return ``(number, filename)`` pairs sorted ascending by chapter number.

    Filenames that don't match the ``chapter_<N>`` pattern are silently
    ignored — they can't be ordered, so they can't participate in
    continuity assembly.
    """
    numbered = [
        (n, f) for f in all_chapter_files
        if (n := _chapter_number(f)) is not None
    ]
    numbered.sort(key=lambda t: t[0])
    return numbered


class ContextAssembler:
    """Builds the budget-fitted creative-mode context block.

    Parameters
    ----------
    num_ctx:
        The model's total context window in tokens (e.g.
        ``[coder] num_ctx_creative`` from AUTO-CR-3).
    max_tokens:
        The reserved output budget in tokens (e.g.
        ``[coder] max_tokens_creative``).
    base_dir:
        Repository root. Chapter files and ``synopsis_path`` are resolved
        relative to this.
    synopsis_path:
        Relative path (from ``base_dir``) to the running synopsis file
        written by AUTO-CR-5's ``SummaryMemory``. Defaults to
        ``"synopsis.md"``.
    bible_path:
        Relative path (from ``base_dir``) to the story bible file written
        by AUTO-CR-23-1's ``StoryBible``. Defaults to ``"story_bible.md"``.
    bible_budget_chars:
        Hard cap on how many characters of the bible are injected per
        prompt. Defaults to ``700`` (AUTO-CR-23-2) — small and fixed, since
        AUTO-CR-23-1 already keeps the bible file itself small.
    """

    def __init__(
        self,
        num_ctx: int,
        max_tokens: int,
        base_dir: "str | Path",
        synopsis_path: str = "synopsis.md",
        bible_path: str = "story_bible.md",
        bible_budget_chars: int = _DEFAULT_BIBLE_BUDGET_CHARS,
    ) -> None:
        # Tolerate 0 / missing values (e.g. num_ctx=0 meaning "server
        # default") with sane fallbacks rather than producing a zero or
        # negative budget.
        # AUTO-CTXASM-GUARD-1: a non-numeric string (an already-loose
        # caller passing something other than 0/missing/a real number)
        # raised a raw ValueError from int(...) — same "tolerate a bad
        # value, don't crash" spirit as the 0/missing case right above,
        # just for a different kind of bad value.
        try:
            self._num_ctx = int(num_ctx) if num_ctx else 4096
        except (ValueError, TypeError):
            logger.warning(
                "ContextAssembler: invalid num_ctx=%r — using 4096", num_ctx,
            )
            self._num_ctx = 4096
        try:
            self._max_tokens = int(max_tokens) if max_tokens else 800
        except (ValueError, TypeError):
            logger.warning(
                "ContextAssembler: invalid max_tokens=%r — using 800", max_tokens,
            )
            self._max_tokens = 800
        self._base_dir = Path(base_dir)
        self._synopsis_path = self._base_dir / synopsis_path
        self._bible_path = self._base_dir / bible_path
        self._bible_budget_chars = max(0, int(bible_budget_chars))

    # ── Public API ───────────────────────────────────────────────────────

    def build_creative_context(
        self,
        target_file: str,
        all_chapter_files: "list[str]",
    ) -> str:
        """Return the assembled context block for *target_file*.

        Budget permitting, contains:
          * ``STORY FACTS (must not contradict)`` — durable facts from
            AUTO-CR-23-1's ``story_bible.md`` (AUTO-CR-23-2). Its budget is
            reserved *first*, before anything else below, so this block is
            always present — even when the synopsis has to drop sections to
            fit. Omitted entirely if the bible file is missing/empty, in
            which case the rest of this method is byte-for-byte unchanged
            from before AUTO-CR-23.
          * ``STORY SO FAR (synopsis)`` — verified synopsis sections for
            chapters ``1..N-1``, newest first, oldest dropped first if the
            budget is exceeded (marked with ``"… [older synopsis omitted]"``).
          * ``PREVIOUS CHAPTER (verbatim)`` — the full text of the
            highest-numbered chapter below *target_file* (most critical for
            voice/continuity).

        If even the previous chapter's full text doesn't fit the budget, it
        is folded into the synopsis fill instead (using its synopsis section
        rather than full text) and a warning is logged — this is how the
        assembler degrades gracefully at small windows (e.g. ``num_ctx=4096``).

        For the first chapter (no predecessors), or when *target_file* isn't
        a recognizable ``chapter_<N>`` filename, returns ``""`` — just the
        task framing, no errors.
        """
        # Top-level guard: this method is documented ("never raises") to
        # fail open on any missing/malformed input. The individual helpers
        # below each guard their own file reads, but nothing previously
        # guarded the assembly logic itself (chapter ordering, budget
        # arithmetic, section merging) — an unexpected error there escaped
        # as a raw exception, breaking the documented contract and aborting
        # the whole chapter-generation task over a context-assembly nicety.
        try:
            return self._build_creative_context_inner(target_file, all_chapter_files)
        except Exception:
            logger.exception(
                "ContextAssembler.build_creative_context failed for %r; "
                "degrading to no extra context (fail-open, per contract)",
                target_file,
            )
            return ""

    def _build_creative_context_inner(
        self,
        target_file: str,
        all_chapter_files: "list[str]",
    ) -> str:
        """Unguarded body of build_creative_context — see that method's
        docstring for behaviour. Split out so the top-level try/except can
        wrap the whole thing in one place."""
        target_num = _chapter_number(target_file)
        if target_num is None:
            return ""

        ordered = _order_chapters(all_chapter_files)
        prior = [(n, f) for n, f in ordered if n < target_num]
        if not prior:
            return ""

        sections = self._read_synopsis_sections()

        # AUTO-CR-23-2: reserve the bible's own budget FIRST — before the
        # synopsis/previous-chapter budget below is computed — so the bible
        # survives even when the window is tight enough to drop synopsis
        # sections. One read, one prepend, one budget subtraction; if there
        # is no bible file (or it's empty) this is a no-op.
        bible_block = self._read_bible_block()
        bible_cost = (len(bible_block) + 2) if bible_block else 0  # +2 = "\n\n" join

        # Read the previous chapter once, up front: it doubles as the
        # sample used to detect the chars-per-token ratio for the budget
        # below (Cyrillic tokenizes far denser than the ~4 chars/token
        # default — see chars_per_token()), and _assemble_core needs its
        # text anyway. Falls back to the synopsis sections as the sample
        # when there's no previous chapter text (e.g. unreadable file).
        prev_num, prev_file = prior[-1]
        prev_text = self._read_chapter_text(prev_file)
        sample_text = prev_text or "\n".join(sections.values())

        total_budget = self._budget_chars(sample_text)
        budget_chars = max(total_budget - bible_cost, 0)

        core = self._assemble_core(target_file, prior, sections, budget_chars, prev_text)

        if not bible_block:
            return core
        if not core:
            return bible_block
        return f"{bible_block}\n\n{core}"

    # ── Story bible (AUTO-CR-23-2) ───────────────────────────────────────

    def _read_bible_block(self) -> str:
        """Read ``story_bible.md`` and format it as the
        ``STORY FACTS (must not contradict)`` block, capped to
        ``bible_budget_chars``.

        Returns ``""`` if the file is missing, unreadable, or empty —
        callers must treat that as "behave exactly as before AUTO-CR-23".
        """
        try:
            text = self._bible_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not text:
            return ""
        if len(text) > self._bible_budget_chars:
            text = text[: self._bible_budget_chars]
        return f"{_BIBLE_HEADER}\n{text}"

    # ── Synopsis + previous-chapter assembly (pre-CR-23 logic) ──────────

    def _assemble_core(
        self,
        target_file: str,
        prior: "list[tuple[int, str]]",
        sections: "dict[str, str]",
        budget_chars: int,
        prev_text: str,
    ) -> str:
        """Assemble the synopsis + previous-chapter blocks for *budget_chars*.

        This is the original (pre-AUTO-CR-23) ``build_creative_context``
        body, unchanged, except that *budget_chars* is now passed in already
        net of the bible's reserved cost rather than computed here, and
        *prev_text* is passed in (already read by the caller) rather than
        re-read here.
        """
        if budget_chars <= 0:
            # Pathological config (max_tokens >= num_ctx) or the bible ate the
            # whole budget. Degrade gracefully instead of returning no context:
            # prefer the synopsis, falling back to a truncated tail of the
            # previous chapter only if there's no synopsis yet.
            prev_num, prev_file = prior[-1]
            logger.warning(
                "ContextAssembler: zero/negative budget for %s "
                "(num_ctx=%d, max_tokens=%d) — degrading (synopsis, else truncated prev).",
                target_file, self._num_ctx, self._max_tokens,
            )
            floor = 2000
            synopsis_block = self._fit_synopsis(sections, prior, floor)
            if synopsis_block:
                return synopsis_block
            if not prev_text:
                return ""
            tail = prev_text[-floor:]
            if len(prev_text) > floor:
                tail = "… [earlier text omitted]\n" + tail
            return self._format_prev_chapter_block(prev_file, tail)

        prev_num, prev_file = prior[-1]  # highest-numbered chapter < target
        prev_block = self._format_prev_chapter_block(prev_file, prev_text)

        if prev_text and len(prev_block) <= budget_chars:
            # Common path: previous chapter fits in full. Fill whatever
            # budget remains with synopsis of everything older than it.
            remaining = budget_chars - len(prev_block) - 2  # joining newline
            synopsis_block = self._fit_synopsis(sections, prior[:-1], remaining)
            parts = [p for p in (synopsis_block, prev_block) if p]
            return "\n\n".join(parts)

        # Degrade path: chapter N-1's full text alone exceeds the budget (or
        # is unreadable). Fold it into the synopsis fill — using its synopsis
        # section instead of full text — alongside everything older.
        logger.warning(
            "ContextAssembler: previous chapter %s (%d chars) does not fit "
            "the %d-char budget for target %s — degrading to its synopsis "
            "section instead of full text.",
            prev_file, len(prev_text), budget_chars, target_file,
        )
        return self._fit_synopsis(sections, prior, budget_chars)

    # ── Budget math ──────────────────────────────────────────────────────

    def _budget_chars(self, sample_text: str = "") -> int:
        """Char budget available for context, after reserving output tokens
        and a fixed instruction-framing overhead.

        Token estimate uses ``chars_per_token(sample_text)`` rather than a
        fixed ``4`` — Cyrillic text tokenizes much denser than Latin, so a
        fixed English-tuned ratio silently overflows num_ctx for Russian
        chapters. *sample_text* should be a representative excerpt of the
        content actually being budgeted (e.g. the previous chapter).
        """
        budget_tokens = self._num_ctx - self._max_tokens - _INSTRUCTION_OVERHEAD_TOKENS
        return int(max(budget_tokens, 0) * chars_per_token(sample_text))

    # ── File / synopsis reading ──────────────────────────────────────────

    def _read_chapter_text(self, filename: str) -> str:
        path = self._base_dir / filename
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("ContextAssembler: cannot read %s: %s", filename, exc)
            return ""

    @staticmethod
    def _format_prev_chapter_block(filename: str, text: str) -> str:
        return f"PREVIOUS CHAPTER (verbatim) — {filename}:\n{text}"

    def _read_synopsis_sections(self) -> "dict[str, str]":
        """Parse ``synopsis.md`` into ``{chapter_filename: section_text}``
        using AUTO-CR-5's ``<!-- BEGIN/END chapter_NN.md -->`` marker format.

        Fail-open: returns ``{}`` if the file is missing or has no parseable
        sections, so this assembler works even before AUTO-CR-5 has ever run.
        """
        try:
            raw = self._synopsis_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        sections: "dict[str, str]" = {}
        for m in _SECTION_RE.finditer(raw):
            name = m.group("name").strip()
            body = m.group("body").strip()
            if name and body:
                sections[name] = body
        return sections

    # ── Synopsis budget fill ─────────────────────────────────────────────

    def _fit_synopsis(
        self,
        sections: "dict[str, str]",
        chapters: "list[tuple[int, str]]",
        budget_chars: int,
    ) -> str:
        """Fill *budget_chars* with synopsis sections for *chapters*, newest
        first, stopping (and dropping all older ones) at the first section
        that doesn't fit. Drops are marked with ``"… [older synopsis omitted]"``.

        Chapters with no synopsis section available (e.g. CR-5 hasn't run
        for them yet) are silently skipped — they neither consume budget nor
        trigger a drop marker on their own.
        """
        if budget_chars <= 0 or not chapters:
            return ""

        newest_first = sorted(chapters, key=lambda t: t[0], reverse=True)

        included: "list[str]" = []
        used = 0
        dropped = False
        for _, fname in newest_first:
            body = sections.get(fname)
            if not body:
                continue
            block = f"## {fname}\n{body}"
            cost = len(block) + (2 if included else 0)  # joining "\n\n"
            if used + cost > budget_chars:
                dropped = True
                break
            included.append(block)
            used += cost

        if not included:
            return ""

        # Restore chronological (oldest → newest) order for narrative flow.
        included.reverse()
        body_text = "\n\n".join(included)
        if dropped:
            body_text = f"{_DROP_MARKER}\n\n{body_text}"
        return f"STORY SO FAR (synopsis):\n{body_text}"
