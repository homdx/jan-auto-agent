"""tests_bug/test_bug_pack_body_head_seed.py

Bug (V2/V3 LOW, tools/auto/context_assembler.py:936): `_pack_body` seeded
its dedupe set with `seen = set(head)`. `head` is the fixed
`_COLLECT_HEADER` / `module: {record.path}` / `parse_error: {...}` lines
that sit OUTSIDE the row loop — the module docstring says exactly that:
they "are not ranked content, so they cannot be cut or reordered". They
are not facts any *row* is expected to restate, so nothing should ever be
deduped against them.

But because `seen` started life containing those strings, a row whose own,
genuinely distinct fact happened to render a line that is byte-identical
to one of them was silently dropped as a "duplicate" of the header — even
though it came from a different row and represented different information
that only coincidentally formatted to the same string. The row was then
miscounted as cut (`rows_cut`) rather than kept (`rows_kept`), and in the
worst case (the coincidence is the row's only line, and no other row has
content) the whole block was reported as having "nothing substantive to
add" when it did.

Fix: `seen` now starts empty (`set()`) and is only ever populated by lines
rows actually contribute to `body`, matching the class's own contract
("`seen` only ever holds facts *rows* already rendered").
"""

from __future__ import annotations

import pytest

import tools.auto.context_assembler as ca
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ConfigRead, ModuleRecord

TARGET = "pkg/hub.py"


@pytest.fixture(autouse=True)
def _restore_pack_rows():
    """Every test here monkeypatches `_PACK_ROWS` to inject a synthetic
    row; restore the real tuple afterwards so no other test in the same
    process sees the fake row."""
    original = ca._PACK_ROWS
    yield
    ca._PACK_ROWS = original


def _model_with_config_read() -> CollectModel:
    """A module with one real fact row (`config_read`) so the block isn't
    trivially empty regardless of the bug, and a second module so the
    model itself isn't degenerate."""
    modules = [
        ModuleRecord(
            path=TARGET,
            config_reads=(ConfigRead(section="coder", key="num_ctx", fallback=8192),),
        ),
        ModuleRecord(path="pkg/partner.py"),
    ]
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules))


def test_a_row_line_coincidentally_matching_the_module_head_line_is_kept():
    """A synthetic row renders `f"module: {target}"` — the exact string the
    head's own `module:` line uses — as ITS fact (imagine, e.g., a fact
    row that lists a related module by the same "module: <path>" shape).
    That row's line must still appear in the block: the header's `module:`
    line appearing once at the top does not make a row's later, distinct
    line the same fact.
    """
    def _fake_row(model, target_file, remaining):
        return f"module: {target_file}"

    ca._PACK_ROWS = (("fake", _fake_row),) + ca._PACK_ROWS

    model = _model_with_config_read()
    block = ca.build_collect_context_block(model, TARGET)
    lines = block.split("\n")

    # One "module: pkg/hub.py" from the head, and a SECOND one contributed
    # by the fake row's own line — before the fix only the head's copy
    # survived.
    assert lines.count(f"module: {TARGET}") == 2
    assert "config_read [coder] num_ctx (fallback=8192)" in lines


def test_a_row_line_coincidentally_matching_the_parse_error_head_line_is_kept():
    """Same bug, via the `parse_error:` head line instead of `module:`."""
    def _fake_row(model, target_file, remaining):
        record = model.module(target_file)
        return f"parse_error: {record.parse_error}"

    ca._PACK_ROWS = (("fake", _fake_row),) + ca._PACK_ROWS

    modules = [
        ModuleRecord(path=TARGET, parse_error="boom"),
        ModuleRecord(path="pkg/partner.py"),
    ]
    model = CollectModel(status=STATUS_FRESH, modules=tuple(modules))

    block = ca.build_collect_context_block(model, TARGET)
    lines = block.split("\n")

    assert lines.count("parse_error: boom") == 2


def test_rows_kept_counts_the_coincidentally_matching_row():
    """The M4 stats dict must count the synthetic row as kept, not cut —
    it rendered a real, non-empty line that made it into the block."""
    def _fake_row(model, target_file, remaining):
        return f"module: {target_file}"

    ca._PACK_ROWS = (("fake", _fake_row),) + ca._PACK_ROWS

    model = _model_with_config_read()
    _block, stats = ca.build_collect_context_block_stats(model, TARGET)

    # config_read + the fake row both contributed lines.
    assert stats["rows_kept"] == 2


def test_row_that_only_restates_the_head_line_and_block_is_otherwise_bare():
    """The worst-case shape the bug produced: the fake row's ONLY line
    coincidentally matches the head, and no other row has anything to
    say. Before the fix this returned "" (treated as "nothing substantive
    to add"); after the fix the fake row's line is real content and the
    block must not be empty.
    """
    def _fake_row(model, target_file, remaining):
        return f"module: {target_file}"

    ca._PACK_ROWS = (("fake", _fake_row),)  # replace entirely: no other rows

    modules = [ModuleRecord(path=TARGET), ModuleRecord(path="pkg/partner.py")]
    model = CollectModel(status=STATUS_FRESH, modules=tuple(modules))

    block = ca.build_collect_context_block(model, TARGET)
    assert block != ""
    assert block.split("\n").count(f"module: {TARGET}") == 2


# ── regression guard: real within-row / cross-row dedupe still works ───────


def test_genuine_duplicate_lines_across_rows_are_still_deduped():
    """The fix must not disable dedupe altogether — only stop seeding it
    with the head. Two rows rendering the identical, genuinely-repeated
    fact still collapse to one line."""
    def _row_one(model, target_file, remaining):
        return "shared fact line"

    def _row_two(model, target_file, remaining):
        return "shared fact line"

    ca._PACK_ROWS = (("one", _row_one), ("two", _row_two))

    modules = [ModuleRecord(path=TARGET), ModuleRecord(path="pkg/partner.py")]
    model = CollectModel(status=STATUS_FRESH, modules=tuple(modules))

    block = ca.build_collect_context_block(model, TARGET)
    assert block.count("shared fact line") == 1
