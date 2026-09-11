"""tests/test_collect_block_budget_keeps_fact_rows.py — L6.

The budget loop used to skip a row whole when it did not fit and move on. Only
`public_symbols` could shrink, so under pressure it was the row that survived —
and it is the one row the target file's own source already makes redundant.
Measured on `tools/auto/coder.py` before this ticket:

    budget=1717  callers calls_into tests config_read... public_symbols
    budget= 900  callers calls_into tests public_symbols
    budget= 400  callers calls_into public_symbols        <- tests dropped
    budget= 250  callers public_symbols                   <- calls_into dropped

This file pins the fix:

  L6-1  `callers` / `calls_into` / `tests` drop names from the end, down to
        their count alone, before the loop gives up on them. The count is the
        fact; the names are the courtesy.
  L6-2  `public_symbols` renders last, into whatever the rows above leave. It
        is never rendered while a fact row that has data is absent.
  L6-3  A row skipped for budget leaves no trace in `seen`, so an identical
        line in a later row still renders.
  L6-4  Monotone: a larger allowance never yields fewer names, and the block
        never exceeds its budget.
  L6-5  `_PACK_ROWS` keeps its order and its seven rows.

`CollectBridge._shrink` is untouched by this ticket — everything here runs
before it, in `build_collect_context_block`. Only structural dataclasses are
built, except the live section at the bottom, which reads this repo's own tree
through the producer's graph and test-map passes rather than from `.collect/`,
so it cannot skip for a missing artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.auto.context_assembler import (
    _CALLERS_PREFIX,
    _CALLS_INTO_PREFIX,
    _COLLECT_HEADER,
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _TESTS_PREFIX,
    _row_callers,
    _row_calls_into,
    _row_neighbours,
    _row_tests,
    build_collect_context_block,
)
from tools.collect import graph as graph_mod
from tools.collect import scanner as scanner_mod
from tools.collect import test_map as test_map_mod
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ConfigRead, FunctionRecord, LLMSummary, ModuleRecord

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGET = "pkg/hub.py"
_SYMBOLS_PREFIX = "public_symbols: "

# The three rows that carry a `+N` tail: a count the row leads with, then a
# courtesy list of names. These are the rows L6 makes shrinkable.
_FACT_ROWS = {
    _row_callers: _CALLERS_PREFIX,
    _row_calls_into: _CALLS_INTO_PREFIX,
    _row_tests: _TESTS_PREFIX,
}
_FACT_PREFIXES = tuple(_FACT_ROWS.values())

# How much data this hub carries, so an assertion reconciles a rendered count
# against the model instead of hard-coding a number that will drift.
_N_CALLERS = 24
_N_CALLS = 20
_N_TESTS = 24
_N_SYMBOLS = 40


# ── helpers ────────────────────────────────────────────────────────────────


def _symbols() -> "tuple[FunctionRecord, ...]":
    """Long qualnames, so the `public_symbols` floor costs real budget."""
    return tuple(
        FunctionRecord(
            qualname=f"{TARGET}:public_function_number_{i:02d}",
            module=TARGET,
            lineno=i + 1,
            signature=f"public_function_number_{i:02d}(...)",
        )
        for i in range(_N_SYMBOLS)
    )


def _hub(symbols: "tuple[FunctionRecord, ...]" = (), config_reads: "tuple[ConfigRead, ...]" = ()):
    """A hub with 24 callers, 20 callees, 24 covering tests and, optionally, a
    symbol list. The counts are large enough that every cap has a remainder to
    announce, and every path is long enough that a count alone costs budget —
    which is what makes the shrink visible at a tight budget rather than free.
    """
    callers = tuple(f"pkg/caller_module_{i:02d}.py" for i in range(_N_CALLERS))
    calls = tuple(f"pkg/dependency_module_{i:02d}.py" for i in range(_N_CALLS))
    tests = tuple(f"tests/test_hub_case_{i:02d}.py" for i in range(_N_TESTS))
    modules = [
        ModuleRecord(
            path=TARGET,
            public_symbols=symbols,
            config_reads=config_reads,
            summary=LLMSummary(purpose="The hub under budget."),
        ),
        ModuleRecord(path="pkg/partner.py", summary=LLMSummary(purpose="A neighbour's purpose.")),
    ]
    for path in (*callers, *calls, *tests):
        modules.append(ModuleRecord(path=path))
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={TARGET: calls},
        imported_by={TARGET: callers},
        test_map={TARGET: tests},
    )


def _line(block: str, prefix: str) -> str:
    """The block's one line starting with `prefix`, or `""` when absent."""
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line
    return ""


_NAME_TOKEN = re.compile(r"[\w./-]+\.py(?::[\w.]+)?")


def _names(line: str) -> int:
    """The paths or symbols a row line shows. The `, +N` announcement is not
    one, and neither is the count the row leads with."""
    return len(_NAME_TOKEN.findall(line))


def _announced_total(line: str) -> int:
    """The total a `+N` row accounts for: the names it shows plus the remainder
    it announces."""
    announced = int(line.rsplit(", +", 1)[1]) if ", +" in line else 0
    return _names(line) + announced


def _head_len() -> int:
    return len(_COLLECT_HEADER) + 1 + len(f"module: {TARGET}")


# ── L6-1: the fact rows shrink to their count ──────────────────────────────


def test_every_fact_row_has_a_count_only_floor_with_no_names_in_it():
    """`tests: 24 files: a, b, c, +21` is worth more than nothing, so each row
    must have a form that keeps the count and drops every name."""
    model = _hub()
    for render, prefix in _FACT_ROWS.items():
        floor = render(model, TARGET, 0)
        assert floor.startswith(prefix), f"{prefix.strip()} has no count-only form"
        assert _names(floor) == 0, f"{floor!r} should carry the count alone"


def test_the_shrink_is_monotone_in_the_allowance():
    """Names are dropped from the end, one at a time: a larger allowance never
    yields a shorter row, and a generous one gives the full row back."""
    model = _hub()
    for render in _FACT_ROWS:
        full = render(model, TARGET, None)
        assert render(model, TARGET, len(full)) == full, "a full allowance gives the full row"
        widths = [len(render(model, TARGET, remaining)) for remaining in range(0, len(full) + 1)]
        assert all(a <= b for a, b in zip(widths, widths[1:])), "the shrink is monotone"


def test_a_larger_allowance_never_yields_fewer_names():
    model = _hub()
    for render in _FACT_ROWS:
        previous = -1
        for remaining in range(0, 4000, 7):
            count = _names(render(model, TARGET, remaining))
            assert count >= previous, (
                f"{render.__name__} at remaining={remaining}: {count} names after {previous}"
            )
            previous = count


def test_a_fact_row_vanishes_only_when_its_count_does_not_fit():
    """Below the count the row is gone; at the count it is back, with no names
    in it. Nothing in between."""
    model = _hub()
    head = _head_len()
    for render, prefix in _FACT_ROWS.items():
        floor = render(model, TARGET, 0)
        short_by_one = head + len(floor) + 1 - 1
        assert not _line(
            build_collect_context_block(model, TARGET, budget=short_by_one), prefix
        ), f"{prefix.strip()} rendered inside its own count"

        at_the_count = head + len(floor) + 1
        line = _line(build_collect_context_block(model, TARGET, budget=at_the_count), prefix)
        assert line == floor, f"{line!r} is not the count-only form {floor!r}"


# ── L6-2: public_symbols only takes what the rows above leave ──────────────


def test_public_symbols_never_displaces_a_fact_row():
    """The defect this ticket exists to remove: swept across the whole budget
    range, `public_symbols` is never rendered while a fact row that has data is
    absent."""
    model = _hub(_symbols())
    assert _line(build_collect_context_block(model, TARGET), _SYMBOLS_PREFIX), (
        "the fixture needs symbols for this test to mean anything"
    )
    for budget in range(40, 2600, 3):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget, f"budget={budget}: block of {len(block)} chars"
        if _line(block, _SYMBOLS_PREFIX):
            for prefix in _FACT_PREFIXES:
                assert _line(block, prefix), (
                    f"budget={budget}: public_symbols displaced {prefix.strip()}"
                )


def test_public_symbols_takes_only_the_budget_the_fact_rows_could_not_use():
    """At the budget that fits every count and nothing more, all three fact
    rows render at their counts and `public_symbols` is the one row left
    without room — "static facts survive", applied to the row the target's own
    source makes redundant."""
    model = _hub(_symbols())
    floors = [render(model, TARGET, 0) for render in _FACT_ROWS]
    budget = _head_len() + sum(len(floor) + 1 for floor in floors)

    block = build_collect_context_block(model, TARGET, budget=budget)
    for render, prefix in _FACT_ROWS.items():
        assert _line(block, prefix) == render(model, TARGET, 0), (
            f"{prefix.strip()} is not at its count at budget {budget}"
        )
    assert not _line(block, _SYMBOLS_PREFIX), (
        "public_symbols took the budget the fact rows needed their counts for"
    )


def test_public_symbols_is_last_and_is_not_shrinkable():
    """It cuts at a symbol boundary on its own and is rendered last, into
    whatever the rows above leave. It takes no equal share of the slack,
    because sharing it would let it claim room a fact row could have used for a
    name."""
    assert _SHRINKABLE_ROWS == {"callers", "calls_into", "tests"}
    assert "public_symbols" not in _SHRINKABLE_ROWS
    assert [name for name, _ in _PACK_ROWS][-1] == "public_symbols"


def _neighbourhood() -> CollectModel:
    """A target with three callers that all carry a purpose, so the
    `neighbours` row has three whole entries to shrink through."""
    callers = tuple(f"pkg/neighbour_{i}.py" for i in range(3))
    modules = [ModuleRecord(path=TARGET)]
    for i, path in enumerate(callers):
        modules.append(
            ModuleRecord(path=path, summary=LLMSummary(purpose=f"Purpose of neighbour number {i}."))
        )
    return CollectModel(
        status=STATUS_FRESH, modules=tuple(modules), imported_by={TARGET: callers}
    )


def test_neighbours_shrinks_by_whole_entries_from_the_end_and_has_no_count_form():
    """The ticket lists `neighbours` among the rows that shrink. It has no
    count to fall back to, so it is not in `_SHRINKABLE_ROWS` (no floor is
    reserved for it); instead it drops whole entries from the end — callers
    first are kept — until the row fits, and is absent when not one entry fits.
    Every surviving line is still a complete fact with its `(llm)` label."""
    assert "neighbours" not in _SHRINKABLE_ROWS
    model = _neighbourhood()
    full = _row_neighbours(model, TARGET, None)
    lines = full.split("\n")
    assert len(lines) == 3 and all(line.endswith("(llm)") for line in lines)

    assert _row_neighbours(model, TARGET, len(full)) == full
    two = _row_neighbours(model, TARGET, len(full) - 1)
    assert two.split("\n") == lines[:2]
    one = _row_neighbours(model, TARGET, len(two) - 1)
    assert one == lines[0]
    assert _row_neighbours(model, TARGET, len(one) - 1) == ""
    assert _row_neighbours(model, TARGET, 0) == ""

    # Monotone in the allowance: more room never yields fewer entries.
    counts = [len([l for l in _row_neighbours(model, TARGET, r).split("\n") if l])
              for r in range(0, len(full) + 5)]
    assert counts == sorted(counts)


def test_a_shrunk_neighbours_row_still_leaves_public_symbols_last():
    """In a block, a partially fitting `neighbours` row renders its leading
    entries instead of vanishing whole and handing the room to
    `public_symbols`."""
    model = _neighbourhood()
    unbudgeted = build_collect_context_block(model, TARGET, budget=None).split("\n")
    above = [l for l in unbudgeted if not l.startswith("neighbours:")]
    full = _row_neighbours(model, TARGET, None)
    two = "\n".join(full.split("\n")[:2])
    # Everything above the row, plus exactly two of its three entries.
    budget = len("\n".join(above)) + 1 + len(two) + 1
    block = build_collect_context_block(model, TARGET, budget=budget)
    got = [l for l in block.split("\n") if l.startswith("neighbours:")]
    assert got == two.split("\n")
    assert len(block) <= budget
    assert "public_symbols:" not in block


# ── L6-3: a skipped row leaves no trace in `seen` ──────────────────────────


def test_a_row_skipped_for_budget_leaves_no_trace_in_seen(monkeypatch):
    """V2.4's dedupe added a row's lines to `seen` before the budget check that
    could still skip the row, so a row that never rendered suppressed an
    identical line in a later row. `seen` is now updated only for rows that
    render."""
    import tools.auto.context_assembler as ca

    model = _hub()

    def _oversized(model_, target, remaining):
        return "shared line\n" + "x" * 400

    def _fits(model_, target, remaining):
        return "shared line\n" + "y" * 50

    monkeypatch.setattr(ca, "_PACK_ROWS", (("oversized", _oversized), ("fits", _fits)))

    block = build_collect_context_block(model, TARGET, budget=300)
    assert block.count("shared line") == 1, (
        "a skipped row's line suppressed the identical line in the row that did render"
    )


def test_deduplication_within_one_row_still_happens():
    """Moving the `seen` update past the budget check must not have disabled
    V2.4's dedupe: a row with three identical lines still renders one."""
    reads = tuple(ConfigRead(section="coder", key="num_ctx", fallback=8192) for _ in range(3))
    model = _hub(config_reads=reads)

    block = build_collect_context_block(model, TARGET)
    assert block.count("config_read [coder] num_ctx (fallback=8192)") == 1


# ── L6-4: monotone in the budget ───────────────────────────────────────────


def test_a_larger_budget_never_renders_fewer_names_in_a_block():
    """Swept across the budget range: where the same rows render, a larger
    budget never shows fewer names in any row."""
    model = _hub(_symbols())
    previous: "dict[str, int]" = {}
    for budget in range(40, 2600, 3):
        block = build_collect_context_block(model, TARGET, budget=budget)
        assert len(block) <= budget
        names: "dict[str, int]" = {}
        for line in block.split("\n"):
            for prefix in (*_FACT_PREFIXES, _SYMBOLS_PREFIX):
                if line.startswith(prefix):
                    names[prefix] = _names(line)
                    break
        for prefix, count in previous.items():
            if prefix in names:
                assert names[prefix] >= count, (
                    f"budget={budget}: {prefix.strip()} shows {names[prefix]} names after "
                    f"{count} at a smaller budget"
                )
        previous = names


def test_public_symbols_announces_everything_it_drops_across_budgets():
    """The pre-existing announcement must survive the new allocation: listed
    plus announced always reconciles with the total."""
    model = _hub(_symbols())
    for budget in range(60, 1200, 11):
        line = _line(build_collect_context_block(model, TARGET, budget=budget), _SYMBOLS_PREFIX)
        if not line:
            continue
        body = line[len(_SYMBOLS_PREFIX):]
        listed = [p for p in body.split(",") if p.strip() and "cut for budget" not in p]
        announced = 0
        if "more, cut for budget" in body:
            announced = int(body.rsplit("(+", 1)[1].split(" more", 1)[0])
        assert len(listed) + announced == _N_SYMBOLS, (budget, line)


# ── L6-5: the row list is unchanged ────────────────────────────────────────


def test_the_row_order_and_the_row_count_are_unchanged():
    """L6 adds no rows and reorders nothing: the tuple order IS the priority,
    and the pinned order is what every V2/V3/V5 test reads off."""
    assert [name for name, _ in _PACK_ROWS] == [
        "callers",
        "calls_into",
        "tests",
        "neighbours",
        "contract",
        "config_read",
        "public_symbols",
    ]
    assert len(_PACK_ROWS) == 7


# ── end to end against this repo's own tree ────────────────────────────────


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


def test_live_coder_py_keeps_every_fact_row_at_the_ticket_budgets(live_model):
    """The observation in the ticket, measured after the fix: at 400 the
    `tests` row is still there, and at 250 it is still there as a count.
    Before L6 both were displaced by `public_symbols`."""
    target = "tools/auto/coder.py"
    has_data = {
        prefix: bool(render(live_model, target, None)) for render, prefix in _FACT_ROWS.items()
    }
    assert all(has_data.values()), "coder.py should carry all three fact rows"

    for budget in (1717, 900, 600, 400, 250):
        block = build_collect_context_block(live_model, target, budget=budget)
        assert len(block) <= budget
        if _line(block, _SYMBOLS_PREFIX):
            for prefix in _FACT_PREFIXES:
                assert _line(block, prefix), (
                    f"budget={budget}: public_symbols displaced {prefix.strip()}"
                )

    # Whatever it lost in names, the row keeps its total. `callers` and
    # `tests` lead with it, so their rendered line starts with the count-only
    # form; `calls_into` states it as names plus announced remainder, which must
    # still reconcile with the model.
    for budget in (1717, 900, 600, 400, 250):
        block = build_collect_context_block(live_model, target, budget=budget)
        for render, prefix in _FACT_ROWS.items():
            line = _line(block, prefix)
            if not line:
                continue
            if prefix == _CALLS_INTO_PREFIX:
                assert _announced_total(line) == len(live_model.calls_into(target)), (budget, line)
            else:
                assert line.startswith(render(live_model, target, 0)), (
                    f"budget={budget}: {line!r} lost the count it leads with"
                )

    at_250 = build_collect_context_block(live_model, target, budget=250)
    assert _line(at_250, _TESTS_PREFIX), "the tests row was displaced at budget 250"
    assert not _line(at_250, _SYMBOLS_PREFIX), (
        "public_symbols still takes the budget the tests row needed"
    )


def test_live_blocks_are_never_over_their_budget(live_model):
    """The whole tree, at the budgets the ticket names."""
    for module in live_model.modules:
        for budget in (1717, 900, 600, 400, 250):
            block = build_collect_context_block(live_model, module.path, budget=budget)
            assert len(block) <= budget, (module.path, budget, len(block))
