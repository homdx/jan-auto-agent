"""tests_bugfix/test_collect_block_contract_dedupe_before_cut.py — PLAN-v2 V2.4.

The bug
-------
V2.4 deduplicated *identical rendered rows* so the pack would not repeat a fact.
`_row_config_read` did the right thing: it dropped byte-identical lines **before**
the budget cut measured the row, because a line the block's own dedupe removes is
budget the row never spends, and a cut sized on that count drops a line the
budget could have afforded.

`_row_contract` never got the same treatment:

    return _shrunk_lines(
        [f"contract {c.name}: {c.description}" for c in sorted(contracts, ...)],
        remaining,
    )

Two `ContractRecord`s render byte-identically whenever they share a name and a
description. That is not a contrived artifact: `COLLECT-10` allows a contract to
be both *seeded* (``provenance="static"``) and *derived*
(``provenance="derived"``), and the two records are distinct objects, so the
record-level dedupe (`if c not in contracts`) keeps both.

So under budget pressure the row measured itself including its own duplicate:
`_shrunk_lines` dropped lines from the end, the duplicate was a second *copy* of
the same fact sitting in the middle of the list, and the contract that came
after it lost its line to a duplicate that was going to be deleted anyway.

Measured before the fix, two contracts rendering the same 117-char line plus one
19-char contract, at the budget that fits head + one copy + the third contract
exactly (200 chars):

    budget=200  COLLECT MODEL (static facts, do not contradict):
                module: pkg/hub.py
                contract c1: zzz...          <- the duplicate ate 19 chars

`contract c2: small` was dropped at a budget that fit both distinct facts, and
the block came out 19 chars under budget — the duplicate claimed the room.

The fix
-------
Deduplicate the rendered lines before `_shrunk_lines`, exactly as
`_row_config_read` does. Nothing about the record-level dedupe changes, and
deduplication is still on the rendered line rather than the contract name, so two
contracts that share a name but not a description both still render.

Only structural dataclasses are built here — no git repo, no artifact on disk,
no LLM.
"""

from __future__ import annotations

from tools.auto.context_assembler import (
    _COLLECT_HEADER,
    _row_contract,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ContractRecord, FunctionRecord, ModuleRecord

TARGET = "pkg/hub.py"

# A 117-char contract line: long enough that one copy plus the short one is a
# meaningful budget, and identical to its duplicate byte for byte.
BIG = "z" * 100
LINE_C1 = f"contract c1: {BIG}"
LINE_C2 = "contract c2: small"

HEAD = len(_COLLECT_HEADER) + 1 + len(f"module: {TARGET}")
# head + exactly one copy of c1 + c2 — the budget the old code wasted on the
# duplicate.
BUDGET_FITTING_BOTH_DISTINCT = HEAD + len(LINE_C1) + 1 + len(LINE_C2) + 1


# ── helpers ────────────────────────────────────────────────────────────────


def _model(contracts, symbols=()) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH,
        modules=(
            ModuleRecord(
                path=TARGET,
                public_symbols=tuple(
                    FunctionRecord(
                        qualname=f"{TARGET}:{sym}", module=TARGET, lineno=i + 1, signature=f"{sym}(...)"
                    )
                    for i, sym in enumerate(symbols)
                ),
            ),
        ),
        contracts=tuple(contracts),
    )


def _contract(name: str, description: str, edge: str = TARGET, provenance: str = "static") -> ContractRecord:
    return ContractRecord(name=name, description=description, known_edge=edge, provenance=provenance)


def _contract_lines(block: str) -> list[str]:
    return [line for line in block.split("\n") if line.startswith("contract ")]


# ── the duplicate no longer eats a line the budget could afford ───────────


def test_a_budget_that_fits_both_distinct_facts_keeps_both():
    """The defect: two records rendering the same line plus a third contract, at
    the budget that fits head + one copy + the third. Before the fix the third
    was dropped and the block came out 19 chars short of its budget."""
    model = _model([
        _contract("c1", BIG, provenance="static"),
        _contract("c1", BIG, provenance="derived"),
        _contract("c2", "small"),
    ])

    block = build_collect_context_block(model, TARGET, budget=BUDGET_FITTING_BOTH_DISTINCT)

    assert _contract_lines(block) == [LINE_C1, LINE_C2]
    assert len(block) <= BUDGET_FITTING_BOTH_DISTINCT
    assert LINE_C2 in block, "the duplicate spent the budget c2 needed its line for"


def test_a_duplicate_contract_line_occupies_no_budget_at_all():
    """The row's own cost must exclude the line the block would delete anyway:
    with one contract and with that contract duplicated the row costs the same,
    so the cut never decides against a later line on the duplicate's behalf."""
    single = _row_contract(_model([_contract("c1", BIG)]), TARGET, None)
    doubled = _row_contract(
        _model([_contract("c1", BIG, provenance="static"), _contract("c1", BIG, provenance="derived")]),
        TARGET,
        None,
    )
    assert single == doubled == LINE_C1
    assert len(doubled) == len(single)


def test_the_duplicate_is_dropped_inside_the_row_before_the_cut():
    """Not only is the duplicate absent from the block, it is absent from the
    row itself, so a cut can never take ground from it."""
    model = _model([
        _contract("c1", BIG, provenance="static"),
        _contract("c1", BIG, provenance="derived"),
        _contract("c2", "small"),
    ])
    row = _row_contract(model, TARGET, None)
    assert row == f"{LINE_C1}\n{LINE_C2}"
    assert row.count(LINE_C1) == 1


# ── dedupe is on the rendered line, never on the name ─────────────────────


def test_same_name_different_description_both_render():
    """The description is part of the fact, so two contracts that share a name
    are not merged."""
    model = _model([_contract("c1", "first"), _contract("c1", "second")])

    block = build_collect_context_block(model, TARGET)

    assert _contract_lines(block) == ["contract c1: first", "contract c1: second"]


def test_different_names_same_description_both_render():
    model = _model([_contract("c1", "same body"), _contract("c2", "same body")])

    assert _contract_lines(build_collect_context_block(model, TARGET)) == [
        "contract c1: same body",
        "contract c2: same body",
    ]


def test_a_symbol_cited_contract_still_renders_once():
    """The record-level dedupe V2.1 kept: one contract citing both the module
    and one of its symbols is one row, not two."""
    model = _model(
        [
            _contract("c1", "the module contract"),
            _contract("c2", "the symbol contract", edge=f"{TARGET}:alpha"),
        ],
        symbols=["alpha"],
    )

    assert _contract_lines(build_collect_context_block(model, TARGET)) == [
        "contract c1: the module contract",
        "contract c2: the symbol contract",
    ]


# ── the cut stays honest with duplicates in the row ───────────────────────


def test_no_contract_line_is_split_mid_line_by_the_budget():
    """A cut row is a whole row — the budget never truncates a line, with or
    without duplicates in it."""
    model = _model([
        _contract("c1", BIG, provenance="static"),
        _contract("c1", BIG, provenance="derived"),
        _contract("c2", "y" * 200),
        _contract("c3", "y" * 200),
        _contract("c4", "y" * 200),
    ])

    for budget in range(60, 901, 13):
        for line in _contract_lines(build_collect_context_block(model, TARGET, budget=budget)):
            assert line.endswith(BIG) or line.endswith("y" * 200)


def test_the_block_never_exceeds_its_budget_with_duplicate_contracts():
    model = _model([
        _contract("c1", BIG, provenance="static"),
        _contract("c1", BIG, provenance="derived"),
        _contract("c2", "y" * 300),
        _contract("c3", "y" * 300),
        _contract("c4", "y" * 300),
    ])

    for budget in range(60, 2401, 17):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget, f"block of {len(block)} chars exceeded budget {budget}"

