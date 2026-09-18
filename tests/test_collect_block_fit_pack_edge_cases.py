"""tests/test_collect_block_fit_pack_edge_cases.py — V2 follow-up.

Unit-level tests for the two budget helpers that have no direct coverage in the
existing suite:

  F-1  ``_minimum_form`` returns the row's floor correctly for every row type:
       shrinkable rows get ``renderer(0)``, ``public_symbols`` gets `""`, and
       the one-fact-per-line rows get their first line.
  F-2  ``_fit_pack_to_budget`` with ``owed <= 0`` returns the full forms
       unchanged.
  F-3  When every floor alone does not fit the budget, whole rows are dropped
       from the end of the order — the rendered rows stay a prefix of
       ``_PACK_ROWS``.
  F-4  A budget that fits exactly the head returns no body rows.
  F-5  A budget smaller than the head still renders the head (``parse_error``
       is not ranked content and cannot be cut).
  F-6  ``_fit_pack_to_budget`` is monotone: a larger budget never yields fewer
       rows than a smaller one.
  F-7  ``_fit_pack_to_budget`` with all rows empty returns all-empty forms.

Only structural dataclasses are built here — no artifact on disk, no LLM.
"""

from __future__ import annotations

from tools.auto.context_assembler import (
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _fit_pack_to_budget,
    _minimum_form,
    _row_cost,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ConfigRead, ContractRecord, FunctionRecord, ModuleRecord


TARGET = "pkg/hub.py"


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str, lineno: int = 1) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"{TARGET}:{name}", module=TARGET, lineno=lineno, signature=f"{name}(...)"
    )


def _model(
    modules=(),
    contracts=(),
    config_reads=(),
) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        contracts=tuple(contracts),
    )


def _hub() -> CollectModel:
    """A hub with symbols, config reads, contracts, and neighbourhood data
    so every V2/V3 row has data."""
    symbols = tuple(_symbol(f"sym_{i}") for i in range(10))
    reads = tuple(ConfigRead(section="coder", key=f"k{i}", fallback=i) for i in range(3))
    callers = tuple(f"pkg/caller_{i}.py" for i in range(5))
    calls = tuple(f"pkg/callee_{i}.py" for i in range(3))
    tests = tuple(f"tests/test_hub_{i}.py" for i in range(3))

    module = ModuleRecord(
        path=TARGET,
        public_symbols=symbols,
        config_reads=reads,
    )
    all_modules = [module]
    for p in (*callers, *calls, *tests):
        all_modules.append(ModuleRecord(path=p))

    contracts = tuple(
        ContractRecord(name=f"c{i}", description="contract body", known_edge=TARGET)
        for i in range(3)
    )
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(all_modules),
        contracts=contracts,
        imported_by={TARGET: callers},
        import_edges={TARGET: calls},
        test_map={TARGET: tests},
    )


def _render_all(model, target: str) -> "dict[str, str]":
    """Full (unbudgeted) form for every row."""
    return {
        name: render(model, target, None)
        for name, render in _PACK_ROWS
    }


def _allowing(model, target: str):
    """A renderer wrapper matching the shape ``_fit_pack_to_budget`` expects."""
    def wrapper(name: str, render):
        return lambda allowance: render(model, target, allowance)
    return {
        name: wrapper(name, render)
        for name, render in _PACK_ROWS
    }


def _head_len_with_parse_error() -> int:
    return len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py\nparse_error: SyntaxError: bad")


# ── F-1: _minimum_form returns the correct floor for every row ─────────────


def test_minimum_form_for_shrinkable_row_calls_renderer_with_zero():
    """Shrinkable rows return ``renderer(0)`` as their floor — the count alone."""
    model = _hub()
    full = _render_all(model, TARGET)
    assert full["callers"]  # sanity: the row has data
    # renderer(0) is the count-only form
    renderers = _allowing(model, TARGET)
    result = _minimum_form("callers", renderers["callers"], full["callers"])
    assert result == renderers["callers"](0)
    # the floor has no names (it is the count alone)
    assert ":" not in result or "callers:" in result


def test_minimum_form_for_public_symbols_returns_empty_string():
    """public_symbols has no floor — it is the first row cut to nothing."""
    model = _hub()
    full = _render_all(model, TARGET)
    assert full["public_symbols"]  # sanity
    renderers = _allowing(model, TARGET)
    result = _minimum_form("public_symbols", renderers["public_symbols"], full["public_symbols"])
    assert result == ""


def test_minimum_form_for_one_fact_per_line_row_returns_first_line():
    """Contract, config_read, neighbours keep their first line as the floor."""
    model = _hub()
    full = _render_all(model, TARGET)
    assert full["contract"]  # sanity
    renderers = _allowing(model, TARGET)
    result = _minimum_form("contract", renderers["contract"], full["contract"])
    assert result == full["contract"].split("\n")[0]

    assert full["config_read"]  # sanity
    result = _minimum_form("config_read", renderers["config_read"], full["config_read"])
    assert result == full["config_read"].split("\n")[0]


def test_minimum_form_for_single_line_form_is_the_form_itself():
    """When the full form is already a single line, the floor is the same."""
    model = _hub()
    full = _render_all(model, TARGET)
    # contract has exactly 3 contracts → 3 lines; take just the first line
    first_line = full["contract"].split("\n")[0]
    renderers = _allowing(model, TARGET)
    result = _minimum_form("contract", renderers["contract"], first_line)
    assert result == first_line


def test_every_shrinkable_row_is_in_minimum_form_floor_set():
    """The set of shrinkable rows must match what _minimum_form treats as
    renderer(0)."""
    for name in _SHRINKABLE_ROWS:
        model = _hub()
        full = _render_all(model, TARGET)
        renderers = _allowing(model, TARGET)
        result = _minimum_form(name, renderers[name], full[name])
        assert result == renderers[name](0), f"{name} floor is not renderer(0)"


# ── F-2: _fit_pack_to_budget with enough budget returns unchanged ───────────


def test_fit_pack_with_sufficient_budget_returns_full_forms():
    """When the full pack fits, nothing is cut."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")
    # Give a budget that is definitely enough
    total = head_len + sum(_row_cost(form) for form in full.values())
    budget = total + 100  # extra room
    result = _fit_pack_to_budget(renderers, full, head_len, budget)
    assert result == full


def test_fit_pack_with_owed_exactly_zero_returns_full_forms():
    """When budget equals the exact full cost, the pack fits exactly."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")
    total = head_len + sum(_row_cost(form) for form in full.values())
    result = _fit_pack_to_budget(renderers, full, head_len, total)
    assert result == full


# ── F-3: floors alone exceed budget → drop trailing rows ──────────────────


def test_when_floors_exceed_budget_rows_are_dropped_from_end():
    """The floors of callers, calls_into, tests must fit; when they don't,
    whole rows are dropped from the end of the order."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    # Find the total cost of all floors
    floors = {
        name: _minimum_form(name, renderers[name], form)
        for name, form in full.items()
        if form
    }

    # Budget just above head but below the cheapest floor → no rows render
    result = _fit_pack_to_budget(renderers, full, head_len, head_len + 10)
    # With a budget this tight, even one floor won't fit
    for name in dict(_PACK_ROWS):
        assert result[name] == "", f"{name} should be empty"


def test_dropped_rows_are_trailing_and_prefix_is_preserved():
    """When floors alone exceed budget, the rows that survive are a prefix of
    _PACK_ROWS (among rows that have data)."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    floors = {
        name: _minimum_form(name, renderers[name], form)
        for name, form in full.items()
        if form
    }

    # Try budgets between head_len and floor_total
    floor_total = head_len + sum(_row_cost(f) for f in floors.values())
    # Only consider rows that have data (non-empty full form)
    rows_with_data = [name for name, _ in _PACK_ROWS if full.get(name, "")]

    for budget in range(head_len + 1, floor_total + 5):
        result = _fit_pack_to_budget(renderers, full, head_len, budget)
        rendered = [name for name in rows_with_data if result[name]]
        if rendered:
            expected = rows_with_data[:len(rendered)]
            assert rendered == expected, (
                f"budget={budget}: rendered {rendered} is not a prefix of {rows_with_data}"
            )


# ── F-4: budget exactly head_len → no body rows ───────────────────────────


def test_budget_equal_to_head_len_yields_no_body_rows():
    """A budget that is exactly the head length leaves no room for any row."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    result = _fit_pack_to_budget(renderers, full, head_len, head_len)
    for name, _ in _PACK_ROWS:
        assert result[name] == "", f"{name} should be empty at budget={head_len}"


# ── F-5: budget < head_len with parse_error still renders the head ─────────


def test_budget_smaller_than_head_len_with_parse_error_still_renders_head():
    """The head is not ranked content — it cannot be cut. When the budget is
    smaller than the head, the head is still emitted. This is the documented
    behaviour: ``parse_error`` is outside the row loop, so a block whose only
    content is a parse error is still a block worth showing.

    This test documents the current behaviour, not a bug: the head is always
    emitted regardless of budget, because the budget check only applies to
    body rows.
    """
    module = ModuleRecord(
        path=TARGET,
        public_symbols=(),
        parse_error="SyntaxError: invalid syntax",
    )
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module,),
    )

    # Budget so small even the head doesn't fit
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py\nparse_error: SyntaxError: invalid syntax")
    tiny_budget = 10

    block = build_collect_context_block(model, TARGET, budget=tiny_budget)
    # The head is still present — it is not ranked content
    lines = block.split("\n")
    assert lines[0] == "COLLECT MODEL (static facts, do not contradict):"
    assert lines[1] == f"module: {TARGET}"
    assert lines[2].startswith("parse_error: ")
    # No body rows
    assert len(lines) == 3


def test_budget_smaller_than_head_len_without_parse_error_returns_empty():
    """Without a parse_error, a budget smaller than the head yields no block
    at all (the early-out: header + bare module line only → return '')."""
    module = ModuleRecord(path=TARGET, public_symbols=())
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module,),
    )

    block = build_collect_context_block(model, TARGET, budget=10)
    assert block == ""


# ── F-6: _fit_pack_to_budget is monotone ──────────────────────────────────


def test_fit_pack_is_monotone_in_budget():
    """A larger budget never yields fewer rows than a smaller one."""
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    # Sweep across the budget range and check that the set of rendered rows
    # is monotone (a row that is absent at a smaller budget may appear at a
    # larger one, but never the reverse).
    previous_rendered: set[str] = set()
    for budget in range(head_len, head_len + 2000, 3):
        result = _fit_pack_to_budget(renderers, full, head_len, budget)
        rendered = {name for name, _ in _PACK_ROWS if result[name]}
        # The rendered set must be a superset of the previous one
        assert rendered >= previous_rendered, (
            f"budget={budget}: rendered {rendered} is not a superset of {previous_rendered}"
        )
        previous_rendered = rendered


def test_fit_pack_larger_budget_never_renders_fewer_names_in_shrinkable_rows():
    """For shrinkable rows, a larger budget never yields fewer names."""
    import re
    model = _hub()
    full = _render_all(model, TARGET)
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    name_re = re.compile(r"[\w./-]+\.py(?::[\w.]+)?")

    previous_counts: dict[str, int] = {}
    for budget in range(head_len, head_len + 3000, 5):
        result = _fit_pack_to_budget(renderers, full, head_len, budget)
        for name in _SHRINKABLE_ROWS:
            form = result[name]
            if not form:
                continue
            count = len(name_re.findall(form))
            prev = previous_counts.get(name, 0)
            assert count >= prev, (
                f"budget={budget}: {name} shows {count} names after {prev}"
            )
            previous_counts[name] = count


# ── F-7: all rows empty → all-empty forms ─────────────────────────────────


def test_fit_pack_with_all_empty_rows_returns_all_empty():
    """When no row has data, the result is all empty forms."""
    model = _hub()
    full = {name: "" for name, _ in _PACK_ROWS}
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    result = _fit_pack_to_budget(renderers, full, head_len, 10000)
    assert result == full


def test_fit_pack_with_all_empty_rows_and_tight_budget():
    """Same: all empty rows, budget just above head → still all empty."""
    model = _hub()
    full = {name: "" for name, _ in _PACK_ROWS}
    renderers = _allowing(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    result = _fit_pack_to_budget(renderers, full, head_len, head_len + 1)
    assert result == full


# ── _row_cost sanity ──────────────────────────────────────────────────────


def test_row_cost_of_empty_form_is_zero():
    assert _row_cost("") == 0


def test_row_cost_of_non_empty_form_is_length_plus_one():
    assert _row_cost("hello") == 6
    assert _row_cost("a\nb") == 4  # 3 chars + 1 for the newline separator


# ── integration: build_collect_context_block with tight budgets ───────────


def test_block_with_budget_below_head_but_above_zero_returns_empty():
    """A budget that cannot fit even the head (no parse_error) returns ''.
    This is the early-out for a block with nothing substantive."""
    module = ModuleRecord(path=TARGET, public_symbols=(_symbol("alpha"),))
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module,),
    )
    block = build_collect_context_block(model, TARGET, budget=1)
    assert block == ""


def test_block_with_parse_error_never_returns_empty_regardless_of_budget():
    """A parse_error is not ranked content, so a block with a parse_error
    is always emitted — even at budget=1."""
    module = ModuleRecord(path=TARGET, parse_error="boom")
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module,),
    )
    for budget in (1, 10, 50, 100):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert block, f"budget={budget}: block with parse_error returned empty"
        assert "parse_error: boom" in block


def test_budget_cut_to_floors_only_yields_counts_not_names():
    """When the budget fits every floor but nothing more, the shrinkable rows
    render at their counts alone — no names."""
    import re
    model = _hub()
    full = _render_all(model, TARGET)
    head_len = len("COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/hub.py")

    # Find the budget that fits exactly all floors
    floors = {}
    for name, render in _PACK_ROWS:
        form = full[name]
        if not form:
            continue
        if name in _SHRINKABLE_ROWS:
            floors[name] = render(model, TARGET, 0)
        elif name == "public_symbols":
            floors[name] = ""
        else:
            floors[name] = form.split("\n")[0]

    floor_budget = head_len + sum(_row_cost(f) for f in floors.values() if f)
    # Give exactly the floor budget
    block = build_collect_context_block(model, TARGET, budget=floor_budget)

    name_re = re.compile(r"[\w./-]+\.py(?::[\w.]+)?")
    for name in _SHRINKABLE_ROWS:
        line = None
        for l in block.split("\n"):
            if l.startswith(f"{name}:"):
                line = l
                break
        if line:
            # At the floor budget, shrinkable rows show no names
            count = len(name_re.findall(line))
            assert count == 0, f"{name} at budget={floor_budget} shows {count} names: {line}"
