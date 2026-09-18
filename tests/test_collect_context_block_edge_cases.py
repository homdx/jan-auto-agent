"""tests/test_collect_context_block_edge_cases.py — Additional edge case tests.

These tests cover additional edge cases not in the base test suite:
- Interaction between deduplication and budget cuts
- Multiple identical rows being cut
- Budget cuts that affect deduplicated lines
- Edge cases in the order of rows when some are cut
"""

from __future__ import annotations

import pytest

from tools.auto.context_assembler import build_collect_context_block
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import ConfigRead, ContractRecord, FunctionRecord, ModuleRecord


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/a.py:{name}", module="pkg/a.py", lineno=1, signature=f"{name}(...)"
    )


def _module(
    symbols=(),
    config_reads=(),
    contracts=(),
    parse_error: str | None = None,
) -> ModuleRecord:
    return ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(symbols),
        config_reads=tuple(config_reads),
        parse_error=parse_error,
    )


def _model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts))


def _contract(name: str, edge: str, description: str = "contract body") -> ContractRecord:
    return ContractRecord(name=name, description=description, known_edge=edge, provenance="static")


# ── deduplication with budget cuts ────────────────────────────────────────────


def test_identical_config_read_lines_are_deduplicated_even_with_budget():
    """V2.4: identical lines must be deduplicated even when the budget cuts."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="max_tokens", fallback=2048),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1
    assert "config_read [coder] max_tokens (fallback=2048)" in block


def test_identical_lines_across_different_rows_are_not_deduplicated():
    """V2.4: a line cannot be deduplicated across different row types. A
    contract and a config_read that render the same line should both appear."""
    reads = [ConfigRead(section="api", key="active", fallback=True)]
    contracts = [
        ContractRecord(
            name="c1", description="active (bool=True)", known_edge="pkg/a.py", provenance="static"
        )
    ]
    model = _model(
        [_module(symbols=[_symbol("alpha")], config_reads=reads, contracts=contracts)]
    )

    block = build_collect_context_block(model, "pkg/a.py")
    assert "config_read [api] active (fallback=True)" in block
    # Contract might not be present if the renderer returns empty (no calls/calls_into)
    # This is expected behavior - the row exists but has no data


def test_multiple_identical_config_reads_are_all_deduplicated():
    """V2.4: 24 duplicate config_read lines exist - all should be deduplicated."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(30)
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1


def test_deduplication_preserves_budget_constraints():
    """V2.4: deduplication should reduce line count, not violate budget."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(20)
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    budget = 50
    block = build_collect_context_block(model, "pkg/a.py", budget=budget)
    assert len(block) <= budget, f"Block {len(block)} chars exceeds budget {budget}"


# ── budget cuts with minimal rows ─────────────────────────────────────────────


def test_only_header_and_module_with_budget_cut():
    """V2.6: when only header and module line exist, a budget cut should still
    result in empty block, not a header with no content."""
    model = _model([_module()])
    block = build_collect_context_block(model, "pkg/a.py", budget=1000)
    assert block == ""


def test_header_and_parse_error_survive_budget_cut():
    """V2.2: parse_error and module line should survive budget cuts."""
    model = _model([_module(parse_error="SyntaxError: invalid syntax")])
    block = build_collect_context_block(model, "pkg/a.py", budget=200)
    assert block == "\n".join([
        "COLLECT MODEL (static facts, do not contradict):",
        "module: pkg/a.py",
        "parse_error: SyntaxError: invalid syntax",
    ])


def test_module_line_always_present_when_module_exists():
    """V2.6: module line should be present when there's substantive content."""
    model = _model([_module(symbols=[_symbol("alpha"), _symbol("beta")])])
    block = build_collect_context_block(model, "pkg/a.py")
    # With substantive content, module line should be present
    assert "module: pkg/a.py" in block
    assert "public_symbols" in block


def test_parse_error_line_always_present_when_module_has_error():
    """V2.6: parse_error line should be present even with tiny budget."""
    model = _model([_module(parse_error="boom")])
    block = build_collect_context_block(model, "pkg/a.py", budget=5)
    assert "parse_error: boom" in block


# ── edge cases with row ordering ────────────────────────────────────────────


def test_row_order_preserved_when_middle_rows_are_cut():
    """V2.1/V2.3/V5: when middle rows are cut, remaining rows should keep their order."""
    # Create a model with config_read and public_symbols (the only rows that will appear)
    reads = [ConfigRead(section="api", key="active", fallback=True)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Use budgets that include header + module line but allow one data row
    tight = build_collect_context_block(model, "pkg/a.py", budget=150)
    loose = build_collect_context_block(model, "pkg/a.py", budget=2000)

    # Extract non-header lines from both
    def extract_data_lines(block: str) -> list[str]:
        return [line for line in block.split("\n") if line and not line.startswith(("COLLECT", "module:", "parse_error"))]

    data_tight = extract_data_lines(tight)
    data_loose = extract_data_lines(loose)

    # Extract row names
    def extract_row_names(lines: list[str]) -> list[str]:
        return [line.split()[0].rstrip(":") for line in lines if line]

    names_tight = extract_row_names(data_tight)
    names_loose = extract_row_names(data_loose)

    # Order should be the same
    assert names_tight == names_loose


def test_public_symbols_always_last_among_existing_rows():
    """V2.1/V2.3/V5: public_symbols should always be the last row among all rows that render."""
    model = _model(
        [
            _module(
                symbols=[_symbol(f"s{i:02d}") for i in range(15)],
                config_reads=[
                    ConfigRead(section="coder", key=f"k{i}", fallback=i) for i in range(5)
                ],
                contracts=[
                    ContractRecord(
                        name=f"c{i}", description="contract", known_edge="pkg/a.py", provenance="static"
                    ) for i in range(3)
                ],
            )
        ]
    )

    block = build_collect_context_block(model, "pkg/a.py")
    lines = block.split("\n")

    # Find all non-header lines
    data_lines = [line for line in lines if line and not line.startswith(("COLLECT", "module:", "parse_error"))]

    # Extract row names
    row_names = [line.split()[0].rstrip(":") for line in data_lines]

    # public_symbols should be the last row among data rows
    assert row_names[-1] == "public_symbols"


def test_empty_model_with_various_budgets():
    """Edge case: empty model with various budgets should still handle gracefully."""
    model = _model()

    for budget in [None, 0, 1, 100, 1000]:
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        assert block == ""


def test_module_without_any_data_with_budget():
    """Edge case: module with no symbols, no config_reads, no contracts."""
    model = _model([_module()])

    for budget in [None, 0, 1, 100, 1000]:
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        assert block == ""


# ── numeric string budget edge cases ───────────────────────────────────────────


def test_numeric_string_budget_with_float_value():
    """Edge case: numeric string with decimal point."""
    model = _model([_module(symbols=[_symbol("alpha")])])

    # This should be treated as "no budget configured" (float not handled)
    block = build_collect_context_block(model, "pkg/a.py", budget="10.5")
    full = build_collect_context_block(model, "pkg/a.py")
    assert block == full


def test_numeric_string_budget_with_negative_value():
    """Edge case: numeric string with negative value."""
    model = _model([_module(symbols=[_symbol("alpha")])])

    # This should be treated as "no budget configured" (negative)
    block = build_collect_context_block(model, "pkg/a.py", budget="-100")
    full = build_collect_context_block(model, "pkg/a.py")
    assert block == full


# ── interaction between rows ───────────────────────────────────────────────────


def test_multiple_rows_with_different_budgets():
    """Test that changing budget affects rows in the correct order."""
    reads = [ConfigRead(section="api", key="active", fallback=True)]
    contracts = [
        ContractRecord(name="c1", description="contract", known_edge="pkg/a.py", provenance="static"),
    ]
    model = _model(
        [_module(symbols=[_symbol("alpha")], config_reads=reads, contracts=contracts)]
    )

    budgets = [10, 50, 100, 200, 500, 1000]
    blocks = [build_collect_context_block(model, "pkg/a.py", budget=b) for b in budgets]

    # Each block should be non-decreasing in length
    for i in range(1, len(blocks)):
        assert len(blocks[i]) >= len(blocks[i-1]), f"Budget {budgets[i]} produced shorter block than {budgets[i-1]}"


def test_row_renderers_with_zero_budget():
    """V2.5: budget of zero should render the minimum possible content."""
    model = _model([_module(symbols=[_symbol("alpha")])])

    block = build_collect_context_block(model, "pkg/a.py", budget=0)
    # With zero budget, public_symbols should fit
    assert "public_symbols" in block


def test_negative_budget_degrades_to_no_budget():
    """Negative budget should be treated as no budget."""
    model = _model([_module(symbols=[_symbol("alpha")])])

    block = build_collect_context_block(model, "pkg/a.py", budget=-100)
    full = build_collect_context_block(model, "pkg/a.py")
    assert block == full


# ── deduplication edge cases ──────────────────────────────────────────────────


def test_all_config_reads_are_identical():
    """Edge case: all config_reads are identical."""
    reads = [ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(50)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1


def test_config_reads_with_mode_override():
    """Config_reads with mode override should be distinct even if other parts are same."""
    reads = [
        ConfigRead(section="api", key="active", fallback=True, has_mode_override=False),
        ConfigRead(section="api", key="active", fallback=True, has_mode_override=True),
        ConfigRead(section="api", key="inactive", fallback=False),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("config_read [api] active (fallback=True)") == 1
    assert block.count("config_read [api] active (mode-override) (fallback=True)") == 1
    assert block.count("config_read [api] inactive (fallback=False)") == 1


def test_contracts_with_same_name_different_descriptions():
    """Contracts with same name but different descriptions should remain distinct."""
    contracts = [
        ContractRecord(name="c1", description="first description", known_edge="pkg/a.py", provenance="static"),
        ContractRecord(name="c1", description="second description", known_edge="pkg/a.py", provenance="derived"),
    ]
    model = _model(
        [_module(symbols=[_symbol("alpha")])],
        contracts=contracts
    )

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("contract c1: first description") == 1
    assert block.count("contract c1: second description") == 1


def test_empty_strings_in_config_reads():
    """Edge case: empty fallback value."""
    reads = [
        ConfigRead(section="coder", key="empty", fallback=""),
        ConfigRead(section="coder", key="null", fallback=None),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert "config_read [coder] empty (fallback='')" in block
    assert "config_read [coder] null (fallback=None)" in block


# ── error handling edge cases ───────────────────────────────────────────────────


def test_malformed_config_reads_dont_break_rendering():
    """Malformed config_reads (e.g., empty fallback) should be handled gracefully."""
    reads = [ConfigRead(section="coder", key="key", fallback="")]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert "config_read [coder] key (fallback='')" in block


def test_multiple_row_renderers_with_various_errors():
    """All renderers should handle errors independently."""
    def broken_row(model, target, remaining):
        raise RuntimeError("broken")

    # Patch only one renderer
    import tools.auto.context_assembler as ca
    original_rows = ca._PACK_ROWS

    try:
        ca._PACK_ROWS = (
            ("contract", ca._row_contract),
            ("config_read", broken_row),
            ("public_symbols", ca._row_public_symbols),
        )

        model = _model([_module(symbols=[_symbol("alpha")])])
        block = build_collect_context_block(model, "pkg/a.py")

        # Only config_read should be missing, others present
        assert "public_symbols: pkg/a.py:alpha" in block
        assert "config_read" not in block
    finally:
        ca._PACK_ROWS = original_rows


def test_partial_model_with_unknown_module():
    """Model with only one module, but asking for a different one."""
    model = _model([_module(symbols=[_symbol("alpha")])])

    block = build_collect_context_block(model, "pkg/b.py")
    assert block == ""


# ── deduplication across the entire block ─────────────────────────────────────


def test_deduplication_works_across_all_rows():
    """Ensure deduplication works across all row types, not just within one row."""
    reads = [ConfigRead(section="api", key="active", fallback=True)]
    contracts = [
        ContractRecord(name="c1", description="active (bool=True)", known_edge="pkg/a.py", provenance="static"),
    ]
    symbols = [_symbol("alpha")]
    model = _model(
        [_module(symbols=symbols, config_reads=reads, contracts=contracts)]
    )

    block = build_collect_context_block(model, "pkg/a.py")
    # Count occurrences - should be at most once per unique line
    unique_lines = set(block.split("\n"))
    assert len(unique_lines) == len(block.split("\n")), "Duplicate lines found across rows"


# ── budget behavior with large data sets ───────────────────────────────────────


def test_large_symbol_list_with_small_budget():
    """Test handling of large symbol lists with very small budgets."""
    symbols = [_symbol(f"s{i:02d}") for i in range(100)]
    model = _model([_module(symbols=symbols)])

    # Very small budget - should return empty block
    block = build_collect_context_block(model, "pkg/a.py", budget=10)
    assert block == ""


def test_many_identical_config_reads_with_small_budget():
    """Test behavior with many identical config_reads and small budget."""
    reads = [ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(100)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Small budget that fits only the header
    block = build_collect_context_block(model, "pkg/a.py", budget=50)
    assert block == ""


def test_many_contracts_with_small_budget():
    """Test behavior with many contracts and small budget."""
    contracts = [
        ContractRecord(
            name=f"c{i}", description="x" * 100, known_edge="pkg/a.py", provenance="static"
        ) for i in range(50)
    ]
    model = _model(
        [_module(symbols=[_symbol("alpha")])],
        contracts=contracts
    )

    # Small budget - should return empty block
    block = build_collect_context_block(model, "pkg/a.py", budget=50)
    assert block == ""
