"""tests_bugfix/test_collect_block_contract_dedup_before_budget.py — PLAN-v2 V2.4.

Duplicate rendered contract rows were removed only while assembling the final
block. The budget allocator measured the un-deduplicated row first, so duplicate
contracts could take the room needed by ``public_symbols`` even though the final
deduplicated block fit inside the budget.
"""

from __future__ import annotations

from tools.auto.context_assembler import build_collect_context_block
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import ContractRecord, FunctionRecord, ModuleRecord


TARGET = "pkg/a.py"


def test_duplicate_contract_rows_do_not_displace_public_symbols_under_budget():
    symbol = FunctionRecord(
        qualname=f"{TARGET}:alpha",
        module=TARGET,
        lineno=1,
        signature="alpha()",
    )
    module_contract = ContractRecord(
        name="c1",
        description="body",
        known_edge=TARGET,
    )
    symbol_contract = ContractRecord(
        name="c1",
        description="body",
        known_edge=f"{TARGET}:alpha",
    )
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(ModuleRecord(path=TARGET, public_symbols=(symbol,)),),
        contracts=(module_contract, symbol_contract),
    )

    expected = "\n".join(
        (
            "COLLECT MODEL (static facts, do not contradict):",
            f"module: {TARGET}",
            "contract c1: body",
            f"public_symbols: {symbol.qualname}",
        )
    )

    assert build_collect_context_block(model, TARGET, budget=len(expected)) == expected
