"""tests_bugfix/test_collect_block_budget_deduplication.py — BUGFIX tests for budget cuts with deduplication.

These tests ensure that deduplication works correctly when budgets cut the block.
The interaction between deduplication and budget cuts is subtle:
1. Deduplication should happen before budget cuts
2. Deduplication should reduce line count but not affect budget calculation
3. A budget cut that would fit deduplicated lines should still work correctly
"""

from __future__ import annotations

import pytest

from tools.auto.context_assembler import build_collect_context_block
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import ConfigRead, FunctionRecord, ModuleRecord


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/a.py:{name}", module="pkg/a.py", lineno=1, signature=f"{name}(...)"
    )


def _module(
    symbols=(),
    config_reads=(),
) -> ModuleRecord:
    return ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(symbols),
        config_reads=tuple(config_reads),
    )


def _model(modules=()) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules))


# ── budget cuts with deduplication ────────────────────────────────────────────


def test_deduplication_before_budget_cut():
    """Deduplication should happen before the budget check, not after."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="max_tokens", fallback=2048),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # With a budget that only fits one line after dedup
    # Without deduplication, we'd have 3 lines, but with dedup, we have 1
    # The budget check should pass after dedup
    block = build_collect_context_block(model, "pkg/a.py", budget=150)
    # The line should be present (deduplication worked)
    assert "config_read [coder] num_ctx (fallback=8192)" in block


def test_deduplication_saves_budget():
    """Deduplication should save budget by reducing line count."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Without dedup, 3 lines would be much longer
    # With dedup, only 1 line
    block = build_collect_context_block(model, "pkg/a.py", budget=200)
    assert "config_read [coder] num_ctx (fallback=8192)" in block
    assert len(block.split("\n")) <= 4  # Header + module + 1 deduped line


def test_multiple_deduped_lines_with_budget():
    """Multiple deduped lines should all appear once, regardless of budget."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="api", key="active", fallback=True),
        ConfigRead(section="api", key="active", fallback=True),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Even with a tight budget, both deduped lines should appear
    block = build_collect_context_block(model, "pkg/a.py", budget=150)
    assert "config_read [coder] num_ctx (fallback=8192)" in block
    assert "config_read [api] active (fallback=True)" in block
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1
    assert block.count("config_read [api] active (fallback=True)") == 1


def test_deduped_lines_preserve_order():
    """Deduped lines should preserve the order they appear in the source."""
    reads = [
        ConfigRead(section="api", key="active", fallback=True),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="api", key="active", fallback=True),  # Duplicate
        ConfigRead(section="coder", key="max_tokens", fallback=2048),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py", budget=300)

    # Extract the order of lines
    lines = [line for line in block.split("\n") if line.startswith("config_read ")]
    assert len(lines) == 3  # api, num_ctx, max_tokens (api is deduped)
    assert lines[0] == "config_read [api] active (fallback=True)"
    assert lines[1] == "config_read [coder] num_ctx (fallback=8192)"
    assert lines[2] == "config_read [coder] max_tokens (fallback=2048)"


def test_deduplication_with_partial_row_fits():
    """When a budget cut happens mid-block, deduplication should still work correctly."""
    # Create many config_reads, most of which will be deduped
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="api", key="active", fallback=True),
        ConfigRead(section="api", key="active", fallback=True),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Tight budget that fits some deduped lines but not all
    block = build_collect_context_block(model, "pkg/a.py", budget=250)

    # At least the first deduped line should appear
    assert "config_read [api] active (fallback=True)" in block


def test_empty_config_reads_list_with_budget():
    """Empty config_reads list should handle budget cuts gracefully."""
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=())])

    for budget in [0, 10, 100, 1000]:
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        # Should still have header and module line if budget is sufficient
        if budget >= 150:
            assert "module:" in block


def test_single_config_read_with_budget():
    """Single config_read should handle budget cuts correctly."""
    reads = [ConfigRead(section="coder", key="num_ctx", fallback=8192)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Even small budget should work (deduped list is just 1 line)
    block = build_collect_context_block(model, "pkg/a.py", budget=150)
    # Should have at least header + module line
    assert "module:" in block


def test_many_deduped_lines_but_tight_budget():
    """Many identical config_reads but tight budget - should only show one after dedup."""
    reads = [ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(100)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Very tight budget - should fit header + module + 1 deduped line
    block = build_collect_context_block(model, "pkg/a.py", budget=150)
    assert len(block) <= 150
    assert "config_read [coder] num_ctx (fallback=8192)" in block


def test_deduplication_does_not_affect_budget_calculation():
    """Deduplication should not affect the initial budget calculation."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # The block should be shorter than without deduplication would be
    block = build_collect_context_block(model, "pkg/a.py", budget=200)
    assert len(block) < 200  # Should fit


def test_budget_increase_with_deduped_lines():
    """When budget increases, deduped lines should still appear correctly."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="api", key="active", fallback=True),
        ConfigRead(section="api", key="active", fallback=True),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Small budget - should fit fewer lines
    small_block = build_collect_context_block(model, "pkg/a.py", budget=100)
    # Large budget - should fit more lines (or same if already all fit)
    large_block = build_collect_context_block(model, "pkg/a.py", budget=500)

    # Large budget should have at least as much content
    assert len(large_block) >= len(small_block)


def test_deduped_lines_in_large_module():
    """Test deduplication in a module with many identical config_reads."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(50)
    ]
    model = _model([_module(symbols=[_symbol(f"s{i:02d}") for i in range(30)], config_reads=reads)])

    # Should show only 1 deduped line, not 50
    block = build_collect_context_block(model, "pkg/a.py", budget=300)
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1


def test_config_reads_with_different_sections_and_keys():
    """Config_reads with different sections/keys should not be deduped."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),  # Same
        ConfigRead(section="coder", key="max_tokens", fallback=2048),
        ConfigRead(section="api", key="active", fallback=True),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Should show 3 lines (2 deduped + 2 unique)
    block = build_collect_context_block(model, "pkg/a.py", budget=200)
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1
    assert block.count("config_read [coder] max_tokens (fallback=2048)") == 1
    assert block.count("config_read [api] active (fallback=True)") == 1


def test_deduplication_with_complex_fallback_values():
    """Deduplication should work with complex fallback values."""
    reads = [
        ConfigRead(section="coder", key="value", fallback=8192),
        ConfigRead(section="coder", key="value", fallback=8192),
        ConfigRead(section="coder", key="value", fallback=4096),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    # Should show 2 lines (1 deduped for 8192, 1 unique for 4096)
    block = build_collect_context_block(model, "pkg/a.py", budget=200)
    assert block.count("config_read [coder] value (fallback=8192)") == 1
    assert block.count("config_read [coder] value (fallback=4096)") == 1
