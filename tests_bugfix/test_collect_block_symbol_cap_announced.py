"""tests_bugfix/test_collect_block_symbol_cap_announced.py — PLAN-v2 V2.3.

The bug
-------
`build_collect_context_block` capped the symbol list at a hard, silent
`[:20]`:

    symbols = ", ".join(s.qualname for s in record.public_symbols[:20])
    lines.append(f"public_symbols: {symbols}")

A reader of that line has no signal that it is partial. Re-measured against the
artifact on disk (`.collect/artifact.json`, 483 modules, 4093 symbols):

    modules above 20 symbols       28
    symbols dropped silently       296

Every one of those 296 symbols was dropped from 28 blocks that looked complete
— the largest module (`tests/test_check_runbook.py`, 65 symbols) advertised 20
and said nothing about the other 45. `CollectBridge._format_module_block` in
the same package already did this the honest way ("… and N more symbol(s) not
listed"); the inject block did not, and the coder is the one consumer that
reads it.

The fix
-------
No budget -> the whole list, no cap at all. With a budget -> cut at a symbol
boundary and announce the remainder in the same style:

    public_symbols: pkg/a.py:alpha, pkg/a.py:beta … (+28 more, cut for budget)

If even one symbol plus the announcement does not fit, the row is absent rather
than a bare "nothing listed" note, so a tight budget is not spent on nothing.

Only structural dataclasses are built here — no git repo, no artifact on disk,
no LLM.
"""

from __future__ import annotations

import re

import pytest

from tools.auto.context_assembler import (
    _SYMBOLS_CUT_NOTE,
    _row_public_symbols,
    build_collect_context_block,
)
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import FunctionRecord, ModuleRecord


PREFIX = "public_symbols: "
NOTE_RE = re.compile(r"… \(\+(\d+) more, cut for budget\)$")


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/a.py:{name}", module="pkg/a.py", lineno=1, signature=f"{name}(...)"
    )


def _model(count: int) -> CollectModel:
    symbols = [_symbol(f"s{i:02d}") for i in range(count)]
    return CollectModel(
        status=STATUS_FRESH, modules=(ModuleRecord(path="pkg/a.py", public_symbols=tuple(symbols)),)
    )


def _listed(block: str) -> list[str]:
    """The symbol names the row lists, excluding the announcement."""
    for line in block.split("\n"):
        if line.startswith(PREFIX):
            body = line[len(PREFIX):]
            match = NOTE_RE.search(body)
            if match:
                body = body[: match.start() - 1]  # strip the space before the "…"
            return [name.strip() for name in body.split(",") if name.strip()]
    return []


def _announced(block: str) -> int:
    """The remainder the row announces, or 0 when the row is complete/absent."""
    for line in block.split("\n"):
        if line.startswith(PREFIX):
            match = NOTE_RE.search(line)
            return int(match.group(1)) if match else 0
    return 0


# ── the silent cap is gone ─────────────────────────────────────────────────


def test_a_module_above_the_old_cap_lists_every_symbol_with_no_budget():
    """28 modules had more than 20 symbols and 296 of them were dropped with no
    signal. With no budget the list must be the whole list."""
    model = _model(65)

    block = build_collect_context_block(model, "pkg/a.py")
    listed = _listed(block)

    assert len(listed) == 65
    assert listed == [f"pkg/a.py:s{i:02d}" for i in range(65)]
    assert _announced(block) == 0
    assert "cut for budget" not in block


def test_no_budget_never_announces_a_remainder():
    """A complete list must not claim to be partial — the announcement is only
    ever a consequence of a budget."""
    for count in (1, 20, 21, 200):
        block = build_collect_context_block(_model(count), "pkg/a.py")
        assert _announced(block) == 0
        assert _SYMBOLS_CUT_NOTE.strip() not in block


def test_every_symbol_of_a_big_module_is_in_its_own_block():
    """The one fact the old code could never give: the reader can count."""
    model = _model(65)
    block = build_collect_context_block(model, "pkg/a.py")
    for i in range(65):
        assert f"pkg/a.py:s{i:02d}" in block


# ── the budget cut is announced ────────────────────────────────────────────


def test_a_budget_cut_announces_the_remainder_instead_of_dropping_silently():
    model = _model(30)

    block = build_collect_context_block(model, "pkg/a.py", budget=200)

    assert NOTE_RE.search(block), "a partial list must announce itself"
    assert len(_listed(block)) + _announced(block) == 30
    assert _announced(block) > 0


def test_the_announcement_is_arithmetically_exact_across_budgets():
    """The promise is "listed + announced == total". It must hold for every
    budget, not just a few hand-picked ones."""
    for count in (1, 5, 20, 21, 65):
        model = _model(count)
        for budget in range(40, 900, 11):
            block = build_collect_context_block(model, "pkg/a.py", budget=budget)
            listed, announced = len(_listed(block)), _announced(block)
            assert listed + announced <= count, (count, budget, listed, announced)
            if listed:
                assert listed + announced == count, (count, budget, listed, announced)


def test_the_announced_remainder_names_the_right_number():
    """A reader must be able to trust the number."""
    model = _model(40)
    block = build_collect_context_block(model, "pkg/a.py", budget=300)
    line = next(l for l in block.split("\n") if l.startswith(PREFIX))
    remainder = 40 - len(_listed(block))
    assert line.split("…")[1].strip() == f"(+{remainder} more, cut for budget)"
    assert line.endswith(_SYMBOLS_CUT_NOTE.format(extra=remainder))


def test_listed_symbols_are_the_source_order_prefix():
    """Cutting must keep the head of the list in order, never a shuffled subset
    — the coder reads symbols top-to-bottom against the file."""
    model = _model(40)
    block = build_collect_context_block(model, "pkg/a.py", budget=160)
    listed = _listed(block)
    assert listed == [f"pkg/a.py:s{i:02d}" for i in range(len(listed))]


def test_exactly_one_public_symbols_row_is_ever_rendered():
    """A budget cut must shrink the row, never split it into two."""
    model = _model(65)
    for budget in list(range(40, 4001, 13)) + [None]:
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        rows = [l for l in block.split("\n") if l.startswith(PREFIX)]
        assert len(rows) <= 1, f"budget={budget}: {len(rows)} public_symbols rows"


def test_more_budget_never_lists_fewer_symbols_than_less_budget():
    """Regression pin for the trim itself: a larger budget must render a
    superset of symbols, because the announcement eats room and has to be paid
    for explicitly rather than discovered after the cut."""
    for count in (21, 40, 65):
        model = _model(count)
        previous = 0
        for remaining in range(0, 2000, 3):
            line = _row_public_symbols(model, "pkg/a.py", remaining)
            current = len(_listed(f"{PREFIX}{line}" if line else ""))
            assert current >= previous, f"count={count} remaining={remaining}: {current} < {previous}"
            previous = current


def test_nothing_fits_yields_no_row_and_no_bare_announcement():
    """An announcement with zero symbols listed says nothing and costs budget;
    in that case the row is absent, not empty."""
    model = _model(65)

    for budget in (0, 1, 40, 60):
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        rows = [l for l in block.split("\n") if l.startswith(PREFIX)]
        if rows:
            assert _listed(block), f"budget={budget}: row lists nothing but announces"


def test_a_budget_tight_enough_for_nothing_returns_no_block_at_all():
    model = _model(65)
    assert build_collect_context_block(model, "pkg/a.py", budget=50) == ""


# ── no other row got a silent cap along the way ────────────────────────────


def test_contracts_and_config_reads_are_never_truncated_by_count():
    """The cap only ever existed on symbols; the other two rows list every
    entry they have, so they need no announcement at all."""
    from tools.collect.model import ConfigRead, ContractRecord

    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=(_symbol("s00"),),
        config_reads=tuple(ConfigRead(section="coder", key=f"k{i}", fallback=i) for i in range(30)),
    )
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module,),
        contracts=tuple(
            ContractRecord(name=f"c{i}", description="body", known_edge="pkg/a.py") for i in range(15)
        ),
    )

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("config_read [coder] ") == 30
    assert block.count("contract ") == 15
    assert "more" not in block


def test_the_source_no_longer_slices_the_symbol_list():
    """The bug itself: a literal index slice of `public_symbols`. Absent a
    budget the row must not be able to truncate the list at all."""
    source = (
        __import__("pathlib").Path(
            __import__("tools.auto.context_assembler", fromlist=["x"]).__file__
        )
    ).read_text(encoding="utf-8")

    assert "public_symbols[:20]" not in source
    assert "public_symbols[: " not in source
