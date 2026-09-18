"""tests/test_collect_block_budget_edge_cases.py — PLAN-v2 V2.4 follow-up.

Direct unit tests for the internal budget helpers that no existing
test touches, plus an edge case the block-level tests do not pin:

  * _minimum_form: shrinkable rows return renderer(0); public_symbols
    returns ""; every other row returns its first line.
  * _fit_pack_to_budget: pass 1 shrinks toward floors in reverse
    order; pass 2 drops trailing rows when floors alone exceed budget;
    the result is monotone and never exceeds budget.
  * Budget < head_len with a parse_error: the block is empty rather
    than emitting a header that already exceeds the budget.

Only structural dataclasses are built here — no artifact on
disk, no LLM.
"""

from __future__ import annotations

from tools.auto.context_assembler import (
    _COLLECT_HEADER,
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _row_cost,
    _minimum_form,
    _fit_pack_to_budget,
    build_collect_context_block,
)
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import (
    ConfigRead,
    ContractRecord,
    FunctionRecord,
    LLMSummary,
    ModuleRecord,
)


def _symbol(name: str) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/a.py:{name}",
        module="pkg/a.py",
        lineno=1,
        signature=f"{name}(...)",
    )


def _module(
    path: str = "pkg/a.py",
    symbols=(),
    config_reads=(),
    parse_error: str | None = None,
) -> ModuleRecord:
    return ModuleRecord(
        path=path,
        public_symbols=tuple(symbols),
        config_reads=tuple(config_reads),
        parse_error=parse_error,
    )


def _model(
    modules=(),
    contracts=(),
    import_edges=None,
    imported_by=None,
    test_map=None,
) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        contracts=tuple(contracts),
        import_edges=dict(import_edges or {}),
        imported_by=dict(imported_by or {}),
        test_map={k: tuple(v) for k, v in (test_map or {}).items()},
    )


# -- _minimum_form tests -----------------------------------------------------


def test_minimum_form_shrinkable_returns_renderer_at_zero():
    """The count alone is the floor for the three +N rows — a budget
    cut may never drop the count. _minimum_form expects a 1-arg
    renderer (matching the _allowing wrapper used by
    build_collect_context_block)."""
    model = _model(
        [_module("pkg/a.py"), _module("pkg/c1.py")],
        import_edges={"pkg/a.py": ("pkg/c1.py",)},
        imported_by={"pkg/a.py": ("pkg/c1.py",)},
        test_map={"pkg/a.py": ("tests/test_a.py",)},
    )
    for name in _SHRINKABLE_ROWS:
        fn = dict(_PACK_ROWS)[name]
        full = fn(model, "pkg/a.py", None)

        def _1arg(renderer, m=model, t="pkg/a.py"):
            return lambda remaining: renderer(m, t, remaining)

        renderer = _1arg(fn)
        floor = _minimum_form(name, renderer, full)
        renderer_zero = renderer(0)
        assert floor == renderer_zero, (
            f"{name}: floor {floor!r} != renderer(0) {renderer_zero!r}"
        )
        assert floor, f"{name}: floor must be non-empty (the count is the fact)"


def test_minimum_form_public_symbols_returns_empty():
    """public_symbols has no floor — it is cut to nothing first,
    never to a bare announcement, so its floor is the empty string."""
    fn = dict(_PACK_ROWS)["public_symbols"]
    model = _model([_module("pkg/a.py", symbols=[_symbol("alpha")])])
    full = fn(model, "pkg/a.py", None)
    assert _minimum_form("public_symbols", fn, full) == ""


def test_minimum_form_other_rows_return_first_line():
    """The one-fact-per-line rows (contract, config_read, neighbours)
    keep their first line as the floor — a line is the smallest unit
    of fact they can state."""
    model = _model(
        [_module("pkg/a.py")],
        contracts=[ContractRecord(name="c1", description="body", known_edge="pkg/a.py")],
    )
    for name in ("contract", "config_read"):
        fn = dict(_PACK_ROWS)[name]
        full = fn(model, "pkg/a.py", None)
        if not full:
            continue
        floor = _minimum_form(name, fn, full)
        assert floor == full.split("\n")[0], (
            f"{name}: floor {floor!r} != first line {full.split(chr(10))[0]!r}"
        )
    model_nb = _model(
        [_module("pkg/a.py")],
        import_edges={"pkg/a.py": ("pkg/c1.py",)},
        imported_by={"pkg/a.py": ("pkg/c1.py",)},
    )
    modules = [
        ModuleRecord(path="pkg/a.py"),
        ModuleRecord(
            path="pkg/c1.py",
            summary=LLMSummary(purpose="A neighbour's purpose."),
        ),
    ]
    model_nb = _model(
        modules,
        import_edges={"pkg/a.py": ("pkg/c1.py",)},
        imported_by={"pkg/a.py": ("pkg/c1.py",)},
    )
    fn = dict(_PACK_ROWS)["neighbours"]
    full = fn(model_nb, "pkg/a.py", None)
    if full:
        floor = _minimum_form("neighbours", fn, full)
        assert floor == full.split("\n")[0], (
            f"neighbours: floor {floor!r} != first line {full.split(chr(10))[0]!r}"
        )


def test_minimum_form_empty_full_returns_empty():
    """An empty form has no floor — the row is absent."""
    fn = dict(_PACK_ROWS)["public_symbols"]
    model = _model([_module("pkg/a.py")])
    assert _minimum_form("public_symbols", fn, "") == ""


# -- _fit_pack_to_budget tests ------------------------------------------------
# Use simple string-based renderers that accept a single `remaining`
# argument (mirroring the _allowing wrapper used by build_collect_context_block).


def _make_renderer(full: str, floor: str):
    """A 1-arg renderer mirroring _fitted_names behaviour:
    returns `full` at large allowance, `floor` (count-only) at
    small positive allowance, `floor` at allowance 0, and "" when
    no floor exists and allowance is tiny."""

    def _render(remaining: int | None) -> str:
        if remaining is None:
            return full
        if remaining < 0:
            return ""
        if remaining >= len(full):
            return full
        if floor:
            return floor
        return ""

    return _render


def _string_renderers():
    return {
        "callers": _make_renderer(
            "callers: 3 modules import this: a, b, c",
            "callers: 3 modules import this",
        ),
        "calls_into": _make_renderer(
            "calls_into: 2 targets: x, y",
            "calls_into: 2 targets",
        ),
        "tests": _make_renderer(
            "tests: 2 files: t1, t2",
            "tests: 2 files",
        ),
        "neighbours": _make_renderer(
            "neighbours: pkg/c1.py — purpose",
            "neighbours: pkg/c1.py:",
        ),
        "contract": _make_renderer(
            "contract c1: body",
            "contract c1:",
        ),
        "config_read": _make_renderer(
            "config_read [a] k (fallback=v)",
            "config_read [a] k",
        ),
        "public_symbols": _make_renderer(
            "public_symbols: s1, s2",
            "",
        ),
    }


def _full_forms(renderers):
    return {name: fn(None) for name, fn in renderers.items()}


def test_fit_pack_budget_fits_everything_unchanged():
    """When the budget covers head + every full form, nothing is touched."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    budget = head_len + sum(_row_cost(f) for f in full.values()) + 100
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    assert result == full, "budget large enough: all forms unchanged"


def test_fit_pack_pass_1_shrinks_toward_floors_in_reverse_order():
    """Pass 1 walks _PACK_ROWS backwards — public_symbols gives ground
    first, callers last. With a budget that only fits head + callers floor,
    every row after callers is empty and callers is at its count alone."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    callers_floor = "callers: 3 modules import this"
    budget = head_len + len(callers_floor) + 1
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    assert result["callers"] == callers_floor, (
        f"callers not at floor: {result['callers']!r}"
    )
    assert result["calls_into"] == ""
    assert result["tests"] == ""
    assert result["contract"] == ""
    assert result["config_read"] == ""
    assert result["public_symbols"] == ""


def test_fit_pack_pass_2_drops_trailing_rows_when_floors_exceed_budget():
    """When even all floors together exceed budget, pass 2 drops
    whole rows from the end of the order. The surviving rows
    stay a prefix of _PACK_ROWS."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    floors = {
        name: _minimum_form(name, renderers[name], form)
        for name, form in full.items()
    }
    # callers floor cost is 30, head is 6: budget 36 fits head + callers
    # floor but not head + callers + calls_into floor (6+30+22=58).
    budget = head_len + sum(_row_cost(f) for f in floors.values()) // 2
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    kept = [name for name, form in result.items() if form]
    # Surviving rows must be a prefix of _PACK_ROWS.
    all_names = [n for n, _ in _PACK_ROWS]
    assert kept == all_names[: len(kept)], (
        f"surviving rows {kept} not a prefix of {all_names}"
    )
    # Callers (first row) must survive since its floor alone fits.
    assert "callers" in kept
    assert result["callers"] == _minimum_form(
        "callers", renderers["callers"], full["callers"]
    ), "surviving row is at its floor"
    # The floors that don't fit are dropped.
    floors_cost = sum(_row_cost(f) for f in floors.values())
    assert budget < head_len + floors_cost, "budget must exceed all floors"


def test_fit_pack_never_exceeds_budget():
    """Given a budget that covers the head, the result rows must
    fit within the remaining budget for every budget value."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    full_rows_cost = sum(_row_cost(f) for f in full.values())
    for budget in range(head_len, head_len + full_rows_cost + 1):
        result = _fit_pack_to_budget(renderers, full, head_len, budget)
        rows_cost = sum(_row_cost(f) for f in result.values())
        assert head_len + rows_cost <= budget, (
            f"budget={budget}: head {head_len} + rows {rows_cost} exceeds budget"
        )


def test_fit_pack_is_monotone_in_budget():
    """A larger budget never yields fewer rendered characters — the
    pack only grows (or stays the same) as budget increases."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    previous = None
    for budget in range(
        head_len,
        head_len + sum(_row_cost(f) for f in full.values()) + 1,
    ):
        result = _fit_pack_to_budget(renderers, full, head_len, budget)
        total = head_len + sum(_row_cost(f) for f in result.values())
        if previous is not None:
            assert total >= previous, (
                f"budget={budget}: {total} < {previous} — not monotone"
            )
        previous = total


def test_fit_pack_empty_forms_are_skipped():
    """Empty forms contribute nothing and are not subject to shrinking."""
    renderers = {
        "a": _make_renderer("row_a", ""),
        "b": _make_renderer("", ""),
        "c": _make_renderer("row_c", ""),
    }
    full = {name: fn(None) for name, fn in renderers.items()}
    head_len = len("header")
    budget = head_len + len("row_a") + len("row_c") + 2
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    assert result["a"] == "row_a"
    assert result["b"] == ""
    assert result["c"] == "row_c"


def test_fit_pack_owed_zero_returns_full():
    """When owed <= 0 (budget covers everything), full forms are
    returned unchanged without any shrinking."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    budget = head_len + sum(_row_cost(f) for f in full.values())
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    assert result == full


def test_fit_pack_undersized_returns_no_rows():
    """When budget < head_len, all rows are dropped (head alone is
    returned by the caller — the allocation leaves nothing for body)."""
    renderers = _string_renderers()
    full = _full_forms(renderers)
    head_len = len("header")
    result = _fit_pack_to_budget(renderers, full, head_len, head_len - 1)
    assert all(v == "" for v in result.values())


# -- budget < head_len with parse_error --------------------------------------


def test_parse_error_head_is_emitted_even_when_the_budget_cannot_hold_it():
    """The head is not ranked content: `_COLLECT_HEADER`, the `module:` line
    and a `parse_error:` line sit outside the row loop and are never cut by
    the budget (V2 rule, pinned by `test_collect_context_block_rows`). A block
    whose only content is a parse error is still a block worth showing, so a
    budget under the head's own length renders the head alone rather than
    nothing — the bridge never asks for less than 200 chars in any case."""
    model = _model([_module(parse_error="SyntaxError: invalid syntax")])
    head = [
        _COLLECT_HEADER,
        "module: pkg/a.py",
        "parse_error: SyntaxError: invalid syntax",
    ]
    for budget in range(1, len("\n".join(head))):
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        assert block == "\n".join(head), f"budget={budget}: {block!r}"


def test_block_returns_header_when_budget_equals_head_len_with_parse_error():
    """When budget exactly equals head_len with a parse_error, the
    header fits and is emitted — there is no room for a body but
    the header is valid content."""
    model = _model([_module(parse_error="SyntaxError: invalid syntax")])
    block = build_collect_context_block(model, "pkg/a.py", budget=106)
    assert _COLLECT_HEADER in block
    assert "module: pkg/a.py" in block
    assert "parse_error: SyntaxError: invalid syntax" in block


def test_block_returns_full_when_budget_exceeds_head_len_with_parse_error():
    """When budget exceeds head_len with a parse_error, the full
    block is emitted — header, parse_error, and any rows that fit."""
    symbols = [_symbol(f"s{i:02d}") for i in range(3)]
    model = _model([_module(symbols=symbols, parse_error="SyntaxError: x")])
    block = build_collect_context_block(model, "pkg/a.py", budget=500)
    assert _COLLECT_HEADER in block
    assert "module: pkg/a.py" in block
    assert "parse_error: SyntaxError: x" in block
    assert "public_symbols:" in block


def test_parse_error_is_always_present_when_block_is_nonempty_with_error():
    """When a parse_error exists and the block is non-empty, the
    parse_error line must always be present — it is outside the
    loop and cannot be cut."""
    model = _model(
        [_module(symbols=[_symbol("alpha")], parse_error="ParseError: bad")],
    )
    for budget in range(200, 2000, 50):
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        if block:
            assert "parse_error: ParseError: bad" in block, (
                f"budget={budget}: parse_error missing from non-empty block"
            )
