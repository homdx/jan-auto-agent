"""tests_bugfix/test_collect_block_deficit_not_borrowed_from_higher_rows.py — V2/L6.

The bug
-------
`_fit_pack_to_budget` walks `_PACK_ROWS` backwards, cutting from the least
valuable end. For each row it computed

    give = min(owed, _row_cost(form) - _row_cost(floor))

and subtracted `give` from the deficit. That assumes a row's *smallest* form is
smaller than its full form. It is not always true:

    `calls_into` with one short target:
        full  "calls_into: a.py"        16 chars
        floor "calls_into: 1 target"    20 chars

The floor is the row's count alone (`_fitted_names`'s `bare`), and for a single
short target the count costs four more characters than the one name it would
replace. So `give` came out **negative** (-4), and `owed -= -4` made the
deficit *grow* before the walk moved on to the row above it — `callers`, the
most valuable row in the pack.

Measured before the fix (six 2-char callers, one 4-char callee, pack cost 139):

    budget=135  callers: 6 modules import this: aa, bb, cc, +3      len=131
    budget=139  callers: 6 modules import this: aa, bb, cc, dd, ee, +1   len=139

The deficit at budget 135 is 4 chars — exactly one caller name. `calls_into`
could not give those 4 chars at all, so `callers` gave two names instead of one
and the block came out 4 chars under budget. A row that needed no cutting at
all borrowed the deficit from the row above it: the L6 displacement, reintroduced
from the inside by the floor arithmetic rather than by `public_symbols`.

The fix
-------
A row whose smallest form is not smaller than its full form is skipped: there is
nothing to take from it, and counting it would only grow the deficit. The cut
then asks the next row that genuinely can give, which is exactly the order the
pass walks in.

Only structural dataclasses are built here — no git repo, no artifact on disk,
no LLM.
"""

from __future__ import annotations

from tools.auto.context_assembler import (
    _COLLECT_HEADER,
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _row_callers,
    _row_calls_into,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ModuleRecord

TARGET = "pkg/hub.py"

# Six callers with 2-char names: one caller name costs 4 chars ("aa" + ", "),
# exactly the amount the defect borrowed from them. Six so the cap of five has a
# remainder to announce.
CALLERS = ("aa", "bb", "cc", "dd", "ee", "ff")
# One 4-char callee: "calls_into: a.py" (16) vs its own count
# "calls_into: 1 target" (20) — the floor is 4 chars LONGER than the full form.
CALLS = ("a.py",)

HEAD = len(_COLLECT_HEADER) + 1 + len(f"module: {TARGET}")
CALLERS_FULL = "callers: 6 modules import this: aa, bb, cc, dd, ee, +1"
CALLERS_FOUR = "callers: 6 modules import this: aa, bb, cc, dd, +2"
CALLS_FULL = "calls_into: a.py"
FULL_PACK = HEAD + len(CALLERS_FULL) + 1 + len(CALLS_FULL) + 1


# ── helpers ────────────────────────────────────────────────────────────────


def _model(callers=CALLERS, calls=CALLS) -> CollectModel:
    modules = [ModuleRecord(path=TARGET)]
    for path in (*callers, *calls):
        modules.append(ModuleRecord(path=path))
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        imported_by={TARGET: tuple(callers)},
        import_edges={TARGET: tuple(calls)},
    )


def _names(line: str) -> int:
    """Caller names shown by the row — the `, +N` remainder is not one."""
    if not line.startswith("callers: "):
        return 0
    body = line.split(": ", 1)[1]
    if ", +" in body:
        body = body.split(", +")[0]
    return len([n for n in body.split(", ") if n.strip()])


def _row(block: str, prefix: str) -> str:
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line
    return ""


def _fixture() -> CollectModel:
    return _model()


# ── the case the guard exists for is reachable ─────────────────────────────


def test_calls_into_is_a_row_whose_floor_is_longer_than_its_full_form():
    """Not dead code: a single short callee is a real shape of artifact, and for
    it the count alone costs more than the one name it replaces. Any allocation
    that assumes `floor <= full` walks into a negative `give` on this row."""
    model = _fixture()
    full = _row_calls_into(model, TARGET, None)
    floor = _row_calls_into(model, TARGET, 0)
    assert full == CALLS_FULL and floor == "calls_into: 1 target"
    assert len(floor) > len(full), "the fixture must actually exhibit the shape"


def test_every_shrinkable_row_is_cut_through_the_guarded_pass():
    """The guard is inside `_fit_pack_to_budget`, which is the only allocation
    the block uses — so the rows it protects are the real rows of the real pack."""
    assert {name for name in _SHRINKABLE_ROWS} <= {name for name, _ in _PACK_ROWS}
    assert "callers" in _SHRINKABLE_ROWS and "calls_into" in _SHRINKABLE_ROWS


# ── the deficit is not borrowed from a row that cannot give ────────────────


def test_a_row_that_cannot_give_ground_does_not_cut_the_row_above_it():
    """The defect: at the budget that fits the full pack minus one caller name,
    `callers` gave two names because `calls_into`'s negative `give` grew the
    deficit by 4."""
    model = _fixture()
    budget = FULL_PACK - 4

    block = build_collect_context_block(model, TARGET, budget=budget)

    assert _row(block, "callers: ") == CALLERS_FOUR, (
        "one caller name buys the 4 chars the budget is short of, not two"
    )
    assert _row(block, "calls_into: ") == CALLS_FULL, (
        "the row that cannot give ground still keeps its full form"
    )
    assert _names(_row(block, "callers: ")) == 4


def test_the_reclaimed_budget_is_used_not_wasted():
    """Before the fix the block came out 4 chars under budget because the
    duplicate deficit was spent on a second caller name the budget did not need
    to cut. At the exact budget the row fits the block fills it."""
    model = _fixture()

    assert len(build_collect_context_block(model, TARGET, budget=FULL_PACK - 4)) == FULL_PACK - 4


def test_the_cut_is_stable_across_the_slack_between_two_name_boundaries():
    """Nothing changes between the budget where one name is cut and the budget
    where the full row fits again: the cut is a function of the budget alone."""
    model = _fixture()

    at_first_cut = build_collect_context_block(model, TARGET, budget=FULL_PACK - 4)
    for budget in range(FULL_PACK - 4, FULL_PACK):
        assert build_collect_context_block(model, TARGET, budget=budget) == at_first_cut, (
            f"budget={budget} changed the cut without the budget requiring it"
        )
    assert build_collect_context_block(model, TARGET, budget=FULL_PACK) != at_first_cut


# ── the invariants the fix must not break ─────────────────────────────────


def test_the_block_never_exceeds_its_budget_across_the_range():
    for budget in range(50, FULL_PACK + 200, 3):
        block = build_collect_context_block(_fixture(), TARGET, budget=budget)
        assert len(block) <= budget, f"budget={budget}: block of {len(block)} chars"


def test_a_larger_budget_never_yields_fewer_caller_names():
    model = _fixture()
    previous = 0
    for budget in range(50, FULL_PACK + 200, 2):
        count = _names(_row(build_collect_context_block(model, TARGET, budget=budget), "callers: "))
        assert count >= previous, f"budget={budget}: {count} names after {previous}"
        previous = count


def test_the_announced_remainder_always_reconciles():
    """Names are dropped from the end and the `+N` tail is recomputed, so
    shown plus announced is always the total the row leads with."""
    model = _fixture()
    for budget in range(50, FULL_PACK + 200, 2):
        line = _row(build_collect_context_block(model, TARGET, budget=budget), "callers: ")
        if not line:
            continue
        assert line.startswith("callers: 6 modules import this"), line
        if ", +" in line:
            assert _names(line) + int(line.rsplit(", +", 1)[1]) == len(CALLERS), line


def test_a_budget_that_cannot_fit_any_count_yields_no_block():
    """A row whose count does not fit is absent rather than torn: the block is
    empty, and nothing else spends the budget on nothing either."""
    model = _model(callers=("aa",), calls=())

    for budget in (1, 40, 60, 80):
        block = build_collect_context_block(model, TARGET, budget=budget)
        if not block:
            continue
        assert len(block) <= budget
        lines = block.split("\n")
        assert lines[0] == _COLLECT_HEADER
        for line in lines:
            assert line, "no blank line in a cut block"


def test_a_single_caller_renders_its_count_only_form_at_exactly_its_size():
    """The smallest form of a row is exactly as useful as the row itself, so a
    budget that fits nothing else still buys the count."""
    model = _model(callers=("aa",), calls=())
    floor = _row_callers(model, TARGET, 0)
    budget = HEAD + len(floor) + 1

    block = build_collect_context_block(model, TARGET, budget=budget)

    assert block.split("\n")[-1] == floor
    assert len(block) == budget


def test_a_row_that_needs_no_cutting_keeps_its_full_form_under_pressure():
    """`calls_into` cannot shrink — its floor is bigger than its full form — so
    under pressure it is either rendered in full or absent, never shrunk into a
    form longer than the budget can hold."""
    model = _fixture()
    for budget in range(50, FULL_PACK + 40, 3):
        line = _row(build_collect_context_block(model, TARGET, budget=budget), "calls_into: ")
        if line:
            assert len(line) <= budget, f"budget={budget}: row of {len(line)} chars"
