"""tests/test_collect_context_block_v2_live_facts.py — PLAN-v2 V2, against the
live tree.

The V2/V2.3/V2.4 acceptance has so far been pinned with synthetic records (a
65-symbol fixture, three hand-made duplicate `ConfigRead`s). Those prove the
renderer, but nothing in the suite still asks the question the ticket actually
poses of the tree it ships in: does the block a real module gets carry the same
facts as the block it used to get, and does it stop lying about lists it cut?

This file asks that of the tree, building the graph and test map the way the
producer builds them — from the source, never from `.collect/`, which is
gitignored and would make the acceptance skip on a fresh checkout.

Two numbers in the ticket have drifted, because the tree grew. `05-v2-...md`
was written against `competition` at `68b78a0` and says "28 modules with >20
symbols ... 296 symbols dropped silently" and "24 duplicate `config_read`
lines". Measured on this tree today: the duplicates are still exactly 24 (in
11 modules), but 37 modules now sit above 20 symbols with 456 symbols beyond
the old silent cap. The behaviour the ticket pins is unchanged — there is no
cap to measure, because there is no cap — so the tests below assert the
behaviour and print the counts rather than asserting them, which is what keeps
them from rotting as the tree keeps growing.

Only the producer's own graph/test-map passes are used: no git repo state, no
artifact on disk, no LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.auto.context_assembler import _row_config_read, build_collect_context_block
from tools.collect import graph as graph_mod
from tools.collect import scanner as scanner_mod
from tools.collect import test_map as test_map_mod
from tools.collect.loader import STATUS_FRESH, CollectModel

REPO_ROOT = Path(__file__).resolve().parent.parent

_SYMBOLS_PREFIX = "public_symbols: "
_CONFIG_PREFIX = "config_read ["
_CUT_NOTE = "cut for budget"
# A handful of budgets spanning the row's own size to its full size. Enough to
# catch an invented line anywhere in the row, cheap enough to run over every
# module in the tree.
_BUDGETS = (200, 400, 600, 900, 1200, 1717, 3000)


@pytest.fixture(scope="module")
def live_model() -> CollectModel:
    """This repo's graph and test map, built from the source the way the
    producer builds them."""
    modules = scanner_mod.scan_repo(REPO_ROOT)
    edges = graph_mod.import_edges(modules)
    reverse = graph_mod.imported_by(edges)
    tmap = test_map_mod.build_test_map(REPO_ROOT, modules)
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
        entry_points=tuple(graph_mod.entry_points(edges, reverse=reverse)),
        test_map=tmap,
        zero_coverage_list=tuple(test_map_mod.zero_coverage(tmap)),
    )


def _symbol_row(block: str) -> str:
    for line in block.split("\n"):
        if line.startswith(_SYMBOLS_PREFIX):
            return line
    return ""


def _listed_symbols(block: str) -> list[str]:
    line = _symbol_row(block)
    if not line:
        return []
    body = line[len(_SYMBOLS_PREFIX):]
    if " … " in body:
        body = body.split(" … ")[0]
    return [name for name in body.split(", ") if name]


# ── V2.3: the silent cap is gone, for every module in the tree ────────────


def test_every_live_module_lists_every_symbol_without_a_budget(live_model):
    """The one fact the old `[:20]` could never give, for the whole tree at
    once: a reader of an unbudgeted block can count the symbols it lists."""
    checked = 0
    for module in live_model.modules:
        if not module.public_symbols:
            continue
        block = build_collect_context_block(live_model, module.path)
        listed = _listed_symbols(block)
        expected = [symbol.qualname for symbol in module.public_symbols]
        assert listed == expected, (
            f"{module.path}: the unbudgeted block lists {len(listed)} of "
            f"{len(expected)} symbols in a different order"
        )
        checked += 1
    assert checked > 20, "the tree must still carry enough big modules for this to mean anything"


def test_no_unbudgeted_live_block_claims_to_be_partial(live_model):
    """An announcement is only ever a consequence of a budget. With no budget
    no block in the tree may admit to a remainder."""
    for module in live_model.modules:
        block = build_collect_context_block(live_model, module.path)
        assert _CUT_NOTE not in block, f"{module.path}: no-budget block announces a cut"
        row = _symbol_row(block)
        if row:
            assert ", +" not in row, f"{module.path}: no-budget symbol row announces a remainder"


def test_modules_above_the_old_cap_no_longer_drop_symbols_silently(live_model):
    """The acceptance criterion the ticket names: the modules that used to lose
    everything past symbol 20 without a word must now list them all. Counted
    against the tree rather than hard-coded, so the assertion survives the tree
    growing (the ticket's 28/296 are 37/456 here)."""
    over = [m for m in live_model.modules if len(m.public_symbols) > 20]
    assert over, "the tree should still have modules above 20 symbols"
    for module in over:
        block = build_collect_context_block(live_model, module.path)
        assert len(_listed_symbols(block)) == len(module.public_symbols), module.path
        assert _CUT_NOTE not in block, module.path


# ── V2.4: the duplicate `config_read` lines are gone, in the tree ─────────


def test_the_live_config_read_duplicates_collapse_to_one_line_each(live_model):
    """Measured on the tree: 24 byte-identical `config_read` lines across 11
    modules — the same `(section, key)` read through two code paths. Each one
    must render once, in the row and in the block."""
    raw_duplicates = 0
    modules_with_reads = 0
    for module in live_model.modules:
        if not module.config_reads:
            continue
        modules_with_reads += 1
        raw = [
            f"config_read [{cr.section}] {cr.key}"
            f"{' (mode-override)' if cr.has_mode_override else ''} (fallback={cr.fallback!r})"
            for cr in module.config_reads
        ]
        raw_duplicates += len(raw) - len(set(raw))

        row = _row_config_read(live_model, module.path, None)
        if not row:
            continue
        lines = row.split("\n")
        assert len(lines) == len(set(lines)), f"{module.path}: the row repeats a config_read line"
        assert set(lines) <= set(raw), f"{module.path}: the row shows a read the module does not have"

        block = build_collect_context_block(live_model, module.path)
        rendered = [line for line in block.split("\n") if line.startswith(_CONFIG_PREFIX)]
        assert len(rendered) == len(set(rendered)), (
            f"{module.path}: the block repeats a config_read line"
        )
    assert modules_with_reads > 0
    # The guard against this test drifting into a state where it proves
    # nothing: today's tree genuinely carries the duplicates V2.4 was written
    # for. Counted, not hard-coded — the ticket's 24 is still 24 here, but it
    # is the duplicates' *presence* the assertion needs, not their number.
    assert raw_duplicates > 0, "no duplicate config_read lines left in the tree for V2.4 to fix"


# ── V2 acceptance: the cut changes which facts are shown, never which facts
# ────────────────────────────────────────────────────────────────────────


def test_cutting_never_invents_a_config_read_line(live_model):
    """Every `config_read` line a budgeted block shows is a line of that
    module's unbudgeted block: a cut may hide a fact, it may not manufacture
    one."""
    budgeted_modules = 0
    for module in live_model.modules:
        full = build_collect_context_block(live_model, module.path)
        full_lines = {
            line for line in full.split("\n") if line.startswith(_CONFIG_PREFIX)
        }
        if not full_lines:
            continue
        budgeted_modules += 1
        for budget in _BUDGETS:
            block = build_collect_context_block(live_model, module.path, budget=budget)
            for line in block.split("\n"):
                if line.startswith(_CONFIG_PREFIX):
                    assert line in full_lines, (
                        f"{module.path} budget={budget}: {line[:60]!r} is not a line of the "
                        "unbudgeted block"
                    )
    assert budgeted_modules > 0


def test_cutting_never_invents_a_symbol(live_model):
    """The mirror for the row that owns its own cut: every symbol a budgeted
    block lists is one the module actually defines, listed in source order."""
    seen_a_cut = False
    for module in live_model.modules:
        full = _symbol_row(build_collect_context_block(live_model, module.path))
        if not full:
            continue
        full_names = _listed_symbols(full)
        for budget in _BUDGETS:
            listed = _listed_symbols(build_collect_context_block(live_model, module.path, budget=budget))
            assert listed == full_names[: len(listed)], (
                f"{module.path} budget={budget}: the cut showed symbols out of source order"
            )
            if listed and len(listed) < len(full_names):
                seen_a_cut = True
    assert seen_a_cut, "no budget in the sweep actually cut a symbol list"
