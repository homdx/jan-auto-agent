"""AUTO-P5-v2 — ArchProbe.last_by_op handed out shared mutable state.

Field report / ground-truth finding (`GROUND-competition.md`,
`tasks/05-archprobe-last-by-op.md`): ``ArchProbe.last_by_op`` returned
``dict(self._last_by_op)``. ``dict()`` copies the mapping but not its
values, and the values are the exact ``[hits, misses]`` lists
``_resolve_op`` tallies into via ``self._last_by_op.setdefault(op, [0,
0])``. So the "copy" was a block of shared mutable state in a trench coat:

    tally = probe.last_by_op["facts"]
    tally[0] += 1                       # looks local...
    probe.execute([...])                # ...but corrupts the probe's own
    assert probe.last_by_op["facts"][0] == 1   #   count on the next call

Same shape of bug as the one fixed for ``StateStore`` in `4796e0a` (see
``tests_bugfix/test_bugfix_m1_get_task_reference_leak.py``): a read
accessor returned a live (or shallow-copied) mutable, so an innocent-
looking write from outside reached state the class still relies on.

AUTO-P5-v2 does not just deep-copy the block. It removes the block: the
dict-of-lists contract is replaced with an ordered tuple of frozen
``ProbeOpTally`` rows, so there is no mutable value left to alias in the
first place — and ordering by op name means ``last_by_op`` and
``last_by_op_str()`` (which already sorted) now agree with each other.

These tests fail if either half regresses: shared state reappearing, or
the rows silently losing their order.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.arch_probe import ArchProbe, ProbeOp, ProbeOpTally  # noqa: E402


class _FakeBridge:
    """Minimal double: only `usable` / `pull_symbol` / `module_symbols`
    matter to ArchProbe's tallying path."""

    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}

    def pull_symbol(self, name: str) -> str:
        return self._answers.get(name, "")

    def module_symbols(self, ref: str) -> str:
        return self._answers.get(("module", ref), "")


def _probe(**answers: str) -> ArchProbe:
    return ArchProbe(_FakeBridge(answers))


# ── the reported escape path ─────────────────────────────────────────────────

def test_mutating_a_last_by_op_row_cannot_reach_the_probe() -> None:
    """The exact reproduction from the finding — a frozen row raises on
    assignment instead of silently accepting the write."""
    probe = _probe(good="module: x\nsignature: good()")
    probe.execute([ProbeOp("facts", "good")])

    row = probe.last_by_op[0]
    with pytest.raises((AttributeError, TypeError)):
        row.hits += 1  # would have been tally[0] += 1 under the old dict shape

    # And regardless of the exception above, the probe's own count is
    # unaffected by anything the caller tried.
    probe.execute([ProbeOp("facts", "good")])
    assert probe.last_by_op == (ProbeOpTally("facts", 1, 0),)


def test_two_reads_are_independent_of_each_other() -> None:
    """Not just immutable — each call gets its own tuple, so one caller
    holding an old reference never sees a later execute()'s numbers."""
    probe = _probe(good="module: x\nsignature: good()")
    probe.execute([ProbeOp("facts", "good")])
    first = probe.last_by_op

    probe.execute([ProbeOp("facts", "good"), ProbeOp("facts", "missing")])
    second = probe.last_by_op

    assert first == (ProbeOpTally("facts", 1, 0),)
    assert second == (ProbeOpTally("facts", 1, 1),)


# ── the block is now an ordered row list ─────────────────────────────────────

def test_last_by_op_is_an_ordered_tuple_of_rows() -> None:
    """The contract is a tuple of ProbeOpTally, not a dict — and it is
    ordered by op name, not by first-seen order."""
    probe = _probe(x="module: x\nsignature: x()")
    # Ask for "read"-shaped, "module"-shaped and "facts"-shaped names in an
    # order that would NOT be alphabetical if last_by_op merely preserved
    # insertion order.
    probe.execute([
        ProbeOp("facts", "zzz"),
        ProbeOp("facts", "x"),
    ])

    rows = probe.last_by_op
    assert isinstance(rows, tuple)
    assert all(isinstance(r, ProbeOpTally) for r in rows)
    assert [r.op for r in rows] == sorted(r.op for r in rows)


def test_last_by_op_str_matches_last_by_op_exactly() -> None:
    """Before the fix, last_by_op_str() sorted independently of last_by_op
    (insertion order) — two views of the same run could disagree on order.
    Now last_by_op_str() is built FROM last_by_op, so they cannot drift."""
    probe = _probe(a="module: x\nsignature: a()")
    probe.execute([
        ProbeOp("module", "tools/nope.py"),
        ProbeOp("facts", "a"),
    ])

    rows = probe.last_by_op
    assert probe.last_by_op_str() == " ".join(str(r) for r in rows)
    assert probe.last_by_op_str() == "facts=1/0 module=0/1"


# ── the fix must not break the thing it guards ───────────────────────────────

def test_rows_still_carry_the_right_counts() -> None:
    probe = _probe(hit="module: x\nsignature: hit()")
    probe.execute([
        ProbeOp("facts", "hit"),
        ProbeOp("facts", "miss"),
        ProbeOp("module", "tools/nope.py"),
    ])
    assert probe.last_by_op == (
        ProbeOpTally("facts", 1, 1),
        ProbeOpTally("module", 0, 1),
    )


def test_empty_execute_yields_empty_rows() -> None:
    probe = _probe()
    assert probe.last_by_op == ()
    assert probe.last_by_op_str() == ""
