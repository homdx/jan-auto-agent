"""tests/test_collect_context_block_rows.py — PLAN-v2 V2.

`build_collect_context_block` is now an ordered row list (`_PACK_ROWS`) rather
than a single pass over the module record. Pins for what V2 promises:

* the row order IS the priority, and `public_symbols` is last;
* the header, the `module:` line and `parse_error:` stay outside the loop, so
  they can neither be reordered nor cut;
* identical rendered lines are deduplicated (24 of today's `config_read`
  lines are byte-identical duplicates);
* `budget=None` renders everything, so every pre-V2 caller keeps working until
  V6 wires `max_context_chars_auto` in;
* a row that does not fit is skipped for the next, shorter row, and the block
  never exceeds its budget;
* the "header + bare module line only -> return `''`" early-out is preserved;
* malformed budgets and broken row renderers are fail-open, never raising into
  a run.

Only structural dataclasses are built here — no git repo, no artifact on disk,
no LLM.
"""

from __future__ import annotations

import pytest

from tools.auto.context_assembler import (
    _COLLECT_HEADER,
    _PACK_ROWS,
    _row_contract,
    _row_config_read,
    _row_public_symbols,
    build_collect_context_block,
)
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import (
    ConfigRead,
    ContractRecord,
    FunctionRecord,
    ModuleRecord,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str, lineno: int = 1) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"pkg/a.py:{name}",
        module="pkg/a.py",
        lineno=lineno,
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


def _model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts)
    )


def _contract(
    name: str, edge: str, description: str = "contract body", provenance: str = "static"
) -> ContractRecord:
    return ContractRecord(name=name, description=description, known_edge=edge, provenance=provenance)


def _symbol_line(block: str) -> str:
    """The block's single `public_symbols:` row, or `""` when the row is absent."""
    for line in block.split("\n"):
        if line.startswith("public_symbols: "):
            return line
    return ""


# ── row order ──────────────────────────────────────────────────────────────


def test_pack_rows_are_ordered_contract_then_config_then_public_symbols():
    """V2.1/V2.3/V3/V5: the tuple order IS the priority, and public_symbols is
    last because it is the one row the target file's own source makes
    redundant. V3 prepended the neighbourhood rows and V5 put `neighbours`
    above the V2 rows; the three rows V2 ported keep their relative order and
    their positions at the tail."""
    names = [name for name, _ in _PACK_ROWS]
    assert names == [
        "callers",
        "calls_into",
        "tests",
        "neighbours",
        "contract",
        "config_read",
        "public_symbols",
    ]
    assert names[-3:] == ["contract", "config_read", "public_symbols"]
    assert names[-1] == "public_symbols"


def test_pack_rows_are_all_callable_renderers():
    assert len(_PACK_ROWS) == 7
    for name, render in _PACK_ROWS:
        assert isinstance(name, str) and name
        assert callable(render)


def test_public_symbols_renders_last_in_the_block():
    module = _module(
        symbols=[_symbol("alpha"), _symbol("beta")],
        config_reads=[ConfigRead(section="coder", key="num_ctx", fallback=8192)],
    )
    model = _model([module], [_contract("c1", "pkg/a.py")])

    lines = build_collect_context_block(model, "pkg/a.py").split("\n")

    assert [l.split(" ")[0].rstrip(":") for l in lines[2:]] == [
        "contract",
        "config_read",
        "public_symbols",
    ]
    assert lines[-1] == "public_symbols: pkg/a.py:alpha, pkg/a.py:beta"


def test_header_and_module_line_are_the_first_two_lines():
    module = _module(
        symbols=[_symbol("alpha")],
        config_reads=[ConfigRead(section="api", key="active", fallback=None)],
    )
    model = _model([module], [_contract("c1", "pkg/a.py")])

    lines = build_collect_context_block(model, "pkg/a.py").split("\n")
    assert lines[0] == _COLLECT_HEADER
    assert lines[1] == "module: pkg/a.py"


def test_parse_error_stays_outside_the_loop_and_survives_a_budget_cut():
    """V2.2: `parse_error:` is not ranked content, so it cannot be cut. A
    module that failed to parse has nothing else to say, and the error IS the
    fact — so a block whose only content is a parse error is still emitted."""
    model = _model([_module(parse_error="SyntaxError: invalid syntax")])

    lines = build_collect_context_block(model, "pkg/a.py", budget=200).split("\n")
    assert lines == [
        _COLLECT_HEADER,
        "module: pkg/a.py",
        "parse_error: SyntaxError: invalid syntax",
    ]


def test_parse_error_and_the_module_line_are_never_cut_by_the_budget():
    model = _model([_module(parse_error="boom")])
    lines = build_collect_context_block(model, "pkg/a.py", budget=1).split("\n")
    assert lines[1] == "module: pkg/a.py"
    assert lines[2] == "parse_error: boom"


def test_parse_error_block_is_exact_when_the_budget_fits_only_the_head():
    """`parse_error` is part of the head, not ranked content: when the budget is
    exactly the header + module + error lines, that is the whole block."""
    model = _model([_module(parse_error="boom")])
    block = build_collect_context_block(model, "pkg/a.py")

    assert len(block) == len(_COLLECT_HEADER) + 1 + len("module: pkg/a.py") + 1 + len("parse_error: boom")
    assert build_collect_context_block(model, "pkg/a.py", budget=len(block)) == block


# ── deduplication ──────────────────────────────────────────────────────────


def test_identical_config_read_lines_are_deduplicated():
    """V2.4: the same (section, key) read through two code paths renders
    byte-identically and must appear once, not twice."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="max_tokens", fallback=2048),
        ConfigRead(section="api", key="active", fallback=True, has_mode_override=True),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    lines = block.split("\n")
    assert len(lines) == len(set(lines)), "no line may repeat"
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1
    assert "config_read [coder] max_tokens (fallback=2048)" in lines
    assert "config_read [api] active (mode-override) (fallback=True)" in lines


def test_distinct_config_reads_with_the_same_key_but_different_fallback_are_not_merged():
    """Deduplication is on the rendered line, never on the key: the fallback is
    part of the fact."""
    reads = [
        ConfigRead(section="coder", key="num_ctx", fallback=8192),
        ConfigRead(section="coder", key="num_ctx", fallback=4096),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    block = build_collect_context_block(model, "pkg/a.py")
    assert "config_read [coder] num_ctx (fallback=8192)" in block
    assert "config_read [coder] num_ctx (fallback=4096)" in block


def test_identical_contract_lines_are_deduplicated():
    """Two contracts that render the same line (same name + description) must
    not repeat, even though they are distinct records (different provenance)."""
    model = _model(
        [_module(symbols=[_symbol("alpha")])],
        [
            _contract("c1", "pkg/a.py", "never raise into a run", "static"),
            _contract("c1", "pkg/a.py", "never raise into a run", "derived"),
        ],
    )

    block = build_collect_context_block(model, "pkg/a.py")
    assert block.count("contract c1: never raise into a run") == 1


def test_contracts_still_dedup_before_rendering_on_the_record():
    """V2.1 keeps the old record-level dedup: a contract citing both the module
    and one of its symbols is one row, not two."""
    contract = _contract("c1", "pkg/a.py", "the contract")
    symbol_contract = _contract("c2", "pkg/a.py:alpha", "the symbol contract")
    model = _model([_module(symbols=[_symbol("alpha")])], [contract, symbol_contract])

    block = build_collect_context_block(model, "pkg/a.py")
    assert "contract c1: the contract" in block
    assert "contract c2: the symbol contract" in block
    assert block.count("contract ") == 2


# ── budget=None renders everything ─────────────────────────────────────────


def test_budget_none_renders_every_row_in_full():
    """V2.5: no budget means no cap at all — the pack is complete."""
    symbols = [_symbol(f"s{i:02d}") for i in range(40)]
    model = _model([_module(symbols=symbols)])

    line = _symbol_line(build_collect_context_block(model, "pkg/a.py"))
    names = line[len("public_symbols: "):].split(", ")
    assert names == [f"pkg/a.py:s{i:02d}" for i in range(40)]
    assert "cut for budget" not in line


def test_default_budget_is_none_so_existing_callers_keep_working():
    """Every pre-V2 caller passes only the model and the target file; the block
    they get must still carry every fact."""
    model = _model(
        [
            _module(
                symbols=[_symbol("alpha"), _symbol("beta")],
                config_reads=[ConfigRead(section="coder", key="num_ctx", fallback=8192)],
            )
        ],
        [_contract("c1", "pkg/a.py", "the contract")],
    )

    block = build_collect_context_block(model, "pkg/a.py")
    assert "contract c1: the contract" in block
    assert "config_read [coder] num_ctx (fallback=8192)" in block
    assert "public_symbols: pkg/a.py:alpha, pkg/a.py:beta" in block


# ── budget cuts ────────────────────────────────────────────────────────────


def test_row_that_does_not_fit_is_skipped_for_the_next_shorter_row():
    """V2.5: cutting under budget pressure IS the `continue` — a row goes whole,
    and the pack is not diluted to make room for it."""
    model = _model(
        [_module(symbols=[_symbol("alpha")])],
        [_contract("c1", "pkg/a.py", "x" * 4000)],
    )

    tight = build_collect_context_block(model, "pkg/a.py", budget=400)
    loose = build_collect_context_block(model, "pkg/a.py", budget=5000)
    assert len(tight) <= 400
    assert "contract c1:" not in tight
    assert "contract c1:" in loose


def test_block_never_exceeds_its_budget():
    model = _model(
        [
            _module(
                symbols=[_symbol(f"s{i:02d}") for i in range(40)],
                config_reads=[ConfigRead(section="coder", key=f"k{i}", fallback=i) for i in range(10)],
            )
        ],
        [_contract(f"c{i}", "pkg/a.py", "y" * 200) for i in range(3)],
    )

    for budget in range(60, 3001, 37):
        block = build_collect_context_block(model, "pkg/a.py", budget=budget)
        assert len(block) <= budget, f"block of {len(block)} chars exceeded budget {budget}"


def test_no_row_is_split_mid_line_by_the_budget():
    """A cut row is a whole row — the budget never truncates a line."""
    model = _model(
        [
            _module(
                symbols=[_symbol(f"s{i:02d}") for i in range(25)],
                config_reads=[ConfigRead(section="coder", key=f"k{i}", fallback=i) for i in range(6)],
            )
        ],
        [_contract(f"c{i}", "pkg/a.py", "z" * 300) for i in range(2)],
    )

    for budget in range(60, 1201, 23):
        for line in build_collect_context_block(model, "pkg/a.py", budget=budget).split("\n"):
            if line.startswith("config_read [coder] "):
                assert line.endswith(")")
            elif line.startswith("contract "):
                assert line.endswith("z" * 300)


def test_a_tiny_budget_yields_no_block_rather_than_a_torn_one():
    """If nothing fits, the early-out returns `""` — never a header with no
    content, which would look like a fact with nothing behind it."""
    model = _model([_module(symbols=[_symbol("alpha")])], [_contract("c1", "pkg/a.py", "z" * 500)])

    block = build_collect_context_block(model, "pkg/a.py", budget=60)
    assert block == ""
    assert "contract c1:" not in block


# ── acceptance: every fact the old block carried is still carried ────


def test_every_fact_the_old_block_carried_is_still_carried():
    """Acceptance: for every module whose old block was within budget,
    the new block carries the same facts (order may differ; content
    may not). Every unique config_read, every contract, and every
    public symbol the record holds appears in the block when the
    rows fit within budget.

    Includes the ticket's specific case: 24 duplicate config_read
    lines in today's tree — the 5 duplicate entries below must not
    appear, and the 15 unique ones must all render.
    """
    symbols = [_symbol(f"s{i:02d}") for i in range(25)]
    reads = [
        ConfigRead(section="coder", key=f"k{i:02d}", fallback=i) for i in range(15)
    ]
    # 5 of these are byte-identical to the first 5 — 24 duplicate
    # config_read lines exist in today's tree per the ticket; the
    # unique facts are 15, not 20.
    reads += [
        ConfigRead(section="coder", key=f"k{i:02d}", fallback=i) for i in range(5)
    ]
    contracts = [
        _contract(f"c{i}", "pkg/a.py", f"contract number {i}") for i in range(8)
    ]
    model = _model(
        [_module(symbols=symbols, config_reads=reads)],
        contracts,
    )

    block = build_collect_context_block(model, "pkg/a.py")
    lines = block.split("\n")

    # All 25 symbols present — no silent [:20] cap, no dropped symbols.
    for i in range(25):
        assert f"pkg/a.py:s{i:02d}" in block

    # All 15 unique config_reads present; the 5 duplicates are dropped.
    assert block.count("config_read [coder]") == 15

    # All 8 contracts present.
    assert block.count("contract c") == 8

    # No line repeats — dedup verified at the block level too.
    assert len(lines) == len(set(lines))

    # public_symbols is the last row.
    assert lines[-1].startswith("public_symbols:")
    assert "contract" not in lines[-1]
    assert "config_read" not in lines[-1]

    # With a budget that fits the whole block, facts are byte-identical
    # to the no-budget case — the budget cut is never triggered.
    assert build_collect_context_block(
        model, "pkg/a.py", budget=len(block) + 1000
    ) == block


# ── early-out ──────────────────────────────────────────────────────────────


def test_header_plus_bare_module_line_only_returns_empty():
    """V2.6: nothing substantive to add means no block at all, so a caller that
    concatenates the result loses nothing."""
    assert build_collect_context_block(_model([_module()]), "pkg/a.py") == ""


def test_unknown_module_returns_empty():
    model = _model([_module("pkg/a.py", symbols=[_symbol("alpha")])])
    assert build_collect_context_block(model, "pkg/does_not_exist.py") == ""


def test_none_and_absent_models_return_empty():
    assert build_collect_context_block(None, "pkg/a.py") == ""
    assert build_collect_context_block(CollectModel(status="absent"), "pkg/a.py") == ""
    assert (
        build_collect_context_block(CollectModel(status="absent"), "pkg/a.py", budget=2000) == ""
    )


def test_stale_model_still_renders_it_is_the_bridge_that_refuses():
    """This function only asks the model; freshness is the bridge's decision."""
    model = CollectModel(
        status="stale", modules=(_module(symbols=[_symbol("alpha")]),)
    )
    assert "public_symbols:" in build_collect_context_block(model, "pkg/a.py")


# ── fail-open ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_budget", ["bogus", "", 0, -5, 1.7, True, False, [], None])
def test_malformed_budget_degrades_to_rendering_everything(bad_budget):
    """A value that is not a positive integer budget means "no budget was
    configured": the full pack, never an exception and never a silent cut."""
    model = _model([_module(symbols=[_symbol(f"s{i}") for i in range(12)])])
    full = build_collect_context_block(model, "pkg/a.py")
    assert build_collect_context_block(model, "pkg/a.py", budget=bad_budget) == full


def test_a_numeric_string_budget_is_honoured():
    """`configparser` hands back strings; a well-formed numeric one still works."""
    model = _model([_module(symbols=[_symbol(f"s{i}") for i in range(40)])])

    full = build_collect_context_block(model, "pkg/a.py")
    cut = build_collect_context_block(model, "pkg/a.py", budget="300")

    assert len(cut) <= 300
    assert cut != full
    assert "cut for budget" in _symbol_line(cut)

def test_public_symbols_announces_the_remainder_when_a_budget_cuts_the_list():
    """V2.3: a partial symbol list must say how many it is hiding. The old
    silent `[:20]` made a 65-symbol module look like a 20-symbol one."""
    model = _model([_module(symbols=[_symbol(f"s{i}") for i in range(30)])])

    block = build_collect_context_block(model, "pkg/a.py", budget=250)
    line = _symbol_line(block)

    listed = [
        part.strip()
        for part in line[len("public_symbols: "):].split(",")
        if part.strip() and "cut for budget" not in part
    ]
    remainder = 30 - len(listed)
    assert line.endswith(f"… (+{remainder} more, cut for budget)")
    assert listed[0] == "pkg/a.py:s0"
    assert "pkg/a.py:s29" not in block


def test_a_row_that_raises_is_dropped_but_the_rest_of_the_pack_survives(monkeypatch):
    """Fail-open house rule: one broken renderer degrades to "no row", never to
    "no block" and never to an exception escaping into a run."""
    import tools.auto.context_assembler as ca

    model = _model(
        [_module(symbols=[_symbol("alpha")])],
        [_contract("c1", "pkg/a.py", "the contract")],
    )

    def _boom(model_, target, remaining):
        raise RuntimeError("artifact is malformed")

    monkeypatch.setattr(
        ca,
        "_PACK_ROWS",
        (
            ("contract", _row_contract),
            ("config_read", _boom),
            ("public_symbols", _row_public_symbols),
        ),
    )
    block = build_collect_context_block(model, "pkg/a.py")
    assert "contract c1: the contract" in block
    assert "public_symbols: pkg/a.py:alpha" in block
    assert "config_read" not in block


def test_malformed_symbol_record_does_not_raise_or_hide_valid_symbols():
    """A hand-edited artifact can carry a symbol whose `qualname` is not a
    string. V2's fail-open rule means the malformed entry is skipped and the
    row still renders the valid symbols it can name."""
    malformed = FunctionRecord(
        qualname=None,
        module="pkg/a.py",
        lineno=2,
        signature="malformed(...)",
    )
    model = _model([_module(symbols=[malformed, _symbol("beta")])])

    block = build_collect_context_block(model, "pkg/a.py")

    assert "public_symbols: pkg/a.py:beta" in block
    assert "malformed" not in block


def test_row_renderers_return_empty_string_not_none():
    model = _model([_module()])
    assert _row_contract(model, "pkg/a.py", None) == ""
    assert _row_config_read(model, "pkg/a.py", None) == ""
    assert _row_public_symbols(model, "pkg/a.py", None) == ""
    assert _row_public_symbols(model, "pkg/unknown.py", None) == ""
