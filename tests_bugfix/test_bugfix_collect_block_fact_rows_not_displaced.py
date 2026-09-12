"""tests_bugfix/test_bugfix_collect_block_fact_rows_not_displaced.py — L6 follow-up.

Two defects in the L6 budget loop survived the ticket that introduced it. Both
were measured against this repo's own tree at `ced5940`:

1. `public_symbols` still displaced the two fact rows L6 did not make
   shrinkable. `config_read` and `contract` ignored their allowance entirely —
   the docstring even said so — so they rendered whole or not at all. Measured
   on `tools/auto/coder.py`: 395 of the 2 480 budgets swept rendered
   `public_symbols` while the 16-line `config_read` row had been skipped for
   budget. 272 on `main.py`. The displacement L6 removes from `callers`,
   `calls_into` and `tests` was still happening one row over.

2. A larger budget could render fewer rows and fewer names than a smaller one.
   The `+N` rows were allowed a share of the slack, and the rows below them
   were allowed "whatever the rows above happened to claim". A row above adds a
   whole name when its allowance crosses a name boundary, so that step hands the
   row below fewer characters than the budget it just gained. Measured on
   `tools/auto/context_assembler.py`: at budget 464 `public_symbols` shows one
   symbol where 463 shows two; at 570 two where 569 shows three. On `main.py`
   the same drop hit `config_read` at 440, 454 and 494, and at 244 the
   `config_read` row disappeared outright where 242 still rendered it.

The fix cuts the pack from the least valuable end down to the budget instead of
asking each row for the largest form that fits what is left: `public_symbols`
gives ground first, then the one-fact-per-line rows by whole lines, then the `+N`
rows by names down to their count. A row's count is never spent to buy room for
a row below it, so the rendered rows stay a prefix of `_PACK_ROWS` and a larger
budget only ever returns content to a row.

`CollectBridge._shrink` is untouched — everything here runs before it, in
`build_collect_context_block`. Only structural dataclasses are built, except the
live section at the bottom, which reads this repo's own tree the way the
producer builds it, so it cannot skip for a missing artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.auto.context_assembler import (
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _row_config_read,
    _row_contract,
    _row_public_symbols,
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
    LLMSummary,
    ModuleRecord,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGET = "pkg/hub.py"
_HEAD = "COLLECT MODEL (static facts, do not contradict):"

_ROWS = {name: render for name, render in _PACK_ROWS}
_FACT_PREFIXES = ("callers: ", "calls_into: ", "tests: ")
_SYMBOLS_PREFIX = "public_symbols: "
_CONFIG_PREFIX = "config_read ["
_CONTRACT_PREFIX = "contract "


def _rows_of(block: str) -> "list[str]":
    """The names of the rows that rendered, in pack order."""
    rendered = []
    for name in _ROWS:
        if name == "public_symbols":
            present = any(line.startswith(_SYMBOLS_PREFIX) for line in block.split("\n"))
        elif name == "config_read":
            present = any(line.startswith(_CONFIG_PREFIX) for line in block.split("\n"))
        elif name == "contract":
            present = any(line.startswith(_CONTRACT_PREFIX) for line in block.split("\n"))
        else:
            present = any(line.startswith(f"{name}: ") for line in block.split("\n"))
        if present:
            rendered.append(name)
    return rendered


def _lines_of(block: str, prefix: str) -> "list[str]":
    return [line for line in block.split("\n") if line.startswith(prefix)]


def _n_names(line: str) -> int:
    """Paths or symbols a row line shows; a `, +N` announcement is not one."""
    return len(re.findall(r"[\w./-]+\.py(?::[\w.]+)?|[\w]+\.[\w]+(?::[\w.]+)", line))


def _head_len() -> int:
    return len(_HEAD) + 1 + len(f"module: {TARGET}")


def _count_ok(line: str, floor: str) -> bool:
    """Whether a `+N` row's line still states the count it leads with.

    `callers` and `tests` put the count before the names, so their line starts
    with its floor. `calls_into` states its count only as "N targets" at its
    floor and leads with the names when it has room, so there the names shown
    plus the announced remainder must still account for every target.
    """
    if _n_names(line) == 0:
        return line == floor
    if line.startswith(floor):
        return True
    stated = re.match(r"^\w+: (\d+) ", floor)
    if not stated:
        return False
    announced = int(line.rsplit(", +", 1)[1]) if ", +" in line else 0
    return _n_names(line) + announced == int(stated.group(1))


# ── fixtures ─────────────────────────────────────────────────────────────────


def _hub(
    n_reads: int = 30,
    n_contracts: int = 4,
    n_symbols: int = 40,
    n_callers: int = 24,
    n_calls: int = 20,
    n_tests: int = 24,
) -> CollectModel:
    """A hub with a long `config_read` and `contract` row, so both have lines to
    drop one at a time, plus a `public_symbols` row long enough to tempt the
    budget away from them."""
    reads = tuple(
        ConfigRead(section="coder", key=f"key_{i:02d}", fallback=8192) for i in range(n_reads)
    )
    contracts = tuple(
        ContractRecord(
            name=f"contract_{i}",
            description=f"the {i}th invariant the hub's callers depend on holds",
            known_edge=TARGET,
        )
        for i in range(n_contracts)
    )
    symbols = tuple(
        FunctionRecord(
            qualname=f"{TARGET}:public_function_number_{i:02d}",
            module=TARGET,
            lineno=i + 1,
            signature=f"public_function_number_{i:02d}(...)",
        )
        for i in range(n_symbols)
    )
    callers = tuple(f"pkg/caller_module_{i:02d}.py" for i in range(n_callers))
    calls = tuple(f"pkg/dependency_module_{i:02d}.py" for i in range(n_calls))
    tests = tuple(f"tests/test_hub_case_{i:02d}.py" for i in range(n_tests))
    modules = [
        ModuleRecord(
            path=TARGET,
            public_symbols=symbols,
            config_reads=reads,
            summary=LLMSummary(purpose="The hub under budget."),
        )
    ]
    for path in (*callers, *calls, *tests):
        modules.append(ModuleRecord(path=path))
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        contracts=contracts,
        import_edges={TARGET: calls},
        imported_by={TARGET: callers},
        test_map={TARGET: tests},
    )


# ── the one-fact-per-line rows shrink by whole lines ─────────────────────────


def test_config_read_drops_whole_lines_from_the_end_instead_of_vanishing():
    """`config_read` used to render all of its call sites or none: it ignored
    its allowance, so a tight budget pushed `public_symbols` into the space a
    `config_read` line could have used. Each line is now a complete call site
    and they go from the end."""
    model = _hub(n_reads=30, n_symbols=0)
    full = _row_config_read(model, TARGET, None)
    lines = full.split("\n")
    assert len(lines) == 30

    # Monotone in the allowance, and never a fragment: a shorter row is always
    # a leading slice of the full one.
    widths, counts = [], []
    for remaining in range(0, len(full) + 1):
        shrunk = _row_config_read(model, TARGET, remaining)
        widths.append(len(shrunk))
        counts.append(len(_lines_of(shrunk, _CONFIG_PREFIX)))
        kept = _lines_of(shrunk, _CONFIG_PREFIX)
        assert kept == lines[: len(kept)], f"remaining={remaining}: not a leading slice"
    assert widths == sorted(widths), "the shrink is not monotone in the allowance"
    assert counts == sorted(counts), "a larger allowance yields fewer lines"

    assert _row_config_read(model, TARGET, len(lines[0]) - 1) == "", (
        "not one complete line fits: the row is absent rather than a fragment"
    )
    assert _row_config_read(model, TARGET, len(lines[0])) == lines[0]


def test_a_duplicated_config_read_costs_the_cut_nothing():
    """The same `(section, key)` read through two code paths renders one line,
    and the cut measures the row as one line: before, the row was measured with
    the duplicate in, so the budget that would have afforded the row's last
    distinct call site was spent on a line the block's dedupe then removed."""
    distinct = _hub(n_reads=6, n_symbols=0, n_contracts=0)
    reads = tuple(ConfigRead(section="coder", key=f"key_{i:02d}", fallback=8192) for i in range(6))
    doubled = CollectModel(
        status=STATUS_FRESH,
        modules=(
            ModuleRecord(path=TARGET, config_reads=reads + reads),
            *(m for m in distinct.modules if m.path != TARGET),
        ),
        import_edges=distinct.import_edges,
        imported_by=distinct.imported_by,
        test_map=distinct.test_map,
    )
    assert _row_config_read(doubled, TARGET, None) == _row_config_read(distinct, TARGET, None)

    # Exactly the budget the six distinct lines need, after the rows above them
    # are at their floors: the doubled module must render the same six.
    for budget in range(120, 700, 7):
        assert build_collect_context_block(doubled, TARGET, budget=budget) == (
            build_collect_context_block(distinct, TARGET, budget=budget)
        ), f"budget={budget}: a duplicate call site changed the block"


def test_contract_drops_whole_lines_from_the_end_instead_of_vanishing():
    """`contract` is one invariant per line, so a cut drops a whole contract,
    never half of one."""
    model = _hub(n_reads=0, n_contracts=4, n_symbols=0)
    full = _row_contract(model, TARGET, None)
    lines = full.split("\n")
    assert len(lines) == 4
    assert full == _row_contract(model, TARGET, len(full))

    for remaining in range(0, len(full) + 1):
        kept = _lines_of(_row_contract(model, TARGET, remaining), _CONTRACT_PREFIX)
        assert kept == lines[: len(kept)], f"remaining={remaining}: not a leading slice"
    assert _row_contract(model, TARGET, len(lines[0]) - 1) == ""
    assert _row_contract(model, TARGET, len(lines[0])) == lines[0]


def test_every_shrunk_line_is_still_a_complete_fact():
    """A line is the unit of the cut, so every line that survives reads exactly
    as it did before any budget was applied — a section, a key, a fallback."""
    model = _hub(n_reads=30, n_contracts=4, n_symbols=0)
    full_config = _lines_of(_row_config_read(model, TARGET, None), _CONFIG_PREFIX)
    full_contract = _lines_of(_row_contract(model, TARGET, None), _CONTRACT_PREFIX)
    for budget in range(60, 3200, 7):
        block = build_collect_context_block(model, TARGET, budget=budget)
        for line in _lines_of(block, _CONFIG_PREFIX):
            assert line in full_config, f"budget={budget}: {line!r} is not a full call site"
        for line in _lines_of(block, _CONTRACT_PREFIX):
            assert line in full_contract, f"budget={budget}: {line!r} is not a full contract"


# ── no fact row is displaced by public_symbols ───────────────────────────────


def test_public_symbols_never_displaces_a_line_based_fact_row():
    """Swept across the whole budget range: `public_symbols` is never rendered
    while a fact row above it that has data was skipped for budget."""
    model = _hub()
    assert _lines_of(build_collect_context_block(model, TARGET), _SYMBOLS_PREFIX), (
        "the fixture needs symbols for this test to mean anything"
    )
    for budget in range(40, 3400, 2):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget, f"budget={budget}: block of {len(block)} chars"
        if not _lines_of(block, _SYMBOLS_PREFIX):
            continue
        for name in ("config_read", "contract"):
            assert _ROWS[name](model, TARGET, None), f"{name} should have data in the fixture"
            assert _rows_of(block), f"budget={budget}"
        for name, prefix in (("config_read", _CONFIG_PREFIX), ("contract", _CONTRACT_PREFIX)):
            assert _lines_of(block, prefix), (
                f"budget={budget}: public_symbols displaced {name.strip()} — a static fact "
                "row lost to the row the target file's own source already shows"
            )


def test_public_symbols_gives_ground_before_a_config_read_line_does():
    """The order of the cut is the order of the pack reversed: `public_symbols`
    empties before the first `config_read` line is dropped."""
    model = _hub(n_reads=20, n_contracts=0)
    full_config = _lines_of(_row_config_read(model, TARGET, None), _CONFIG_PREFIX)
    for budget in range(40, 3000, 2):
        block = build_collect_context_block(model, TARGET, budget=budget)
        n_config = len(_lines_of(block, _CONFIG_PREFIX))
        if n_config == len(full_config):
            continue  # nothing has been cut from config_read
        assert not _lines_of(block, _SYMBOLS_PREFIX), (
            f"budget={budget}: config_read lost a line while public_symbols still had {block!r}"
        )


# ── the rendered rows are a prefix of the pack order ─────────────────────────


def test_the_rendered_rows_are_a_prefix_of_the_pack_order():
    """A row is never rendered at the cost of a row above it: if a row with data
    is absent, every row below it is absent too, `public_symbols` excepted."""
    model = _hub()
    order = list(_ROWS)
    for target_budget in (3000, 1500, 900, 600, 400, 250, 180, 140, 110):
        block = build_collect_context_block(model, TARGET, budget=target_budget)
        rendered = _rows_of(block)
        absent_with_data = [
            name
            for name in order
            if _ROWS[name](model, TARGET, None) and name not in rendered
        ]
        for name in absent_with_data:
            for below in order[order.index(name) + 1 :]:
                if below == "public_symbols":
                    continue
                assert below not in rendered, (
                    f"budget={target_budget}: {below} rendered while {name} was skipped"
                )


def test_a_fact_count_is_never_spent_to_buy_room_below_it():
    """The counts of the `+N` rows are the facts the pack exists to carry, so
    they survive any cut: swept across the budget range, a count that renders
    is still a count, never a names-only line that has quietly dropped it."""
    model = _hub(n_symbols=0)
    counts = [name for name in _ROWS if name in _SHRINKABLE_ROWS]
    floors = {name: _ROWS[name](model, TARGET, 0) for name in counts}
    for budget in range(40, 2000, 2):
        block = build_collect_context_block(model, TARGET, budget=budget)
        for name in counts:
            lines = _lines_of(block, f"{name}: ")
            if not lines:
                continue
            assert _count_ok(lines[0], floors[name]), (
                f"budget={budget}: {lines[0]!r} is not the count {floors[name]!r}"
            )


# ── a larger budget never renders less ───────────────────────────────────────


def test_a_larger_budget_never_renders_fewer_rows_or_names():
    """The defect that a share-based allowance left open: a row above adds a
    whole name when its allowance crosses a name boundary, and that step handed
    the row below fewer characters than the budget it just gained."""
    model = _hub()
    order = list(_ROWS)
    previous_rows: "list[str] | None" = None
    previous_counts: "dict[str, int]" = {}
    for budget in range(40, 3400, 1):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget, f"budget={budget}: block of {len(block)} chars"
        rendered = _rows_of(block)
        if previous_rows is not None:
            assert len(rendered) >= len(previous_rows), (
                f"budget={budget}: {len(rendered)} rows after {len(previous_rows)} at a smaller "
                f"budget — {previous_rows} became {rendered}"
            )
        counts: "dict[str, int]" = {}
        for line in block.split("\n"):
            for name in order:
                if name == "public_symbols" and line.startswith(_SYMBOLS_PREFIX):
                    counts[name] = len(re.findall(r"public_function_number_\d+", line))
                elif name == "config_read" and line.startswith(_CONFIG_PREFIX):
                    counts[name] = len(_lines_of(block, _CONFIG_PREFIX))
                elif name == "contract" and line.startswith(_CONTRACT_PREFIX):
                    counts[name] = len(_lines_of(block, _CONTRACT_PREFIX))
                elif line.startswith(f"{name}: "):
                    counts[name] = _n_names(line)
        for name, count in previous_counts.items():
            if name in counts:
                assert counts[name] >= count, (
                    f"budget={budget}: {name} shows {counts[name]} after {count} at a smaller "
                    f"budget"
                )
        previous_rows, previous_counts = rendered, counts


# ── the live tree ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_model() -> CollectModel:
    """This repo's graph and test map, built from the source tree the way the
    producer builds them — never from `.collect/`, which is gitignored and
    would make the acceptance tests skip on a fresh checkout."""
    modules = scanner_mod.scan_repo(REPO_ROOT)
    edges = graph_mod.import_edges(modules)
    reverse = graph_mod.imported_by(edges)
    tmap = test_map_mod.build_test_map(REPO_ROOT, modules)
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
        entry_points=tuple(graph_mod.entry_points(edges, reverse=reverse)),
        test_map=tmap,
        zero_coverage_list=tuple(test_map_mod.zero_coverage(tmap)),
    )


def test_live_config_read_is_never_displaced_by_public_symbols(live_model):
    """The measurement that found the defect: on `tools/auto/coder.py`, 395 of
    the budgets swept rendered `public_symbols` while the `config_read` row had
    been skipped for budget. On `main.py`, 272."""
    for target in ("tools/auto/coder.py", "main.py"):
        assert _row_config_read(live_model, target, None), (
            f"{target} should carry config_read lines"
        )
        assert _row_public_symbols(live_model, target, None), (
            f"{target} should carry symbols"
        )
        for budget in (1717, 900, 600, 400, 250):
            block = build_collect_context_block(live_model, target, budget=budget)
            assert len(block) <= budget, (target, budget, len(block))
            if _lines_of(block, _SYMBOLS_PREFIX):
                assert _lines_of(block, _CONFIG_PREFIX), (
                    f"{target} budget={budget}: public_symbols displaced config_read"
                )


def test_live_blocks_are_monotone_in_the_budget(live_model):
    """A larger budget never renders fewer rows, nor fewer names in a row that
    renders at both budgets. Swept over the whole range, not just the ticket's
    five budgets."""
    for target in ("tools/auto/coder.py", "tools/auto/context_assembler.py", "main.py"):
        previous_rows = None
        previous_counts: "dict[str, int]" = {}
        for budget in range(40, 2200, 2):
            block = build_collect_context_block(live_model, target, budget=budget)
            assert len(block) <= budget, (target, budget, len(block))
            rendered = _rows_of(block)
            if previous_rows is not None:
                assert len(rendered) >= len(previous_rows), (
                    f"{target} budget={budget}: {len(rendered)} rows after {len(previous_rows)}"
                )
            counts: "dict[str, int]" = {}
            for line in block.split("\n"):
                if line.startswith(_SYMBOLS_PREFIX):
                    counts["public_symbols"] = _n_names(line)
                elif line.startswith(_CONFIG_PREFIX):
                    counts["config_read"] = len(_lines_of(block, _CONFIG_PREFIX))
                elif line.startswith(_CONTRACT_PREFIX):
                    counts["contract"] = len(_lines_of(block, _CONTRACT_PREFIX))
                elif line.startswith("callers: "):
                    counts["callers"] = _n_names(line)
                elif line.startswith("calls_into: "):
                    counts["calls_into"] = _n_names(line)
                elif line.startswith("tests: "):
                    counts["tests"] = _n_names(line)
            for name, count in previous_counts.items():
                if name in counts:
                    assert counts[name] >= count, (
                        f"{target} budget={budget}: {name} shows {counts[name]} after {count}"
                    )
            previous_rows, previous_counts = rendered, counts


def test_live_every_row_is_at_its_floor_or_fuller(live_model):
    """The cut never goes below a floor: a `+N` row at its count still states
    the count, and a line-based row still states one complete line."""
    for target in ("tools/auto/coder.py", "main.py"):
        for budget in (1717, 900, 600, 400, 250, 180, 140, 110):
            block = build_collect_context_block(live_model, target, budget=budget)
            for name in ("callers", "calls_into", "tests"):
                lines = _lines_of(block, f"{name}: ")
                if not lines:
                    continue
                assert _count_ok(lines[0], _ROWS[name](live_model, target, 0)), (
                    f"{target} budget={budget}: {lines[0]!r} lost its count"
                )
            full_reads = _lines_of(_row_config_read(live_model, target, None), _CONFIG_PREFIX)
            for line in _lines_of(block, _CONFIG_PREFIX):
                assert line in full_reads, (
                    f"{target} budget={budget}: {line!r} is not a complete call site"
                )


def test_the_shrinkable_rows_are_still_just_the_three_count_rows():
    """`_SHRINKABLE_ROWS` keeps meaning "a count plus a courtesy list of names":
    the line-based rows are not in it, because they have no count to fall back
    to and no floor is reserved for them."""
    assert _SHRINKABLE_ROWS == {"callers", "calls_into", "tests"}
    for name in ("neighbours", "contract", "config_read", "public_symbols"):
        assert name not in _SHRINKABLE_ROWS
    assert [name for name, _ in _PACK_ROWS][-1] == "public_symbols"
