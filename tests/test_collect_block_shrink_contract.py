"""tests/test_collect_block_shrink_contract.py — L6 follow-up.

The L6 fix (ced5940) landed the budget allocation; these tests pin the
*contracts underneath it* that the ticket describes but the original suite did
not directly assert. Without them, each of the invariants below can regress by
a one-line edit to a helper and every block-level test still passes, because the
block-level tests only observe the allocation's output at a few budgets.

  S-1  ``_fitted_names`` never returns a names-bearing form longer than its
       allowance, is monotone in the allowance, and ``None`` gives the full
       capped row — the three properties the whole two-pass allocation stands
       on.
  S-2  When no names-bearing form fits, ``_fitted_names`` returns ``bare`` —
       and returns it even when ``bare`` itself exceeds the allowance. The
       caller asks for a form to *measure*, and the budget check in
       ``build_collect_context_block`` is the only thing that decides whether
       the row renders. Flipping that would make a fact row vanish instead of
       being measured, which is exactly the L6 defect.
  S-3  ``_SHRINKABLE_ROWS`` and ``_PACK_ROWS`` cannot diverge silently: every
       shrinkable name is a real row, and each shrinkable row's floor is its
       shortest form across a full allowance sweep. The floor is what the
       reservation is built from, so a renderer that does not honour the
       shrinking contract would mis-reserve silently.
  S-4  ``public_symbols`` is monotone in the allowance at renderer level, so a
       larger allowance never yields fewer symbols — the trim loop inside it
       exists only to hold that.
  S-5  A row that renders a *shrunk* form still suppresses an identical line in
       a later row. L6 moved the ``seen`` update past the budget check; the
       mirror image (the row that *did* render) must keep contributing.

Only structural dataclasses — no artifact on disk, no LLM.
"""

from __future__ import annotations

import re

import pytest

from tools.auto.context_assembler import (
    _CALLERS_PREFIX,
    _CALLS_INTO_PREFIX,
    _CUT_NOTE,
    _PACK_ROWS,
    _SHRINKABLE_ROWS,
    _TESTS_PREFIX,
    _capped_names,
    _fitted_names,
    _row_public_symbols,
    build_collect_context_block,
)
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import FunctionRecord, ModuleRecord


TARGET = "pkg/hub.py"
_SYMBOLS_PREFIX = "public_symbols: "

_NAMES = tuple(f"pkg/module_number_{i:02d}.py" for i in range(12))
_SYMBOLS = tuple(f"{TARGET}:public_symbol_number_{i:02d}" for i in range(12))


def _block_line(block: str, prefix: str) -> str:
    """The block's one line starting with ``prefix``, or ``""`` when absent."""
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line
    return ""


def _n_names(line: str) -> int:
    """Path or symbol names in a row line. The count lead and the announced
    ``+N`` tail are not names."""
    return len(re.findall(r"[\w./-]+\.py(?::[\w.]+)?", line))


# ── S-1: _fitted_names honour the allowance ────────────────────────────────


def test_fitted_names_never_exceeds_the_allowance_when_it_keeps_names():
    """Every names-bearing form it returns must fit, so the allowance is a real
    ceiling and not a hint."""
    head = _CALLS_INTO_PREFIX
    bare = f"calls_into: {len(_NAMES)} targets"
    for remaining in range(0, 1000):
        line = _fitted_names(head, bare, _NAMES, 5, remaining)
        if _n_names(line):
            assert len(line) <= remaining, (remaining, line)


def test_fitted_names_is_monotone_in_the_allowance():
    """Names are dropped from the end, one at a time: a larger allowance never
    yields a shorter line or fewer names."""
    head = _TESTS_PREFIX + "12 files: "
    bare = f"tests: {len(_NAMES)} files"
    for remaining in range(0, 1000):
        line = _fitted_names(head, bare, _NAMES, 3, remaining)
        assert _n_names(line) >= 0
    widths = [len(_fitted_names(head, bare, _NAMES, 3, r)) for r in range(0, 1000)]
    assert all(a <= b for a, b in zip(widths, widths[1:])), "the shrink is monotone in width"
    counts = [_n_names(_fitted_names(head, bare, _NAMES, 3, r)) for r in range(0, 1000)]
    assert all(a <= b for a, b in zip(counts, counts[1:])), "the shrink is monotone in names"


def test_fitted_names_with_no_budget_gives_the_full_capped_row():
    """``remaining=None`` must be byte-identical to the pre-L6 call, so every
    pre-L6 caller keeps working."""
    for head, bare in (
        (_CALLS_INTO_PREFIX, f"calls_into: {len(_NAMES)} targets"),
        (_TESTS_PREFIX + "12 files: ", f"tests: {len(_NAMES)} files"),
    ):
        assert _fitted_names(head, bare, _NAMES, 5, None) == f"{head}{_capped_names(_NAMES, 5)}"


def test_fitted_names_announced_remainder_always_reconciles():
    """At every level of the cut the announced remainder is the remainder of
    *this* list, so listed-plus-announced always equals the total."""
    head = _CALLERS_PREFIX + "12 modules import this: "
    bare = "callers: 12 modules import this"
    for remaining in range(0, 1000, 3):
        line = _fitted_names(head, bare, _NAMES, 5, remaining)
        if _CUT_NOTE.strip(", ") in line or ", +" in line:
            announced = int(line.rsplit(", +", 1)[1])
            assert _n_names(line) + announced == len(_NAMES), (remaining, line)


# ── S-2: the count alone is always available ───────────────────────────────


def test_fitted_names_falls_back_to_bare_when_no_names_fit():
    """``bare`` is the count alone — the one thing the row still says when its
    names do not fit."""
    bare = f"tests: {len(_NAMES)} files"
    head = _TESTS_PREFIX + "12 files: "
    line = _fitted_names(head, bare, _NAMES, 3, 0)
    assert line == bare
    assert _n_names(line) == 0


def test_fitted_names_returns_bare_even_when_bare_does_not_fit():
    """The measure-don't-decide contract: the caller measures the floor and the
    budget check in ``build_collect_context_block`` decides. Returning ``""``
    here instead of ``bare`` would make the row unmeasurable, and an
    unmeasurable row is a row the loop cannot fit — the L6 displacement."""
    bare = f"tests: {len(_NAMES)} files"
    head = _TESTS_PREFIX + "12 files: "
    for remaining in range(0, len(bare)):
        assert _fitted_names(head, bare, _NAMES, 3, remaining) == bare, remaining


def test_fitted_names_is_empty_only_when_there_is_nothing_to_count():
    """An empty name list is the one case where the row genuinely has nothing
    to say."""
    head = _TESTS_PREFIX + "0 files: "
    bare = "tests: 0 files"
    assert _fitted_names(head, bare, (), 3, None) == ""
    assert _fitted_names(head, bare, (), 3, 0) == ""
    assert _fitted_names(head, bare, (), 3, 500) == ""


# ── S-3: the shrinkable set cannot diverge from the row list ──────────────


def test_every_shrinkable_name_is_a_real_pack_row():
    """The set is by name and the tuple is the priority order; a name that
    drifts out of sync would reserve a floor for a row that does not exist, or
    leave a real row without one."""
    names = {name for name, _ in _PACK_ROWS}
    assert _SHRINKABLE_ROWS <= names, f"{_SHRINKABLE_ROWS - names} are not rows"
    for name in _SHRINKABLE_ROWS:
        assert name in dict(_PACK_ROWS)


def test_each_shrinkable_row_floor_is_its_shortest_form():
    """The reservation is built from ``render(model, target, 0)``. That is only
    a floor if no smaller allowance could produce a longer line — i.e. the
    renderer honours the shrinking contract the allocation assumes."""
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(ModuleRecord(path=TARGET),) + tuple(ModuleRecord(path=p) for p in _NAMES),
        imported_by={TARGET: _NAMES},
        import_edges={TARGET: _NAMES},
        test_map={TARGET: _NAMES},
    )
    for name, render in _PACK_ROWS:
        if name not in _SHRINKABLE_ROWS:
            continue
        floor = render(model, TARGET, 0)
        assert floor, f"{name} has no count-only floor"
        assert _n_names(floor) == 0, f"{name}'s floor {floor!r} still carries names"
        for remaining in range(0, 1200):
            assert len(render(model, TARGET, remaining)) >= len(floor), (name, remaining)


def test_public_symbols_is_last_and_has_no_floor_reserved():
    """It stays last and is not in the shrinkable set, so it only ever takes
    the budget no row above it could use — the L6 guarantee."""
    assert "public_symbols" not in _SHRINKABLE_ROWS
    assert [name for name, _ in _PACK_ROWS][-1] == "public_symbols"
    assert "public_symbols" not in {name for name, _ in _PACK_ROWS if name in _SHRINKABLE_ROWS}


# ── S-4: public_symbols is monotone in the allowance ──────────────────────


def _symbol_model() -> CollectModel:
    return CollectModel(
        status=STATUS_FRESH,
        modules=(
            ModuleRecord(
                path=TARGET,
                public_symbols=tuple(
                    FunctionRecord(qualname=q, module=TARGET, lineno=i + 1, signature="x(...)")
                    for i, q in enumerate(_SYMBOLS)
                ),
            ),
        ),
    )


def test_public_symbols_is_monotone_in_the_allowance():
    """A larger allowance never yields fewer symbols. The trim loop inside the
    renderer exists only to hold this, and nothing else in the suite checks it
    at renderer level."""
    model = _symbol_model()
    full = _row_public_symbols(model, TARGET, None)
    assert _row_public_symbols(model, TARGET, len(full)) == full

    widths = [len(_row_public_symbols(model, TARGET, r)) for r in range(0, len(full) + 2)]
    assert all(a <= b for a, b in zip(widths, widths[1:])), "width is monotone"

    counts = []
    for r in range(0, len(full) + 2):
        line = _row_public_symbols(model, TARGET, r)
        if not line:
            counts.append(0)
            continue
        body = line[len(_SYMBOLS_PREFIX):]
        listed = [p for p in body.split(",") if p.strip() and "cut for budget" not in p]
        counts.append(len(listed))
    assert all(a <= b for a, b in zip(counts, counts[1:])), "symbol count is monotone"


def test_public_symbols_cut_note_always_reconciles():
    """Listed plus announced always equals the total at every cut level."""
    model = _symbol_model()
    for r in range(0, len(_row_public_symbols(model, TARGET, None)) + 2, 3):
        line = _row_public_symbols(model, TARGET, r)
        if not line:
            continue
        body = line[len(_SYMBOLS_PREFIX):]
        listed = [p for p in body.split(",") if p.strip() and "cut for budget" not in p]
        announced = int(body.rsplit("(+", 1)[1].split(" more", 1)[0]) if "more, cut for budget" in body else 0
        assert len(listed) + announced == len(_SYMBOLS), (r, line)


# ── S-5: a rendered shrunk row still feeds `seen` ─────────────────────────


def test_a_rendered_shrunk_row_still_suppresses_an_identical_later_line(monkeypatch):
    """Mirror of the L6 fix: ``seen`` is updated only for rows that render, so
    a skipped row leaves no trace. The other half of the invariant is that a
    row which *did* render — in a shrunk form — still suppresses a duplicate."""
    import tools.auto.context_assembler as ca

    model = CollectModel(
        status=STATUS_FRESH,
        modules=(ModuleRecord(path=TARGET),) + tuple(ModuleRecord(path=p) for p in _NAMES),
        imported_by={TARGET: _NAMES},
    )

    def _shrunk(model_, target, remaining):
        # A names-bearing form that fits only a small allowance: it renders,
        # shrunk, and must still contribute its lines to `seen`.
        return "shared line\npkg/module_number_00.py"

    def _duplicate(model_, target, remaining):
        return "shared line\npkg/module_number_01.py"

    monkeypatch.setattr(
        ca, "_PACK_ROWS", (("shrunk", _shrunk), ("duplicate", _duplicate))
    )
    block = build_collect_context_block(model, TARGET, budget=200)
    assert "shared line" in block, "the shrunk row must render at all"
    assert block.count("shared line") == 1, (
        "a rendered (shrunk) row must still suppress the identical later line"
    )


def test_rendered_rows_are_never_reordered(monkeypatch):
    """The emitted order is the tuple order; a regression that let a later row
    print before an earlier one would read as a corruption of the priority."""
    import tools.auto.context_assembler as ca

    rows = tuple(
        (f"row{i}", lambda model_, target, remaining, _i=i: f"row_{_i}: data")
        for i in range(5)
    )
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(ModuleRecord(path=TARGET),),
    )
    monkeypatch.setattr(ca, "_PACK_ROWS", rows)

    block = build_collect_context_block(model, TARGET)
    labels = [line.split(":")[0] for line in block.split("\n")[2:]]
    assert labels == [f"row_{i}" for i in range(5)]

    # and when only alternate rows have anything to say, the survivors keep order
    rows2 = tuple(
        (
            name,
            (lambda model_, target, remaining, _n=name: f"row_{_n[4]}: data" if int(_n[4]) % 2 == 0 else ""),
        )
        for name, _ in rows
    )
    monkeypatch.setattr(ca, "_PACK_ROWS", rows2)
    labels2 = [line.split(":")[0] for line in build_collect_context_block(model, TARGET).split("\n")[2:]]
    assert labels2 == sorted(labels2, key=lambda l: int(l[4]))
