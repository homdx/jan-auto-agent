"""tests/test_collect_pack_neighbour_purpose.py — PLAN-v2 V5.

The `neighbours` row: Pass B's 483 LLM calls per full build get their first
agent consumer. The row carries the purpose of the target's *neighbours*,
never of the target itself — the target's source is already in the prompt, so
a paraphrase of it is noise, while its callers' and callees' purposes are not
in the prompt and cannot be derived from it.

  V5-1  Up to three entries, `callers_of` then `calls_into`, in that order,
        keeping `callers_of`'s own ranking.
  V5-2  Each purpose cut at the first sentence or 110 chars, whichever is
        shorter, never split mid-word and never left a bare newline.
  V5-3  A neighbour with an empty purpose is skipped, not rendered blank.
  V5-4  `(llm)` on every line of the row and on no other line in the pack: the
        row is the only non-static one, so the label is what tells the model
        the line is weaker evidence than the rows above it.
  V5-5  The row sits below `tests` and above `contract` in `_PACK_ROWS`, so
        under a budget cut the static facts above it survive and the prose is
        what goes.

Only structural dataclasses are built here — no git repo except for the
end-to-end section, which runs a real `collect` without an LLM so the absence
path is exercised through the actual producer and loader rather than only
against hand-built tables. The live section at the bottom pins the row against
this repo's own tree; the one artifact-backed test skips where `.collect/` has
not been built, since it is gitignored.

Acceptance drift, against this tree: the ticket places `neighbours` below
`risk` and expects it to be dropped before `fails_open` at half the pack's
budget. V4's two rows have not landed here yet (see the V4 ticket), so V5-5 is
asserted against the rows that do sit above `neighbours` — callers, calls_into
and tests. The slot in `_PACK_ROWS` is the one V4 needs: V4 inserts above
`neighbours`, so this ticket needs no reordering when it lands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.auto.context_assembler import (
    _CALLERS_PREFIX,
    _CALLS_INTO_PREFIX,
    _NEIGHBOURS_LLM_LABEL,
    _NEIGHBOURS_MAX_ENTRIES,
    _NEIGHBOURS_PREFIX,
    _NEIGHBOURS_PURPOSE_MAX_CHARS,
    _PACK_ROWS,
    _TESTS_PREFIX,
    _first_sentence,
    _row_config_read,
    _row_contract,
    _row_neighbours,
    _row_public_symbols,
    build_collect_context_block,
)
from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ConfigRead, FunctionRecord, LLMSummary, ModuleRecord
from tools.collect.risk import RiskEntry
from tools.collect.scanner import scan_repo
from tools.collect.graph import import_edges as build_edges
from tools.collect.graph import imported_by as reverse_index
from tools.collect.test_map import build_test_map, zero_coverage


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / ".collect" / "artifact.json"

TARGET = "pkg/hub.py"
CALLERS = ("pkg/alpha.py", "pkg/beta.py")
CALLED = ("tools/x.py",)
TESTS = ("tests/test_hub.py",)
ALL_PATHS = (TARGET,) + CALLERS + CALLED + TESTS

# Deliberately long and terminator-free inside the first 110 chars, so the char
# ceiling is what cuts it. The boundary after "before" sits well before the
# limit, so the cut must land there rather than inside a word.
LONG_PURPOSE = (
    "Loads standard SKILL.md files and applies them as in-memory config overlays "
    "to the parsed agents.ini before the agent pipeline starts so downstream "
    "modules run unmodified"
)
# Two sentences, the first comfortably inside the limit: the sentence cut must
# win over the char ceiling.
TWO_SENTENCES = (
    "Runs the per-round inner loop of the autonomous pipeline. It then hands "
    "each attempt to the coder, the executor and the validator in turn, and "
    "reports the verdict."
)
WORDY = "alpha beta gamma delta epsilon zeta"


# ── helpers ────────────────────────────────────────────────────────────────


def _module(
    path: str,
    *,
    purpose: str = "",
    symbols: tuple = (),
    config_reads: tuple = (),
) -> ModuleRecord:
    """A module record shaped exactly as the artifact's: `summary` is `None`
    unless a purpose is given, which is what a producer without an LLM looks
    like."""
    return ModuleRecord(
        path=path,
        public_symbols=tuple(
            FunctionRecord(
                qualname=f"{path}:{sym}", module=path, lineno=1, signature=f"def {sym}()"
            )
            for sym in symbols
        ),
        config_reads=tuple(config_reads),
        summary=LLMSummary(purpose=purpose, notes="") if purpose else None,
    )


def _risk(path: str, blast_radius: int) -> RiskEntry:
    return RiskEntry(
        path=path, loc=10, blast_radius=blast_radius, unguarded_count=0,
        undocumented_fail_open_count=0, zero_coverage=False, score=0,
    )


def _model(
    modules,
    *,
    import_edges=None,
    imported_by=None,
    test_map=None,
    zero_coverage=(),
    risk_index=(),
) -> CollectModel:
    """A fresh `CollectModel` with only the tables the row under test needs.

    Defaults are empty containers, never `None`, matching the loader — an
    artifact that never wrote a table reads as "nothing known", which is the
    fail-open path every renderer must survive.
    """
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges=dict(import_edges or {}),
        imported_by=dict(imported_by or {}),
        test_map={k: tuple(v) for k, v in (test_map or {}).items()},
        zero_coverage_list=tuple(zero_coverage),
        risk_index=tuple(risk_index),
    )


def _neighbour_graph():
    """The graph most tests run on: `TARGET` is imported by two shipped modules,
    imports one, and is covered by one test file."""
    import_edges = {path: () for path in ALL_PATHS}
    import_edges[TARGET] = CALLED
    imported_by = {path: () for path in ALL_PATHS}
    imported_by[TARGET] = CALLERS
    return import_edges, imported_by


def _rich_model() -> CollectModel:
    """Every row of the pack fires, so a test can see the whole block at once.

    Purposes are attached to the neighbours only; `TARGET` carries none, so a
    row that rendered the target's own purpose instead of its neighbours' would
    be caught. Blast radii run *against* the alphabetical order, so a row that
    sorted neighbour names alphabetically would list `pkg/alpha.py` first.
    """
    import_edges, imported_by = _neighbour_graph()
    modules = [
        _module(
            TARGET,
            symbols=("hub_fn", "Hub"),
            config_reads=tuple(
                ConfigRead(section="api", key=key, fallback=fallback)
                for key, fallback in (
                    ("active", "local"),
                    ("verify_ssl", True),
                    ("timeout", 30),
                    ("retries", 3),
                    ("endpoint", None),
                )
            ),
        ),
        _module(CALLERS[0], purpose=LONG_PURPOSE, symbols=("alpha_fn",)),
        _module(CALLERS[1], purpose=TWO_SENTENCES, symbols=("beta_fn",)),
        _module(CALLED[0], purpose="Streams model output as it arrives.", symbols=("x_fn",)),
        _module(TESTS[0]),
    ]
    risk = [_risk(path, 10 * i) for i, path in enumerate(CALLERS, start=1)]
    test_map = {path: () for path in ALL_PATHS}
    test_map[TARGET] = TESTS
    return _model(
        modules,
        import_edges=import_edges,
        imported_by=imported_by,
        test_map=test_map,
        zero_coverage=[path for path, tests in test_map.items() if not tests],
        risk_index=risk,
    )


def _neighbour_lines(block: str) -> list:
    return [line for line in block.split("\n") if line.startswith(_NEIGHBOURS_PREFIX)]


# Every row this file can make render, by the prefix it renders with. Used to
# read a block back into a label list — `config_read`'s own text carries no
# colon, so splitting a line on ":" would return the whole line instead of its
# label.
_ROW_PREFIXES = (
    _CALLERS_PREFIX,
    _CALLS_INTO_PREFIX,
    _TESTS_PREFIX,
    _NEIGHBOURS_PREFIX,
    "contract ",
    "config_read ",
    "public_symbols: ",
)


def _row_labels(block: str) -> list:
    """Each body line (after the header and `module:` line) as its row's label."""
    labels = []
    for line in block.split("\n")[2:]:
        labels.append(
            next((prefix.split(": ")[0].rstrip() for prefix in _ROW_PREFIXES if line.startswith(prefix)), None)
        )
    return labels


def _row_sequence(block: str) -> list:
    """The rows the block renders, in order — one entry per row, so a row that
    spans several lines still counts once."""
    labels = _row_labels(block)
    return [label for i, label in enumerate(labels) if i == 0 or label != labels[i - 1]]


def _paths_in(lines: list) -> list:
    """The neighbour paths the row named, in the order it named them."""
    return [line.split(" — ")[0].split(_NEIGHBOURS_PREFIX, 1)[1] for line in lines]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


# ── V5-4: provenance labelling ─────────────────────────────────────────────


def test_every_neighbours_line_carries_the_llm_label():
    """AC: the row renders `(llm)` — it is the only non-static row in the pack."""
    lines = _neighbour_lines(build_collect_context_block(_rich_model(), TARGET))
    assert lines, "the rich model must render a neighbours row"
    for line in lines:
        assert line.endswith(_NEIGHBOURS_LLM_LABEL), line


def test_no_other_line_in_the_pack_carries_the_llm_label():
    """The label is provenance isolation by *labelling*: without it a reader
    could not tell the AST facts from the LLM prose at all."""
    block = build_collect_context_block(_rich_model(), TARGET)
    for line in block.split("\n"):
        if not line.startswith(_NEIGHBOURS_PREFIX):
            assert _NEIGHBOURS_LLM_LABEL not in line, line


@pytest.mark.parametrize("render", [_row_contract, _row_config_read, _row_public_symbols])
def test_no_static_row_renderer_can_emit_the_llm_label(render):
    """The same claim at renderer level, so a later edit to a static row cannot
    smuggle prose into a row whose label promises facts."""
    model = _rich_model()
    assert _NEIGHBOURS_LLM_LABEL not in render(model, TARGET, None)


# ── V5-5: position in the pack ─────────────────────────────────────────────


def test_the_row_sits_below_the_neighbourhood_rows_and_above_the_v2_rows():
    """The tuple order IS the priority. `neighbours` goes in the slot V4's
    `fails_open`/`risk` land above, so V4 needs no reordering when it arrives."""
    names = [name for name, _ in _PACK_ROWS]
    assert names.index("neighbours") == 3
    assert names.index("tests") < names.index("neighbours")
    for lower in ("contract", "config_read", "public_symbols"):
        assert names.index("neighbours") < names.index(lower)


def test_the_row_renders_after_tests_and_before_config_read_in_the_block():
    """The emitted order is the tuple order, not just a property of the tuple.
    The rich model has no contract row, so `contract` is absent and the rows
    below `neighbours` are the two V2 rows."""
    assert _row_sequence(build_collect_context_block(_rich_model(), TARGET)) == [
        "callers",
        "calls_into",
        "tests",
        "neighbours",
        "config_read",
        "public_symbols",
    ]


# ── V5-1: ordering and the cap ─────────────────────────────────────────────


def test_the_row_lists_callers_before_calls_into_and_caps_at_three():
    """AC: up to three entries from `callers_of` + `calls_into`, in that order
    — the callers are the blast radius a change actually breaks."""
    model = _rich_model()
    callers = model.callers_of(TARGET)
    lines = _neighbour_lines(build_collect_context_block(model, TARGET))
    assert len(lines) == _NEIGHBOURS_MAX_ENTRIES
    named = _paths_in(lines)
    assert named[: len(callers)] == callers
    assert named[-len(CALLED):] == list(CALLED)


def test_the_row_keeps_the_caller_order_callers_of_returns():
    """`callers_of` ranks by the caller's own blast radius; radii run against
    the alphabetical order here, so a row that sorted names alphabetically
    would list `pkg/alpha.py` first."""
    model = _rich_model()
    assert model.callers_of(TARGET) == [CALLERS[1], CALLERS[0]]
    lines = _neighbour_lines(build_collect_context_block(model, TARGET))
    assert _paths_in(lines)[:2] == [CALLERS[1], CALLERS[0]]


def test_the_row_caps_at_three_even_when_more_neighbours_have_purposes():
    import_edges, imported_by = _neighbour_graph()
    extras = tuple(f"pkg/c{i}.py" for i in range(4))
    imported_by[TARGET] = extras + CALLERS
    modules = [
        _module(TARGET),
        _module(CALLERS[0], purpose="First caller purpose for the cap test."),
        _module(CALLERS[1], purpose="Second caller purpose for the cap test."),
        _module(CALLED[0], purpose="Callee purpose for the cap test."),
        _module(TESTS[0]),
    ]
    modules += [_module(p, purpose=f"Purpose of {p}, also cut by the cap.") for p in extras]
    model = _model(
        modules,
        import_edges=import_edges,
        imported_by=imported_by,
        risk_index=(_risk(CALLERS[0], 30), _risk(CALLERS[1], 20)),
    )
    lines = _neighbour_lines(build_collect_context_block(model, TARGET))
    assert len(lines) == _NEIGHBOURS_MAX_ENTRIES
    # the cap keeps the two heaviest callers, then takes the callee it meets
    # next in the candidate order, cutting everything after the third slot
    assert _paths_in(lines) == [CALLERS[0], CALLERS[1], extras[0]]
    assert extras[1] not in "\n".join(lines)
    assert CALLED[0] not in "\n".join(lines)


def test_the_row_never_lists_the_target_as_its_own_neighbour():
    """The design decision the ticket states so it is not quietly reversed: the
    target's own purpose is never in the row, because its source is already in
    the prompt. `import_edges` drops self-edges in a produced artifact, but a
    hand-edited one can carry them."""
    import_edges, imported_by = _neighbour_graph()
    import_edges[TARGET] = (TARGET,) + CALLED
    imported_by[TARGET] = (TARGET,) + CALLERS
    target_purpose = "This is the target's own purpose and must never render."
    modules = [
        _module(TARGET, purpose=target_purpose),
        _module(CALLERS[0], purpose="A caller's purpose."),
        _module(CALLERS[1], purpose="Another caller's purpose."),
        _module(CALLED[0], purpose="A callee's purpose."),
        _module(TESTS[0]),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    row = _row_neighbours(model, TARGET, None)
    assert TARGET not in row
    assert target_purpose not in row


def test_the_row_is_absent_when_only_the_target_has_a_purpose():
    """The strongest form of the same rule: with the target's own prose as the
    only prose in the artifact, the row is absent rather than a paraphrase of
    the file the coder already has."""
    import_edges, imported_by = _neighbour_graph()
    modules = [
        _module(TARGET, purpose="Only the target was summarised."),
        _module(CALLERS[0]),
        _module(CALLERS[1]),
        _module(CALLED[0]),
        _module(TESTS[0]),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    assert _row_neighbours(model, TARGET, None) == ""
    assert _NEIGHBOURS_PREFIX not in build_collect_context_block(model, TARGET)


# ── V5-2: the purpose cut ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "limit", "want"),
    [
        ("One short sentence.", 110, "One short sentence."),
        ("First sentence. Second sentence follows.", 110, "First sentence."),
        ("First sentence! A command. Then a question?", 110, "First sentence!"),
        ("First? Next one.", 110, "First?"),
        # an abbreviation followed by a lowercase word is not a sentence end
        ("Checks (e.g. diff) are enforced first. Then the harness runs the "
        "acceptance script and reports the verdict for every gate.", 110,
         "Checks (e.g. diff) are enforced first."),
        ("Classifies accesses (e.g. `if not x:`) as guarded.", 110,
         "Classifies accesses (e.g. `if not x:`) as guarded."),
        ("No terminator anywhere in this run of words and it keeps going", 110,
         "No terminator anywhere in this run of words and it keeps going"),
        ("alpha " * 40, 110, "alpha " * 17 + "alpha"),
        ("alpha " * 40, 6, "alpha"),
        ("", 110, ""),
        ("   ", 110, ""),
        ("   \n\t  ", 110, ""),
        ("No terminator and a limit of two that fits no word", 2, "No"),
    ],
)
def test_first_sentence_takes_the_shorter_of_sentence_and_char_cut(text, limit, want):
    """AC: the first sentence or 110 chars, whichever is shorter."""
    assert _first_sentence(text, limit) == want
    assert len(_first_sentence(text, limit)) <= limit


@pytest.mark.parametrize("limit", list(range(6, 30)) + [50, 110])
def test_first_sentence_never_splits_a_word_at_the_char_ceiling(limit):
    """A cut mid-word (`"in-memor"`) would read as a bug in a line already
    labelled weak evidence, so the ceiling backs up to the last boundary."""
    got = _first_sentence(WORDY, limit)
    assert len(got) <= limit
    assert WORDY.startswith(got)
    if len(got) < len(WORDY):
        assert WORDY[len(got)] == " ", got
    assert not got.startswith(" ") and not got.endswith(" ")


def test_first_sentence_falls_back_to_the_hard_cut_when_no_boundary_fits():
    """A single 120-char token has no boundary to back up to; the ceiling
    applies as written."""
    assert _first_sentence("x" * 120, 110) == "x" * 110


def test_first_sentence_flattens_newlines_and_runs_of_spaces():
    """A summary is LLM prose and can carry a newline or a run of spaces; the
    row is one line per neighbour, so both go. The sentence cut still fires on
    the flattened text."""
    prose = "line one\nline two   and more, plus the rest of the thought"
    flattened = "line one line two and more, plus the rest of the thought"
    assert _first_sentence(prose, 110) == flattened

    cut = _first_sentence(prose, 30)
    assert len(cut) <= 30
    assert flattened.startswith(cut)
    assert flattened[len(cut)] == " "

    assert _first_sentence("line one\nline two.\nA second sentence follows here.", 110) == (
        "line one line two."
    )

    block = build_collect_context_block(_rich_model(), TARGET)
    assert len(_neighbour_lines(block)) == _NEIGHBOURS_MAX_ENTRIES
    for line in _neighbour_lines(block):
        assert "  " not in line.split(" — ", 1)[1]  # collapsed, not left a double space


def test_every_rendered_purpose_respects_the_limit():
    """The limit holds on real prose, not just on the helper's fixtures."""
    model = _rich_model()
    for line in _neighbour_lines(build_collect_context_block(model, TARGET)):
        purpose = line.split(" — ", 1)[1]
        assert purpose.endswith(_NEIGHBOURS_LLM_LABEL)
        assert len(purpose.rstrip(_NEIGHBOURS_LLM_LABEL).strip()) <= _NEIGHBOURS_PURPOSE_MAX_CHARS
    # and the sentence cut really won where it should have
    assert _row_neighbours(model, TARGET, None).split("\n")[0].split(" — ", 1)[1].startswith(
        "Runs the per-round inner loop of the autonomous pipeline."
    )


# ── V5-3: empty purposes ───────────────────────────────────────────────────


def test_a_neighbour_with_an_empty_purpose_is_skipped_not_rendered_blank():
    """AC: skipped, not rendered blank. 228 of this repo's 469 modules have no
    purpose today, so padding a gap would spend budget on exactly the thing a
    fact pack exists to avoid."""
    import_edges, imported_by = _neighbour_graph()
    modules = [
        _module(TARGET),
        _module(CALLERS[0], purpose=""),
        _module(CALLERS[1], purpose="   \n\t   "),
        _module(CALLED[0], purpose="Streams model output as it arrives."),
        _module(TESTS[0]),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    row = _row_neighbours(model, TARGET, None)
    assert row, "the surviving purpose must still render"
    assert f"{_NEIGHBOURS_PREFIX}{CALLERS[0]}" not in row
    assert f"{_NEIGHBOURS_PREFIX}{CALLERS[1]}" not in row
    for line in row.split("\n"):
        assert " — " in line
        assert line.split(" — ", 1)[1].rstrip(_NEIGHBOURS_LLM_LABEL).strip()
    assert CALLED[0] in row


def test_the_row_still_fills_three_slots_when_earlier_neighbours_are_empty():
    """Skipping must not cost the row a slot: a purpose further down the
    candidate list still earns one of the three lines. V11 recovers ~219
    test-file purposes, and every recovered one is a row that can render."""
    import_edges, imported_by = _neighbour_graph()
    import_edges[TARGET] = CALLED + ("tools/y.py", "tools/z.py")
    modules = [
        _module(TARGET),
        _module(CALLERS[0], purpose=""),
        _module(CALLERS[1], purpose=""),
        _module(CALLED[0], purpose="First callee purpose."),
        _module("tools/y.py", purpose="Second callee purpose."),
        _module("tools/z.py", purpose="Third callee purpose."),
        _module(TESTS[0]),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    lines = _neighbour_lines(build_collect_context_block(model, TARGET))
    assert len(lines) == _NEIGHBOURS_MAX_ENTRIES
    assert _paths_in(lines) == [CALLED[0], "tools/y.py", "tools/z.py"]


def test_the_row_is_absent_when_no_neighbour_has_a_purpose():
    import_edges, imported_by = _neighbour_graph()
    modules = [_module(TARGET)] + [_module(path) for path in CALLERS + CALLED + TESTS]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    assert _row_neighbours(model, TARGET, None) == ""


# ── the rest of the pack is unaffected ─────────────────────────────────────


def test_without_summaries_the_block_is_identical_to_the_pack_minus_the_row(monkeypatch):
    """AC: with no summaries in the artifact the row is absent and the rest of
    the pack is unaffected — V5 adds a row, it never rewrites one. Proven by
    comparing against the same model rendered with the row removed from
    `_PACK_ROWS` entirely."""
    import tools.auto.context_assembler as ca

    import_edges, imported_by = _neighbour_graph()
    modules = [_module(TARGET, symbols=("hub_fn",))] + [_module(path) for path in CALLERS + CALLED + TESTS]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by, test_map={TARGET: TESTS})

    block = build_collect_context_block(model, TARGET)

    monkeypatch.setattr(
        ca, "_PACK_ROWS", tuple((name, render) for name, render in _PACK_ROWS if name != "neighbours")
    )
    without_row = build_collect_context_block(model, TARGET)

    assert block == without_row
    assert _NEIGHBOURS_PREFIX not in block
    assert _NEIGHBOURS_LLM_LABEL not in block


def test_without_summaries_the_existing_rows_are_all_still_present_in_order(monkeypatch):
    """The same check from the reader's side: every row V2/V3 shipped is in the
    block a summary-less artifact still produces, in the same order."""
    import tools.auto.context_assembler as ca

    import_edges, imported_by = _neighbour_graph()
    modules = [
        _module(
            TARGET,
            symbols=("hub_fn",),
            config_reads=(ConfigRead(section="api", key="active", fallback="local"),),
        )
    ] + [_module(path) for path in CALLERS + CALLED + TESTS]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by, test_map={TARGET: TESTS})

    block = build_collect_context_block(model, TARGET)
    assert _row_sequence(block) == [
        "callers",
        "calls_into",
        "tests",
        "config_read",
        "public_symbols",
    ]

    monkeypatch.setattr(
        ca, "_PACK_ROWS", tuple((name, render) for name, render in _PACK_ROWS if name != "neighbours")
    )
    assert block == build_collect_context_block(model, TARGET)


# ── V5-5: the row goes before the static facts under a budget cut ──────────


def test_at_half_the_pack_the_row_shrinks_while_the_rows_above_it_survive():
    """AC, adapted (see the module docstring): the ticket says `neighbours`
    gives way before the rows ranked above it. V5 pinned that as "the row is
    gone at half the pack"; L6 shrinks rows instead of dropping them whole, so
    at half the pack the fact rows above keep their count and the prose row
    keeps at most the leading entries it can still afford — fewer than the
    full three — and never outranks them."""
    model = _rich_model()
    full = build_collect_context_block(model, TARGET)
    full_lines = _neighbour_lines(full)
    assert len(full_lines) == 3
    half = len(full) // 2
    block = build_collect_context_block(model, TARGET, budget=half)
    lines = _neighbour_lines(block)
    assert len(lines) < len(full_lines) and lines == full_lines[: len(lines)], (
        f"budget {half} (half of {len(full)}): the row must be shrunk from the end\n{block}"
    )
    for prefix in (_CALLERS_PREFIX, _CALLS_INTO_PREFIX, _TESTS_PREFIX):
        assert any(line.startswith(prefix) for line in block.split("\n")), (
            f"budget {half}: a row ranked above `neighbours` went too\n{block}"
        )


def test_no_budget_ever_renders_the_row_before_the_rows_above_it():
    """The ordering invariant across the whole budget range, so a regression
    that let the row outrank the static rows is caught at any budget."""
    model = _rich_model()
    full = build_collect_context_block(model, TARGET)
    for budget in range(60, len(full) + 1):
        lines = build_collect_context_block(model, TARGET, budget=budget).split("\n")
        if not any(line.startswith(_NEIGHBOURS_PREFIX) for line in lines):
            continue
        for prefix in (_CALLERS_PREFIX, _CALLS_INTO_PREFIX, _TESTS_PREFIX):
            assert any(line.startswith(prefix) for line in lines), f"budget {budget}: {lines!r}"


def test_a_cut_row_is_always_a_leading_run_of_whole_entries():
    """L6: a cut row is a prefix of the full row — whole entries dropped from
    the end, callers kept first — never a fragment of a line and never an entry
    out of order. Every surviving line still carries its `(llm)` label."""
    model = _rich_model()
    full = build_collect_context_block(model, TARGET)
    full_lines = _neighbour_lines(full)
    for budget in range(40, len(full) + 1):
        lines = _neighbour_lines(build_collect_context_block(model, TARGET, budget=budget))
        assert lines == full_lines[: len(lines)], f"budget {budget}: {lines!r}"
        assert all(line.endswith(_NEIGHBOURS_LLM_LABEL) for line in lines)


def test_a_block_with_the_row_never_exceeds_its_budget():
    model = _rich_model()
    for budget in range(60, 2600, 29):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget, f"block of {len(block)} chars exceeded budget {budget}"


def test_budget_none_still_renders_every_row_in_full():
    model = _rich_model()
    assert build_collect_context_block(model, TARGET, budget=None) == build_collect_context_block(
        model, TARGET
    )
    assert _neighbour_lines(build_collect_context_block(model, TARGET))


# ── fail-open ──────────────────────────────────────────────────────────────


def test_the_row_returns_empty_string_not_none():
    model = _model([_module(TARGET)])
    assert _row_neighbours(model, TARGET, None) == ""
    assert _row_neighbours(model, TARGET, 1) == ""
    assert _row_neighbours(model, TARGET, 4000) == ""


def test_the_row_is_absent_for_an_unknown_module():
    assert _row_neighbours(_rich_model(), "pkg/nowhere.py", None) == ""


def test_the_row_is_absent_on_an_absent_model():
    assert _row_neighbours(CollectModel(status="absent"), TARGET, None) == ""


def test_the_row_skips_a_neighbour_the_module_table_does_not_know():
    """`callers_of` does not filter against the module table (only
    `calls_into` does), so a partial artifact can hand the row a caller it has
    no record for. That neighbour is skipped, never raised on."""
    import_edges, imported_by = _neighbour_graph()
    imported_by[TARGET] = ("pkg/phantom_caller.py",) + CALLERS
    modules = [
        _module(TARGET),
        _module(CALLERS[0], purpose="A caller's purpose."),
        _module(CALLERS[1], purpose="Another caller's purpose."),
        _module(CALLED[0], purpose="A callee's purpose."),
        _module(TESTS[0]),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    row = _row_neighbours(model, TARGET, None)
    assert "pkg/phantom_caller.py" not in row
    assert row.count(_NEIGHBOURS_PREFIX) == _NEIGHBOURS_MAX_ENTRIES


def test_the_row_skips_a_neighbour_with_a_malformed_summary():
    """Fail-open house rule for the one field this row is built on. A produced
    artifact always carries an `LLMSummary`, but a hand-edited one can carry a
    summary slot that is not one at all, or one whose `purpose` is not a
    string. Both degrade to a skipped neighbour — never a raise into a run."""
    import_edges, imported_by = _neighbour_graph()
    imported_by[TARGET] = ("pkg/not_a_summary.py", "pkg/not_a_string.py") + CALLERS

    class NotASummary:
        """Whatever a half-written artifact put in the summary slot."""

    modules = [
        _module(TARGET),
        ModuleRecord(path="pkg/not_a_summary.py", summary=NotASummary()),
        ModuleRecord(
            path="pkg/not_a_string.py",
            summary=LLMSummary(purpose=["not", "a", "string"], notes=""),
        ),
        _module(CALLERS[0], purpose="A caller's purpose."),
        _module(CALLERS[1], purpose="Another caller's purpose."),
        _module(CALLED[0], purpose="A callee's purpose."),
    ]
    model = _model(modules, import_edges=import_edges, imported_by=imported_by)
    row = _row_neighbours(model, TARGET, None)
    assert "pkg/not_a_summary.py" not in row
    assert "pkg/not_a_string.py" not in row
    assert _paths_in(row.split("\n")) == [*CALLERS, *CALLED]


def test_first_sentence_returns_empty_for_a_non_string_purpose():
    """The guard is in the cutter itself, so any future row that takes prose
    from an artifact inherits it."""
    assert _first_sentence(["not", "a", "string"]) == ""
    assert _first_sentence(12345) == ""
    assert _first_sentence(None) == ""


def test_the_row_survives_a_standin_model_without_a_summary_table():
    """Fail-open house rule for the minimal stand-in the block documents: a
    model exposing only `.available`/`.module`/`.contracts_for` degrades to no
    row, never to an exception, so the block still renders."""

    class StandIn:
        available = True

        def module(self, path):
            return _module(TARGET, symbols=("hub_fn",))

        def contracts_for(self, path_or_qualname):
            return []

    block = build_collect_context_block(StandIn(), TARGET)
    assert block.startswith("COLLECT MODEL")
    assert _NEIGHBOURS_PREFIX not in block


def test_rendering_is_deterministic_across_two_independently_built_models():
    """COLLECT-3: two models built from the same tables agree byte for byte,
    so the pack a coder sees does not depend on iteration order."""
    a = _rich_model()
    b = _model(
        list(reversed(a.modules)),
        import_edges=dict(a.import_edges),
        imported_by=dict(a.imported_by),
        test_map=dict(a.test_map),
        zero_coverage=list(a.zero_coverage_list),
        risk_index=tuple(reversed(a.risk_index)),
    )
    assert build_collect_context_block(a, TARGET) == build_collect_context_block(b, TARGET)


# ── end to end: a real collect, real loader, real block ────────────────────


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    # Seed contracts and the gate map are not under test here; neutralising
    # them keeps the collected graph and test map deterministic.
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def collected_repo(tmp_path: Path) -> Path:
    """A repo whose graph is known by hand, collected with no LLM — the exact
    state of a tree whose producer never ran Pass B."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("import os\n\ndef core_fn():\n    return 1\n", encoding="utf-8")
    (pkg / "mid.py").write_text(
        "from pkg.core import core_fn\n\ndef mid_fn():\n    return core_fn()\n", encoding="utf-8"
    )
    (pkg / "leaf.py").write_text(
        "from pkg.mid import mid_fn\n\ndef leaf_fn():\n    return mid_fn()\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_core.py").write_text(
        "from pkg.core import core_fn\n\ndef test_core():\n    assert core_fn() == 1\n",
        encoding="utf-8",
    )

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    cli_mod.action_collect(tmp_path)
    return tmp_path


def test_a_collected_repo_without_an_llm_has_no_neighbours_row(collected_repo):
    """AC through the real producer and loader: no summaries on disk means no
    row, and the block is otherwise exactly the pre-V5 shape."""
    model = loader_mod.load(collected_repo)
    assert model.module("pkg/core.py").summary is None

    block = build_collect_context_block(model, "pkg/core.py")
    assert block.split("\n") == [
        "COLLECT MODEL (static facts, do not contradict):",
        "module: pkg/core.py",
        "callers: 2 modules import this (1 non-test): pkg/mid.py",
        "tests: 1 file: tests/test_core.py",
        "public_symbols: pkg/core.py:core_fn",
    ]
    assert _NEIGHBOURS_PREFIX not in block
    assert _NEIGHBOURS_LLM_LABEL not in block


def test_a_collected_repo_gains_the_row_when_summaries_are_present(collected_repo):
    """And the same artifact, with Pass B's prose attached, renders the row —
    the payoff this ticket exists for. Built in memory: the producer writes
    summaries only through the LLM, which these tests do not run."""
    model = loader_mod.load(collected_repo)
    modules = tuple(
        m.with_llm_summary(LLMSummary(purpose="The core function of the package.", notes=""))
        if m.path == "pkg/mid.py"
        else m
        for m in model.modules
    )
    patched = CollectModel(**{**model.__dict__, "modules": modules})

    row = _row_neighbours(patched, "pkg/core.py", None)
    assert row == (
        f"{_NEIGHBOURS_PREFIX}pkg/mid.py — The core function of the package."
        f"{_NEIGHBOURS_LLM_LABEL}"
    )
    block = build_collect_context_block(patched, "pkg/core.py")
    assert _NEIGHBOURS_PREFIX in block
    # the target's own prose never renders, even if it had been attached
    assert "pkg/core.py — " not in block


# ── the ticket's acceptance criteria, against this repo's own tree ──────────


@pytest.fixture(scope="module")
def live_model() -> CollectModel:
    """This repo's graph and test map, built straight from the source tree.

    `.collect/` is gitignored and rebuilt per machine, so loading the artifact
    would make these AC tests skip on every fresh checkout. The producer's own
    graph and test-map passes are cheap and need no LLM, so the model is
    assembled from them directly; no summaries are attached, which is the
    "the producer ran Pass A only" state the row must degrade to.
    """
    modules = scan_repo(REPO_ROOT)
    edges = build_edges(modules)
    reverse = reverse_index(edges)
    tmap = build_test_map(REPO_ROOT, modules)
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={key: tuple(sorted(value)) for key, value in edges.items()},
        imported_by={key: tuple(sorted(value)) for key, value in reverse.items()},
        test_map=tmap,
        zero_coverage_list=tuple(zero_coverage(tmap)),
    )


def test_live_tree_without_summaries_renders_no_neighbours_row(live_model):
    """AC on the real tree: 469 modules, none summarised, so `neighbours` is
    absent and the pack for `tools/auto/coder.py` is otherwise unchanged."""
    target = "tools/auto/coder.py"
    assert all(m.summary is None for m in live_model.modules)
    assert _row_neighbours(live_model, target, None) == ""
    assert _NEIGHBOURS_PREFIX not in build_collect_context_block(live_model, target)


def test_live_row_lists_coder_callers_first_when_summaries_are_attached(live_model):
    """The design decision, checked on the real shape: callers lead, every line
    is labelled and no other line is, and the cap holds — even when every
    module in the tree has prose."""
    target = "tools/auto/coder.py"
    modules = tuple(
        m.with_llm_summary(LLMSummary(purpose=f"LLM-written purpose for {m.path} " + "word " * 30, notes=""))
        for m in live_model.modules
    )
    patched = CollectModel(**{**live_model.__dict__, "modules": modules})

    row = _row_neighbours(patched, target, None)
    lines = row.split("\n")
    assert 0 < len(lines) <= _NEIGHBOURS_MAX_ENTRIES
    callers = patched.callers_of(target)
    assert _paths_in(lines)[: len(callers)] == callers
    assert target not in row

    block = build_collect_context_block(patched, target)
    for line in block.split("\n"):
        if line.startswith(_NEIGHBOURS_PREFIX):
            assert line.endswith(_NEIGHBOURS_LLM_LABEL)
        else:
            assert _NEIGHBOURS_LLM_LABEL not in line


@pytest.mark.skipif(
    not ARTIFACT.exists(),
    reason=".collect/ artifact not built on this checkout (gitignored); the row's "
    "absence path is pinned by the tests above",
)
def test_the_live_artifact_renders_coder_neighbours_with_real_llm_prose():
    """The first agent consumer of Pass B, against the real artifact: at most
    three labelled lines, callers first, and a purpose the target's source
    cannot supply."""
    model = loader_mod.load(REPO_ROOT)
    target = "tools/auto/coder.py"
    row = _row_neighbours(model, target, None)

    assert row, "this repo's artifact carries summaries, so the row must render"
    lines = row.split("\n")
    assert len(lines) <= _NEIGHBOURS_MAX_ENTRIES
    callers = model.callers_of(target)
    assert _paths_in(lines)[: len(callers)] == callers
    assert "tools/auto/inner_loop.py" in _paths_in(lines)

    for line in lines:
        assert line.endswith(_NEIGHBOURS_LLM_LABEL), line
        purpose = line.split(" — ", 1)[1].rstrip(_NEIGHBOURS_LLM_LABEL).strip()
        assert 0 < len(purpose) <= _NEIGHBOURS_PURPOSE_MAX_CHARS, line
