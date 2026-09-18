"""tests/test_collect_block_fit_and_minimum_form.py — V2/L6 unit tests.

Direct unit tests for the two budget-allocation helpers that the existing suite
only exercises indirectly through `build_collect_context_block`:

  `_minimum_form` — the floor a budget cut may not go below. The `+N` rows
  are their count alone; `public_symbols` has none; the one-fact-per-line
  rows keep their first line.

  `_fit_pack_to_budget` — one form per row, cut from the least valuable end
  down to `budget`. Pass 1 shrinks rows toward their floors in reverse order;
  pass 2 drops trailing rows when the floors alone exceed the budget.

Also pins the within-row dedup in `_row_contract` (same treatment as
`_row_config_read`): two contracts that render identically are measured once,
not twice, so a budget cut sized on the deduplicated form does not drop a
line the budget could have afforded.

Only structural dataclasses are built here — no artifact on disk, no LLM.
"""

from __future__ import annotations

from typing import Callable

import pytest

from tools.auto.context_assembler import (
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _fit_pack_to_budget,
    _minimum_form,
    _row_cost,
    _row_contract,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ConfigRead, ContractRecord, FunctionRecord, ModuleRecord


# ── helpers ────────────────────────────────────────────────────────────────

TARGET = "pkg/fit.py"
HEAD = "COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/fit.py"
HEAD_LEN = len(HEAD)


def _symbol(name: str) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/fit.py:{name}", module=TARGET, lineno=1, signature=f"{name}(...)"
    )


def _model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts)
    )


def _contract(
    name: str, edge: str, description: str = "body", provenance: str = "static"
) -> ContractRecord:
    return ContractRecord(name=name, description=description, known_edge=edge, provenance=provenance)


# ── _minimum_form ─────────────────────────────────────────────────────────


def test_minimum_form_for_shrinkable_row_is_the_count_only_form():
    """The floor of a `+N` row is `renderer(0)` — the count alone, which
    `_fitted_names` returns for an allowance of zero."""
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(ModuleRecord(path=TARGET),)
        + tuple(ModuleRecord(path=p) for p in ("pkg/a.py", "pkg/b.py", "pkg/c.py")),
        imported_by={TARGET: ("pkg/a.py", "pkg/b.py", "pkg/c.py")},
    )
    floor = _row_contract(model, TARGET, 0)  # contract is not shrinkable; use callers
    # callers is shrinkable; its floor is the count alone
    from tools.auto.context_assembler import _row_callers

    floor = _row_callers(model, TARGET, 0)
    assert floor == "callers: 3 modules import this"
    assert "pkg/" not in floor, "the floor must carry no names"


def test_minimum_form_for_public_symbols_is_empty():
    """`public_symbols` has no floor at all — it is the first row cut to
    nothing, never the last row left standing."""
    renderer = lambda allowance: "public_symbols: alpha, beta"  # noqa: E731
    assert _minimum_form("public_symbols", renderer, "public_symbols: alpha, beta") == ""


def test_minimum_form_for_one_fact_per_line_row_is_the_first_line():
    """The floor of `contract` / `config_read` / `neighbours` is its first
    line: a line is the smallest unit of a fact they can state."""
    full = "contract c1: first\ncontract c2: second\ncontract c3: third"
    renderer = lambda allowance: full  # noqa: E731
    assert _minimum_form("contract", renderer, full) == "contract c1: first"


def test_minimum_form_for_empty_full_form_is_empty():
    """An empty row has no floor to reserve."""
    renderer = lambda allowance: ""  # noqa: E731
    assert _minimum_form("contract", renderer, "") == ""


def test_minimum_form_for_single_line_row_is_the_whole_line():
    """A single-line row cannot be shrunk: its floor is the line itself."""
    line = "config_read [coder] num_ctx (fallback=8192)"
    renderer = lambda allowance: line  # noqa: E731
    assert _minimum_form("config_read", renderer, line) == line


# ── _fit_pack_to_budget: pass 1 (shrink toward floors) ────────────────────


_ALL_ROW_NAMES = tuple(name for name, _ in _PACK_ROWS)


def _simple_pack() -> "tuple[dict[str, Callable], dict[str, str]]":
    """A pack with one shrinkable row and one non-shrinkable row, plus empty
    entries for every other row so `_fit_pack_to_budget` can iterate the full
    `_PACK_ROWS` tuple without a KeyError."""
    full_form: dict[str, str] = {name: "" for name in _ALL_ROW_NAMES}
    full_form["callers"] = "callers: 3 modules import this: a, b, c"
    full_form["contract"] = "contract c1: body one\ncontract c2: body two"

    def _shrinkable_renderer(allowance):
        if allowance is None or allowance >= 39:
            return "callers: 3 modules import this: a, b, c"
        if allowance >= 36:
            return "callers: 3 modules import this: a, b"
        if allowance >= 33:
            return "callers: 3 modules import this: a"
        return "callers: 3 modules import this"

    def _non_shrinkable_renderer(allowance):
        if allowance is None:
            return "contract c1: body one\ncontract c2: body two"
        lines = ["contract c1: body one", "contract c2: body two"]
        while lines and len("\n".join(lines)) > allowance:
            lines.pop()
        return "\n".join(lines)

    renderers: dict[str, Callable] = {name: (lambda a: "") for name in _ALL_ROW_NAMES}
    renderers["callers"] = _shrinkable_renderer
    renderers["contract"] = _non_shrinkable_renderer

    return renderers, full_form


def test_fit_pack_returns_full_pack_when_budget_is_large():
    """No overage means no cut: the full pack is returned unchanged."""
    renderers, full = _simple_pack()
    total = HEAD_LEN + sum(_row_cost(f) for f in full.values())
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, total + 100)
    assert result == full


def test_fit_pack_shrinks_from_the_least_valuable_end():
    """The cut walks `_PACK_ROWS` backwards: rows closer to the end give
    ground first. `contract` (index 4) gives before `callers` (index 0)."""
    renderers, full = _simple_pack()
    total = HEAD_LEN + sum(_row_cost(f) for f in full.values())
    budget = total - 5
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, budget)
    assert result["contract"] != full["contract"]
    assert result["callers"] == full["callers"]


def test_fit_pack_never_shrinks_below_the_floor():
    """A row is never cut below its floor: the counts of the `+N` rows survive
    any cut."""
    renderers, full = _simple_pack()
    total = HEAD_LEN + sum(_row_cost(f) for f in full.values())
    budget = total - 100
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, budget)
    if result["callers"]:
        assert "import this: a" not in result["callers"] and "import this: a," not in result["callers"]


def test_fit_pack_drops_trailing_rows_when_floors_exceed_budget():
    """When even every floor does not fit, whole rows are dropped from the end
    of the order. The rendered rows stay a prefix of `_PACK_ROWS`."""
    renderers, full = _simple_pack()
    # Budget smaller than head + any floor
    budget = HEAD_LEN - 1  # head alone exceeds budget
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, budget)
    assert all(not f for f in result.values()), "everything is dropped"


def test_fit_pack_keeps_prefix_of_rows_when_floors_fit():
    """When the floors of the leading rows fit but the later ones don't, the
    trailing rows are dropped and the leading ones keep their floors."""
    renderers, full = _simple_pack()
    floor_callers = renderers["callers"](0)
    budget = HEAD_LEN + _row_cost(floor_callers) + 1
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, budget)
    assert result["callers"] == floor_callers
    assert result["contract"] == ""


def test_fit_pack_is_monotone_in_budget():
    """A larger budget never yields a shorter pack: the cut is a function of
    the budget alone, so content is only ever returned to a row, never taken
    away."""
    renderers, full = _simple_pack()
    total = HEAD_LEN + sum(_row_cost(f) for f in full.values())
    previous_total = -1
    for budget in range(0, total + 50):
        result = _fit_pack_to_budget(renderers, full, HEAD_LEN, budget)
        current_total = sum(_row_cost(f) for f in result.values())
        assert current_total >= previous_total, (
            f"budget={budget}: total cost {current_total} < {previous_total} at a smaller budget"
        )
        previous_total = current_total


# ── _fit_pack_to_budget: edge cases ────────────────────────────────────────


def test_fit_pack_with_all_empty_forms_returns_empty():
    """A pack where every row is empty has nothing to cut."""
    renderers = {"alpha": lambda a: "", "beta": lambda a: ""}
    full = {"alpha": "", "beta": ""}
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, 100)
    assert result == {"alpha": "", "beta": ""}


def test_fit_pack_with_zero_budget_returns_empty():
    """A zero budget means nothing fits, including the floors."""
    renderers, full = _simple_pack()
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, 0)
    assert all(not f for f in result.values())


def test_fit_pack_with_budget_exactly_fitting_full_pack():
    """A budget exactly equal to the full pack's cost renders everything."""
    renderers, full = _simple_pack()
    total = HEAD_LEN + sum(_row_cost(f) for f in full.values())
    result = _fit_pack_to_budget(renderers, full, HEAD_LEN, total)
    assert result == full


# ── _row_contract within-row dedup ─────────────────────────────────────────


def test_contract_row_deduplicates_identical_lines_before_budget_cut():
    """Two contracts that render the same line (same name and description,
    different provenance) must be measured once, not twice. Without within-row
    dedup, the row's cost is inflated, and a budget cut sized on that inflated
    count drops a line the budget could have afforded."""
    module = ModuleRecord(path=TARGET, public_symbols=(_symbol("alpha"),))
    contracts = [
        _contract("c1", TARGET, "the contract", "static"),
        _contract("c1", TARGET, "the contract", "derived"),
        _contract("c2", TARGET, "another contract", "static"),
    ]
    model = _model([module], contracts)

    # The full form has two identical lines for c1, but the deduplicated form
    # has one. The block's across-row dedup would remove the duplicate anyway,
    # so the row must measure its cost without it.
    full = _row_contract(model, TARGET, None)
    lines = full.split("\n")
    assert len(lines) == 2, f"expected 2 unique lines, got {len(lines)}: {lines}"
    assert lines.count("contract c1: the contract") == 1
    assert lines.count("contract c2: another contract") == 1


def test_contract_row_dedup_does_not_inflate_budget_cost():
    """A budget that fits the deduplicated form must render both lines. Without
    within-row dedup, the row's measured cost is 2x the deduplicated cost, and
    the budget cut might drop a line that the deduplicated form could afford."""
    module = ModuleRecord(path=TARGET, public_symbols=(_symbol("alpha"),))
    # Three contracts: two render identically, one is distinct
    contracts = [
        _contract("c1", TARGET, "body A" * 10, "static"),
        _contract("c1", TARGET, "body A" * 10, "derived"),
        _contract("c2", TARGET, "body B" * 10, "static"),
    ]
    model = _model([module], contracts)

    full = _row_contract(model, TARGET, None)
    deduped_lines = full.split("\n")
    assert len(deduped_lines) == 2

    # Budget that fits head + both deduplicated lines
    budget = HEAD_LEN + sum(len(l) + 1 for l in deduped_lines)
    block = build_collect_context_block(model, TARGET, budget=budget)

    assert "contract c1: " in block
    assert "contract c2: " in block
    assert block.count("contract c1: ") == 1
    assert block.count("contract c2: ") == 1


def test_contract_within_row_dedup_matches_config_read_behavior():
    """The contract row now deduplicates within itself the same way
    `_row_config_read` does: byte-identical lines are dropped before the cut
    measures the row."""
    module = ModuleRecord(path=TARGET, public_symbols=(_symbol("alpha"),))
    contracts = [
        _contract("c1", TARGET, "same body", "static"),
        _contract("c1", TARGET, "same body", "derived"),
        _contract("c1", TARGET, "same body", "static"),
    ]
    model = _model([module], contracts)

    full = _row_contract(model, TARGET, None)
    lines = full.split("\n")
    assert lines == ["contract c1: same body"], f"got {lines}"


def test_contract_distinct_descriptions_are_not_merged():
    """Deduplication is on the rendered line, never on the name: the description
    is part of the fact."""
    module = ModuleRecord(path=TARGET, public_symbols=(_symbol("alpha"),))
    contracts = [
        _contract("c1", TARGET, "body A", "static"),
        _contract("c1", TARGET, "body B", "static"),
    ]
    model = _model([module], contracts)

    full = _row_contract(model, TARGET, None)
    lines = full.split("\n")
    assert len(lines) == 2
    assert "contract c1: body A" in lines
    assert "contract c1: body B" in lines
