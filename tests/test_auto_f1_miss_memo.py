"""tests/test_auto_f1_miss_memo.py — AUTO-F1: run-level miss memo.

`facts` fell from 70% to 43% across the epic, and the trace shows why: in
run 13, `retry` alone was asked 60 times and never once resolved — the
Architect is naming a concept, not a real symbol, and nothing remembers
that the name already failed the first 59 times it was asked. AUTO-F1
kills that class of waste at the cheapest possible point: `ArchProbe`
remembers every ``(op, arg)`` that missed this run, and a repeated ask for
the same pair is answered from the memo — instantly, with no bridge
lookup, and with a digest line that escalates with the repetition count.

  AC-F1-1  A repeated miss for the same (op, arg) performs no bridge lookup.
  AC-F1-2  The memo message names the repetition count.
  AC-F1-3  The memo is scoped to the run: a fresh ArchProbe starts empty.
  AC-F1-4  reset() does NOT clear it — that is the entire point.
  AC-F1-5  Memo hits count as misses in by_op, exactly like a real miss.
  AC-F1-6  A memo-hit counter reaches the trace (ArchProbe.last_memo_hits),
           so the saving is visible, not merely asserted.
  AC-F1-7  A hit is never memoised — only misses.
  AC-F1-8  The memo is bounded (probe_memo_max_entries, default 200) and
           evicts oldest-first.

Mutation checks this file also happens to cover: clearing the memo in
reset(), memoising a hit, and counting a memo hit as a hit would all fail
one of the tests below.
"""

from __future__ import annotations

import pytest

from tools.auto.arch_probe import ArchProbe, ProbeOp


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    """Counting double — only `usable`, `pull_symbol` and `module_symbols`
    matter, and every call is recorded so a test can assert the bridge was
    (not) touched. Mirrors tests/test_auto_p1_probe_parser.py::_FakeBridge,
    with `module_symbols` added for the op-scoping test below."""

    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}
        self.calls: list[str] = []
        self.module_calls: list[str] = []

    def pull_symbol(self, name: str) -> str:
        self.calls.append(name)
        return self._answers.get(name, "")

    def module_symbols(self, ref: str) -> str:
        self.module_calls.append(ref)
        return self._answers.get(("module", ref), "")


def _ops(*names: str) -> list[ProbeOp]:
    return [ProbeOp("facts", n) for n in names]


# ─────────────────────────────────────────────────────────────────────────────
# The memo
# ─────────────────────────────────────────────────────────────────────────────

class TestMissMemo:

    def test_repeated_miss_skips_the_bridge(self) -> None:
        """AC-F1-1: the property the whole ticket exists for. `retry` misses
        once for real; every later ask must cost nothing."""
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        probe.execute(_ops("retry"))
        probe.execute(_ops("retry"))

        assert bridge.calls == ["retry"], (
            "only the first ask should ever reach the bridge"
        )

    def test_memo_message_names_the_repeat_count(self) -> None:
        """AC-F1-2: the digest must say THIS SPECIFIC name is dead, with a
        count that climbs — not repeat the same silent (not found) the model
        has already ignored. Paired with a resolvable name each round so the
        digest is not suppressed by the all-miss empty-round rule."""
        bridge = _FakeBridge({"good": "module: x\nsignature: good()"})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))  # seeds the memo — no visible digest
        out2 = probe.execute(_ops("good", "retry"))
        out3 = probe.execute(_ops("good", "retry"))

        assert "already looked up 2 times" in out2
        assert "already looked up 3 times" in out3
        assert "retry` is not a symbol in this repository" in out2

    def test_reset_preserves_memo_but_a_new_instance_does_not(self) -> None:
        """AC-F1-3 + AC-F1-4, and the mutation check for each: clearing the
        memo in reset() and starting a fresh instance non-empty would both
        show up here as an extra bridge call in the wrong place."""
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        probe.reset()
        probe.execute(_ops("retry"))
        assert bridge.calls == ["retry"], (
            "AC-F1-4: reset() must not clear the memo"
        )

        fresh = ArchProbe(bridge)
        fresh.execute(_ops("retry"))
        assert bridge.calls == ["retry", "retry"], (
            "AC-F1-3: a fresh ArchProbe must start with an empty memo"
        )

    def test_memo_hits_count_as_misses_not_hits(self) -> None:
        """AC-F1-5 / AC-F1-6, and the mutation check "count memo hits as
        hits": a memo hit must land in last_misses and last_by_op's miss
        slot, never the hit slot, and must be visible via last_memo_hits."""
        bridge = _FakeBridge({"good": "module: x\nsignature: good()"})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        probe.execute(_ops("good", "retry"))

        assert probe.last_hits == 1
        assert probe.last_misses == 1
        assert probe.last_by_op == {"facts": [1, 1]}
        assert probe.last_memo_hits == 1

    def test_a_hit_is_never_memoised(self) -> None:
        """AC-F1-7, and the mutation check "memoise hits as well as misses":
        a symbol that resolves must be looked up fresh every time — only an
        absence is worth remembering."""
        bridge = _FakeBridge({"retry": "module: x\nsignature: retry()"})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        probe.execute(_ops("retry"))

        assert bridge.calls == ["retry", "retry"]
        assert probe.last_hits == 1

    def test_memo_evicts_oldest_first_at_the_cap(self) -> None:
        """AC-F1-8: bounded, oldest-first, so a pathological run cannot grow
        the memo without limit."""
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge, memo_max_entries=2)

        probe.execute(_ops("a"))
        probe.execute(_ops("b"))
        probe.execute(_ops("c"))  # memo is full — evicts "a", the oldest
        probe.execute(_ops("a"))  # "a" was evicted — a real lookup again

        assert bridge.calls == ["a", "b", "c", "a"]

    def test_memo_is_scoped_by_op_not_just_arg(self) -> None:
        """A `facts` miss must not silently answer `module` for the same
        name — the memo key is (op, arg), not arg alone. This is the test
        plan's own example: a name that misses under `facts` must still get
        a real `module` lookup."""
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        probe.execute([ProbeOp("module", "retry")])

        assert bridge.calls == ["retry"]
        assert bridge.module_calls == ["retry"], (
            "a facts miss must not memo-block a module lookup of the same name"
        )

    # ── AUTO-F1 follow-up: scope and the solo-repeat gap ───────────────────
    #
    # A run trace showed the memo firing on a `read` miss and rendering
    # "`tools/auto/inner_loop.py:254-350` is not a symbol in this
    # repository" — the epic's non-goals rule out `module`/`read`
    # ("No change to `module` or `read`. They work."), and a path is not
    # a symbol regardless. The two tests below pin that down: `module`
    # and `read` misses are never memoised, so a message written for
    # `facts` can never be attached to either.

    def test_module_misses_are_never_memoised(self) -> None:
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "nope.py")])
        probe.execute([ProbeOp("module", "nope.py")])

        assert bridge.module_calls == ["nope.py", "nope.py"], (
            "a module miss must reach the bridge every time — module is "
            "out of scope for this story"
        )

    def test_read_misses_are_never_memoised_or_mislabelled(self) -> None:
        bridge = _FakeBridge({"good": "module: x\nsignature: good()"})
        probe = ArchProbe(bridge, base_dir="/tmp")

        probe.execute([ProbeOp("read", "nope.py:1-10"), ProbeOp("facts", "good")])
        out = probe.execute(
            [ProbeOp("read", "nope.py:1-10"), ProbeOp("facts", "good")]
        )

        assert "is not a symbol in this repository" not in out, (
            "a `read` miss must never get the facts-shaped memo message — "
            "a path is not a symbol"
        )

    def test_solo_repeat_reaches_the_digest(self) -> None:
        """AC-F1-2, end to end: a repeat asked ALONE — the common real-run
        shape for `facts retry` — must not be swallowed by the all-miss-
        round rule, or the escalating message it exists to deliver never
        reaches the model. Before this follow-up, every solo repeat came
        back as an empty digest."""
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute(_ops("retry"))
        out = probe.execute(_ops("retry"))

        assert "already looked up 2 times this run" in out, (
            "a solo repeat must still surface the escalating memo message"
        )
