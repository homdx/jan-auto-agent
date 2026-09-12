"""tests/test_collect_block_v2_preserves_pre_v2_facts.py — PLAN-v2 V2, AC #1.

The ticket's first acceptance criterion:

    For every module whose old block was within budget, the new block carries
    the same facts (order may differ; content may not).

No test in the suite asserted it. `tests/test_collect_context_block_rows.py`
pins the *new* shape (row order, dedupe, budget cuts) and
`tests/test_collect_inject_auto.py` pins one module's new block byte for byte,
but neither compares against what the block said **before** V2, so a rewrite
that silently drops a contract or a `config_read` line would pass the whole
suite.

`_legacy_block` below is the pre-V2 single-pass renderer, copied verbatim from
`72bbc86^` (the commit before `V2: the block becomes an ordered row list`).
It is intentionally kept here rather than deleted: it is the reference the
behaviour-preservation claim is measured against, and V2's own promise is that
a pre-V2 caller who stops passing `budget` sees the same facts, in possibly a
different order.

Facts are compared as sets of rendered lines. That is the right unit because
V2 changed exactly two things about content: `public_symbols` lost its silent
`[:20]` cap, and byte-identical `config_read` lines are deduplicated. Both are
changes in *how many* of a fact appear, never in *what a fact says* — so the
rendered line is still the thing to compare, only de-duplicated.

Only structural dataclasses are built here, except the live section at the
bottom, which reads this repo's own tree through the producer's scan pass
rather than from `.collect/`, so it cannot skip for a missing artifact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.auto.context_assembler import (
    _COLLECT_HEADER,
    _PACK_ROWS,
    build_collect_context_block,
)
from tools.collect import graph as graph_mod
from tools.collect import scanner as scanner_mod
from tools.collect import test_map as test_map_mod
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import (
    ConfigRead,
    ContractRecord,
    FunctionRecord,
    ModuleRecord,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = "pkg/a.py"

_SYMBOLS_PREFIX = "public_symbols: "
_CONTRACT_PREFIX = "contract "
_CONFIG_PREFIX = "config_read "


# ── the pre-V2 reference renderer ───────────────────────────────────────────


def _legacy_block(model, target_file: str, task_mode: str = "code") -> str:
    """The block as `build_collect_context_block` rendered it before V2,
    copied from `72bbc86^`. Do not modernise it — it is the reference."""
    if model is None or not getattr(model, "available", False):
        return ""

    record = model.module(target_file)
    if record is None:
        return ""

    lines = [_COLLECT_HEADER, f"module: {record.path}"]

    if record.parse_error:
        lines.append(f"parse_error: {record.parse_error}")

    if record.public_symbols:
        symbols = ", ".join(s.qualname for s in record.public_symbols[:20])
        lines.append(f"public_symbols: {symbols}")

    contracts = list(model.contracts_for(target_file))
    # also surface contracts cited against a symbol defined in this file
    for sym in record.public_symbols:
        contracts.extend(c for c in model.contracts_for(sym.qualname) if c not in contracts)
    if contracts:
        for c in sorted(contracts, key=lambda c: c.name):
            lines.append(f"contract {c.name}: {c.description}")

    config_reads = list(record.config_reads)
    if config_reads:
        for cr in config_reads:
            mode_note = " (mode-override)" if cr.has_mode_override else ""
            lines.append(
                f"config_read [{cr.section}] {cr.key}{mode_note} (fallback={cr.fallback!r})"
            )

    if len(lines) == 2 and not record.public_symbols:
        # Only the header + bare module line — nothing substantive to add.
        return ""

    return "\n".join(lines)


def _legacy_facts(legacy: str) -> dict[str, set[str]]:
    """The legacy block split by row. Duplicate lines collapse to a set: V2's
    dedupe means the new block cannot be compared line-for-line against a
    block that repeats itself."""
    out: dict[str, set[str]] = {
        "parse_error": set(),
        "contract": set(),
        "config_read": set(),
        "symbol": set(),
    }
    for line in legacy.split("\n"):
        if line.startswith("parse_error: "):
            out["parse_error"].add(line)
        elif line.startswith(_CONTRACT_PREFIX):
            out["contract"].add(line)
        elif line.startswith(_CONFIG_PREFIX):
            out["config_read"].add(line)
        elif line.startswith(_SYMBOLS_PREFIX):
            body = line[len(_SYMBOLS_PREFIX):]
            out["symbol"].update(s.strip() for s in body.split(",") if s.strip())
    return out


def _new_lines(block: str) -> list[str]:
    return block.split("\n") if block else []


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(name: str, lineno: int = 1) -> FunctionRecord:
    return FunctionRecord(
        qualname=f"{TARGET}:{name}", module=TARGET, lineno=lineno, signature=f"{name}(...)"
    )


def _module(
    symbols=(),
    config_reads=(),
    parse_error: str | None = None,
) -> ModuleRecord:
    return ModuleRecord(
        path=TARGET,
        public_symbols=tuple(symbols),
        config_reads=tuple(config_reads),
        parse_error=parse_error,
    )


def _contract(name: str, edge: str, description: str = "contract body") -> ContractRecord:
    return ContractRecord(name=name, description=description, known_edge=edge)


def _read(section: str, key: str, fallback, mode_override: bool = False) -> ConfigRead:
    return ConfigRead(section=section, key=key, fallback=fallback, has_mode_override=mode_override)


def _model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts)
    )


# ── the header is untouched ────────────────────────────────────────────────


def test_the_header_and_module_line_are_byte_identical_to_the_legacy_block():
    """V2 moved everything into a row list; the two lines that were never
    ranked content must not have been reworded either."""
    module = _module(symbols=[_symbol("alpha")])
    model = _model([module], [_contract("c1", TARGET)])

    legacy = _legacy_block(model, TARGET).split("\n")
    new = _new_lines(build_collect_context_block(model, TARGET))

    assert new[0] == legacy[0] == _COLLECT_HEADER
    assert new[1] == legacy[1] == f"module: {TARGET}"


def test_the_parse_error_line_survives_the_row_rewrite():
    """`parse_error:` stays outside the loop, so it is neither reworded nor
    cut. It is the only fact a module that failed to parse has."""
    model = _model([_module(parse_error="SyntaxError: unexpected EOF")])

    legacy = _legacy_block(model, TARGET)
    new = build_collect_context_block(model, TARGET)

    assert "parse_error: SyntaxError: unexpected EOF" in legacy.split("\n")
    assert "parse_error: SyntaxError: unexpected EOF" in _new_lines(new)
    # and it is still there under budget pressure
    for budget in (1, 40, 200):
        assert "parse_error: SyntaxError: unexpected EOF" in _new_lines(
            build_collect_context_block(model, TARGET, budget=budget)
        )


# ── contracts ──────────────────────────────────────────────────────────────


def test_every_legacy_contract_line_survives_the_row_rewrite():
    module = _module(symbols=[_symbol("alpha"), _symbol("beta")])
    contracts = [
        _contract("c1", TARGET, "never raise into a run"),
        _contract("c2", f"{TARGET}:alpha", "the symbol contract"),
        _contract("c3", "other.py", "not this module's"),
        _contract("c4", TARGET, "x" * 400),
    ]
    model = _model([module], contracts)

    facts = _legacy_facts(_legacy_block(model, TARGET))
    new = build_collect_context_block(model, TARGET)

    assert facts["contract"], "the fixture must carry contracts for this to mean anything"
    assert "contract c3: not this module's" not in facts["contract"]
    for line in facts["contract"]:
        assert line in _new_lines(new), f"the row rewrite dropped a contract the legacy block had: {line!r}"


def test_a_legacy_contract_the_row_rewrite_deduplicated_still_appears_once():
    """Two records that render the same line: V2 collapses the duplicate, but
    the line must not disappear with it."""
    contracts = [
        _contract("c1", TARGET, "never raise into a run"),
        _contract("c1", f"{TARGET}:alpha", "never raise into a run"),
    ]
    model = _model([_module(symbols=[_symbol("alpha")])], contracts)

    legacy = _legacy_block(model, TARGET)
    new = build_collect_context_block(model, TARGET)
    line = "contract c1: never raise into a run"

    assert legacy.count(line) == 2, "the legacy block repeated the line; that is the premise"
    assert new.count(line) == 1


# ── config reads ───────────────────────────────────────────────────────────


def test_every_legacy_config_read_line_survives_the_row_rewrite():
    reads = [
        _read("coder", "num_ctx", 8192),
        _read("coder", "num_ctx", 8192),  # V2 dedupes this one
        _read("coder", "num_ctx", 4096),  # same key, different fallback: a different fact
        _read("api", "active", None, mode_override=True),
        _read("collect", "staleness", "warn"),
    ]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    facts = _legacy_facts(_legacy_block(model, TARGET))
    new = build_collect_context_block(model, TARGET)

    assert len(facts["config_read"]) == 4
    for line in facts["config_read"]:
        assert line in _new_lines(new), (
            f"the row rewrite dropped a config_read the legacy block had: {line!r}"
        )
    # and the duplicate is the only line that may have collapsed
    assert new.count("config_read [coder] num_ctx (fallback=8192)") == 1


def test_two_config_reads_that_render_the_same_line_are_one_fact_not_zero():
    reads = [_read("coder", "num_ctx", 8192) for _ in range(5)]
    model = _model([_module(symbols=[_symbol("alpha")], config_reads=reads)])

    facts = _legacy_facts(_legacy_block(model, TARGET))
    new = build_collect_context_block(model, TARGET)

    assert len(facts["config_read"]) == 1
    assert new.count(next(iter(facts["config_read"]))) == 1


# ── public symbols ─────────────────────────────────────────────────────────


def test_every_symbol_the_legacy_block_listed_is_in_the_new_block():
    symbols = [_symbol(f"s{i:02d}") for i in range(25)]  # 5 over the legacy cap
    model = _model([_module(symbols=symbols)])

    facts = _legacy_facts(_legacy_block(model, TARGET))
    new = build_collect_context_block(model, TARGET)

    assert len(facts["symbol"]) == 20, "the legacy block caps at 20; that is the point"
    for qualname in facts["symbol"]:
        assert qualname in new, f"the row rewrite dropped a symbol the legacy block had: {qualname!r}"


def test_the_new_block_lists_the_symbols_the_legacy_cap_silently_dropped():
    """AC #1 says content may not change; the cap was content being removed.
    The dropped tail is recoverable and must now be present."""
    symbols = [_symbol(f"s{i:02d}") for i in range(25)]
    model = _model([_module(symbols=symbols)])

    new = build_collect_context_block(model, TARGET)
    legacy_symbols = _legacy_facts(_legacy_block(model, TARGET))["symbol"]
    dropped = {f"{TARGET}:s{i:02d}" for i in range(20, 25)}

    assert not dropped & legacy_symbols
    for qualname in dropped:
        assert qualname in new, f"the silently-dropped symbol is still missing: {qualname!r}"


# ── the whole block, at no budget ──────────────────────────────────────────


def test_no_budget_carries_every_legacy_fact():
    """V2.5's promise, stated as AC #1: a pre-V2 caller that passes no budget
    gets the same facts."""
    symbols = [_symbol(f"s{i:02d}") for i in range(30)]
    reads = [
        _read("coder", f"k{i}", i) for i in range(6)
    ] + [_read("coder", "k0", 0)]
    contracts = [
        _contract("c1", TARGET, "never raise"),
        _contract("c2", f"{TARGET}:s00", "the symbol contract"),
        _contract("c3", "other.py", "not this module's"),
    ]
    model = _model([_module(symbols=symbols, config_reads=reads, parse_error="SyntaxError: boom")], contracts)

    facts = _legacy_facts(_legacy_block(model, TARGET))
    new = build_collect_context_block(model, TARGET)

    for line in facts["parse_error"]:
        assert line in _new_lines(new)
    for line in facts["contract"]:
        assert line in _new_lines(new)
    for line in facts["config_read"]:
        assert line in _new_lines(new)
    for qualname in facts["symbol"]:
        assert qualname in new


def test_no_budget_is_a_superset_of_the_legacy_block_line_for_line():
    """No legacy line may be missing from the new block. `public_symbols` is
    compared symbol by symbol rather than line by line, because V2 removed its
    `[:20]` cap: the legacy line is a strict prefix of the new one, not equal."""
    symbols = [_symbol(f"s{i:02d}") for i in range(24)]
    reads = [_read("coder", f"k{i}", i) for i in range(4)] + [_read("coder", "k1", 1)]
    model = _model(
        [_module(symbols=symbols, config_reads=reads, parse_error="boom")],
        [_contract("c1", TARGET, "d1"), _contract("c2", f"{TARGET}:s01", "d2")],
    )

    legacy = _legacy_block(model, TARGET)
    new = build_collect_context_block(model, TARGET)

    new_lines = _new_lines(new)
    new_symbols = {
        s.strip()
        for line in new_lines
        if line.startswith(_SYMBOLS_PREFIX)
        for s in line[len(_SYMBOLS_PREFIX):].split(",")
        if s.strip()
    }
    for line in legacy.split("\n"):
        if not line:
            continue
        if line.startswith(_SYMBOLS_PREFIX):
            for s in line[len(_SYMBOLS_PREFIX):].split(","):
                s = s.strip()
                if s:
                    assert s in new_symbols, f"legacy symbol absent from the new block: {s!r}"
        else:
            assert line in new_lines, f"legacy line absent from the new block: {line!r}"


# ── the early-out is preserved ─────────────────────────────────────────────


def test_the_early_out_agrees_with_the_legacy_block():
    """A module with nothing to say must give no block in both versions, and a
    module that has something to say must give one in both."""
    cases = {
        "nothing at all": _model([_module()]),
        "parse error only": _model([_module(parse_error="boom")]),
        "symbols only": _model([_module(symbols=[_symbol("alpha")])]),
        "config read only": _model([_module(config_reads=[_read("coder", "k", 1)])]),
        "contract only": _model([_module()], [_contract("c1", TARGET, "d")]),
    }
    for label, model in cases.items():
        legacy_empty = _legacy_block(model, TARGET) == ""
        new_empty = build_collect_context_block(model, TARGET) == ""
        assert legacy_empty == new_empty, (
            f"{label}: legacy={legacy_empty} new={new_empty} — the early-out diverged"
        )


def test_an_absent_or_unknown_model_gives_the_same_empty_block_as_before():
    """COLLECT-23's original AC: when there is no collect data the caller loses
    nothing. V2 may not have changed that."""
    assert _legacy_block(None, TARGET) == ""
    assert build_collect_context_block(None, TARGET) == ""
    absent = CollectModel(status="absent")
    assert _legacy_block(absent, TARGET) == ""
    assert build_collect_context_block(absent, TARGET) == ""
    present = _model([_module(symbols=[_symbol("alpha")])])
    assert _legacy_block(present, "pkg/unknown.py") == ""
    assert build_collect_context_block(present, "pkg/unknown.py") == ""


# ── every legacy fact is recoverable under a budget ────────────────────────


def test_every_legacy_fact_is_recovered_at_some_budget_and_never_lost_again():
    """AC #1's budgeted form: no legacy fact is permanently unreachable. Each
    one appears at some budget, and once it appears it stays — a budget cut may
    remove a fact, it may not make it vanish and then reappear later."""
    symbols = [_symbol(f"s{i:02d}") for i in range(40)]
    reads = [_read("coder", f"k{i}", i) for i in range(10)]
    contracts = [_contract(f"c{i}", TARGET, "y" * 60) for i in range(4)]
    model = _model([_module(symbols=symbols, config_reads=reads)], contracts)

    facts = _legacy_facts(_legacy_block(model, TARGET))
    assert facts["contract"] and facts["config_read"] and facts["symbol"]

    first_seen: dict[str, int] = {}
    for budget in range(1, 4001, 7):
        block = build_collect_context_block(model, TARGET, budget=budget)
        lines = _new_lines(block)
        for line in facts["parse_error"] | facts["contract"] | facts["config_read"]:
            if line in lines:
                first_seen.setdefault(line, budget)
        for qualname in facts["symbol"]:
            if qualname in block:
                first_seen.setdefault(qualname, budget)

    assert first_seen, "no legacy fact was ever rendered"
    # and monotone: once a fact is in, it stays in at every larger budget
    for budget in range(1, 4001, 7):
        block = build_collect_context_block(model, TARGET, budget=budget)
        lines = set(_new_lines(block))
        for fact, seen_at in first_seen.items():
            if seen_at <= budget:
                if fact in facts["symbol"]:
                    assert fact in block
                else:
                    assert fact in lines


# ── the row order changed, the content did not ─────────────────────────────


def test_the_row_rewrite_reordered_but_did_not_rewrite_a_row():
    """AC #1 allows the order to differ. Pin that it actually did differ, so
    this test cannot pass by accident from a no-op, and that the lines it
    moved are byte-identical to the legacy lines."""
    module = _module(
        symbols=[_symbol("alpha"), _symbol("beta")],
        config_reads=[_read("coder", "num_ctx", 8192)],
    )
    model = _model([module], [_contract("c1", TARGET, "the contract")])

    legacy_lines = _legacy_block(model, TARGET).split("\n")
    new_lines = _new_lines(build_collect_context_block(model, TARGET))

    assert legacy_lines != new_lines, "the order must have changed; this test pins the delta"
    for line in legacy_lines:
        if line.startswith((_SYMBOLS_PREFIX, _CONTRACT_PREFIX, _CONFIG_PREFIX, "parse_error:")):
            assert line in new_lines, f"the row moved by the rewrite was also reworded: {line!r}"


def test_the_legacy_order_is_not_reused():
    """The legacy order was header, module, parse_error, public_symbols,
    contract, config_read. V2 puts public_symbols last, so its position in the
    new block must be later than the legacy one."""
    module = _module(
        symbols=[_symbol("alpha")],
        config_reads=[_read("coder", "k", 1)],
        parse_error="boom",
    )
    model = _model([module], [_contract("c1", TARGET, "d")])

    legacy = _legacy_block(model, TARGET).split("\n")
    new = _new_lines(build_collect_context_block(model, TARGET))

    syms = [l for l in new if l.startswith(_SYMBOLS_PREFIX)]
    assert syms, "the fixture must render a symbol row"
    assert new.index(syms[0]) == len(new) - 1, "public_symbols must still be last"
    assert legacy.index(next(l for l in legacy if l.startswith(_SYMBOLS_PREFIX))) < len(new) - 1


# ── end to end against this repo's own tree ────────────────────────────────


@pytest.fixture(scope="module")
def live_model() -> CollectModel:
    """This repo's symbols and config reads, built from the source tree the way
    the producer builds them — never from `.collect/`, which is gitignored and
    would make AC #1 skip on a fresh checkout. Contracts come from the
    artifact, so they are absent here; the structural rows are what the scan
    pass owns and what this test checks."""
    modules = scanner_mod.scan_repo(REPO_ROOT)
    edges = graph_mod.import_edges(modules)
    reverse = graph_mod.imported_by(edges)
    tmap = test_map_mod.build_test_map(REPO_ROOT, modules)
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
        test_map=tmap,
    )


def test_live_modules_carry_every_legacy_fact(live_model):
    """AC #1 stated literally, over the whole tree: for every module, the new
    block carries every fact the pre-V2 block carried."""
    checked_symbols = 0
    checked_reads = 0
    for module in live_model.modules:
        legacy = _legacy_block(live_model, module.path)
        if not legacy:
            continue
        new = build_collect_context_block(live_model, module.path)
        facts = _legacy_facts(legacy)

        new_lines = _new_lines(new)
        for line in facts["contract"]:
            assert line in new_lines, (module.path, line)
        for line in facts["config_read"]:
            assert line in new_lines, (module.path, line)
            checked_reads += 1
        for qualname in facts["symbol"]:
            assert qualname in new, (module.path, qualname)
            checked_symbols += 1

    assert checked_symbols, "the tree must carry symbols for this test to mean anything"
    assert checked_reads, "the tree must carry config reads for this test to mean anything"


def test_live_modules_recover_the_legacy_cap_tail(live_model):
    """The modules the legacy `[:20]` truncated: their new block must carry the
    symbols the old one dropped."""
    recovered = 0
    for module in live_model.modules:
        if len(module.public_symbols) <= 20:
            continue
        legacy_symbols = _legacy_facts(_legacy_block(live_model, module.path))["symbol"]
        assert len(legacy_symbols) == 20
        new = build_collect_context_block(live_model, module.path)
        dropped = {s.qualname for s in module.public_symbols[20:]}
        assert not dropped & legacy_symbols
        still_missing = [q for q in dropped if q not in new]
        assert not still_missing, (module.path, still_missing)
        recovered += len(dropped)
    assert recovered > 0, (
        "this repo should have modules above the legacy cap, so the tail "
        "this test recovers must be non-empty"
    )
