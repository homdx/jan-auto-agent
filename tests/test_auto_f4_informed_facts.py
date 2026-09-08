"""tests/test_auto_f4_informed_facts.py — AUTO-F4: teach and measure the
`module` -> `facts` sequence.

`PROBE_INSTRUCTIONS` forbade guessing at a symbol name but never told the
model what to do instead. AUTO-F4 adds the missing half of the instruction
("ask `module <path>` first, then `facts` for one of the names it
returned") and, because instructions in this epic have repeatedly been
ignored (see AUTO-F1's own trace evidence), measures whether it is actually
followed rather than assuming it is: a `facts` ask is "informed" when its
symbol was named in a `module` hit earlier in the same batch, "blind"
otherwise.

  AC-F4-1  The instructions state the sequence, not only the prohibition.
  AC-F4-2  `probe_result` carries `informed_facts` / `blind_facts` counts.
  AC-F4-3  `analyze_logs` reports the split when `facts` asks exist.
  AC-F4-4  Hit rate is reported separately for informed and blind lookups.
  AC-F4-5  No behaviour change: a blind `facts` still runs.

This story measures; it does not restrict — every test below confirms a
blind ask is still answered exactly as before, and only the bookkeeping
around it is new.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyze_logs import analyze, render_run_summary
from tools.auto.arch_probe import ArchProbe, PROBE_INSTRUCTIONS, ProbeOp


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    """Same shape as test_auto_f1_miss_memo.py's double — `module_symbols`
    returns text in collect's real format (two-space-indented name per
    line) so `_learn_module_names`'s regex has something realistic to
    match, not a hand-tuned fixture string."""

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


def _module_block(path: str, *names: str) -> str:
    """A `module` hit in collect's real format — see
    CollectBridge._format_module_block."""
    lines = [f"module: {path}"]
    for n in names:
        lines.append(f"  {n}(...)  :1 — does a thing")
    return "\n".join(lines)


def _facts_ops(*names: str) -> list[ProbeOp]:
    return [ProbeOp("facts", n) for n in names]


# ─────────────────────────────────────────────────────────────────────────────
# AC-F4-1: instructions
# ─────────────────────────────────────────────────────────────────────────────

class TestInstructions:

    def test_instructions_state_the_sequence_not_only_the_prohibition(self) -> None:
        assert "module <path>` first" in PROBE_INSTRUCTIONS
        assert "then ask `facts`" in PROBE_INSTRUCTIONS


# ─────────────────────────────────────────────────────────────────────────────
# AC-F4-4 / AC-F4-5: informed vs. blind, at the ArchProbe level
# ─────────────────────────────────────────────────────────────────────────────

class TestInformedVsBlind:

    def test_facts_ask_after_a_module_hit_counts_as_informed(self) -> None:
        """A name that a `module` result actually listed, asked afterwards,
        must be tallied informed — and, since it resolves, an informed
        hit."""
        bridge = _FakeBridge({
            ("module", "tools/x.py"): _module_block("tools/x.py", "helper"),
            "helper": "module: tools/x.py\nsignature: helper()",
        })
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/x.py")])
        probe.execute(_facts_ops("helper"))

        assert probe.informed_facts == (1, 1), "one informed ask, one informed hit"
        assert probe.blind_facts == (0, 0)

    def test_facts_ask_with_no_prior_module_hit_counts_as_blind(self) -> None:
        """AC-F4-5: the ask still runs exactly as before — this is a
        measurement, not a restriction — it is simply counted as blind."""
        bridge = _FakeBridge({"helper": "module: tools/x.py\nsignature: helper()"})
        probe = ArchProbe(bridge)

        probe.execute(_facts_ops("helper"))

        assert bridge.calls == ["helper"], "a blind ask must still run"
        assert probe.blind_facts == (1, 1)
        assert probe.informed_facts == (0, 0)

    def test_a_missed_blind_ask_is_counted_but_not_as_a_hit(self) -> None:
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute(_facts_ops("retry"))

        assert probe.blind_facts == (0, 1), "asked once, hit zero times"

    def test_only_names_module_actually_returned_count_as_informed(self) -> None:
        """A name NOT in the module's inventory, asked after an unrelated
        module hit, is still a guess — informed means "this exact name was
        shown", not "some module was looked up this batch"."""
        bridge = _FakeBridge({
            ("module", "tools/x.py"): _module_block("tools/x.py", "helper"),
        })
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/x.py")])
        probe.execute(_facts_ops("retry"))  # not one of the names shown

        assert probe.blind_facts == (0, 1)
        assert probe.informed_facts == (0, 0)

    def test_a_module_miss_teaches_nothing(self) -> None:
        bridge = _FakeBridge({})
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/nope.py")])
        probe.execute(_facts_ops("helper"))

        assert probe.blind_facts == (0, 1)
        assert probe.informed_facts == (0, 0)

    def test_a_name_truncated_out_of_the_digest_is_not_learned(self) -> None:
        """A `module` hit's raw result can be longer than the per-op char
        cap (`ArchProbe._cap`) and get hard-truncated before it ever reaches
        the model — `_format_block` sends only the capped text onward. A
        name that appears solely past that truncation point was never
        actually shown, so it must not be learned as "informed": counting
        it would credit the model for reading a name that was cut from its
        own digest."""
        padding = "x" * 300
        body = (
            "module: tools/x.py\n"
            "  helper(...)  :1 — does a thing\n"
            f"  # {padding}\n"
            "  faraway(...)  :2 — never actually shown\n"
        )
        assert "faraway" not in body[:200], (
            "fixture must place the name past the cap for this test to mean "
            "anything"
        )
        bridge = _FakeBridge({
            ("module", "tools/x.py"): body,
            "faraway": "module: tools/x.py\nsignature: faraway()",
        })
        probe = ArchProbe(bridge, max_chars=200)  # 200 is ArchProbe's floor

        probe.execute([ProbeOp("module", "tools/x.py")])
        probe.execute(_facts_ops("faraway"))

        assert probe.blind_facts == (1, 1), (
            "resolves fine (it is a real symbol) but must count as blind: "
            "the model never actually saw it in the truncated digest"
        )
        assert probe.informed_facts == (0, 0)


# ─────────────────────────────────────────────────────────────────────────────
# AC-F4-2 test plan item: "split survives across rounds within a batch and
# resets between batches"
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchScoping:

    def test_split_survives_across_rounds_within_a_batch(self) -> None:
        bridge = _FakeBridge({
            ("module", "tools/x.py"): _module_block("tools/x.py", "helper"),
            "helper": "module: tools/x.py\nsignature: helper()",
        })
        probe = ArchProbe(bridge)

        # Round 1: learn "helper" via module.
        probe.execute([ProbeOp("module", "tools/x.py")])
        # Round 2: one informed ask, one blind ask.
        probe.execute(_facts_ops("helper"))
        # Round 3 (still the same batch — no reset() in between): another
        # informed ask must still be recognised.
        probe.execute(_facts_ops("helper", "retry"))

        assert probe.informed_facts == (2, 2), "both 'helper' asks across rounds"
        assert probe.blind_facts == (0, 1), "the one 'retry' ask"

    def test_reset_clears_the_split_and_the_learned_names(self) -> None:
        bridge = _FakeBridge({
            ("module", "tools/x.py"): _module_block("tools/x.py", "helper"),
            "helper": "module: tools/x.py\nsignature: helper()",
        })
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/x.py")])
        probe.execute(_facts_ops("helper"))
        assert probe.informed_facts == (1, 1)

        probe.reset()
        assert probe.informed_facts == (0, 0)
        assert probe.blind_facts == (0, 0)

        # "helper" was learned in the PREVIOUS batch — a fresh batch must
        # not remember it, or "resets between batches" is not actually true.
        probe.execute(_facts_ops("helper"))
        assert probe.blind_facts == (1, 1), (
            "a name learned in an earlier batch must not carry over"
        )
        assert probe.informed_facts == (0, 0)

    def test_facts_informed_str_is_empty_with_no_facts_asks(self) -> None:
        bridge = _FakeBridge({("module", "tools/x.py"): _module_block("tools/x.py", "helper")})
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/x.py")])

        assert probe.facts_informed_str() == ""

    def test_facts_informed_str_reports_both_sides(self) -> None:
        bridge = _FakeBridge({
            ("module", "tools/x.py"): _module_block("tools/x.py", "helper"),
            "helper": "module: tools/x.py\nsignature: helper()",
        })
        probe = ArchProbe(bridge)

        probe.execute([ProbeOp("module", "tools/x.py")])
        probe.execute(_facts_ops("helper", "retry"))

        assert probe.facts_informed_str() == "informed=1/1 blind=0/1"


# ─────────────────────────────────────────────────────────────────────────────
# AC-F4-3 / AC-F4-4: analyze_logs renders the split, and omits it cleanly
# ─────────────────────────────────────────────────────────────────────────────

def _evt(kind: str, params: dict | None = None, run_id: str = "run1", **kw) -> dict:
    return {
        "run_id": run_id,
        "kind": kind,
        "source": kw.get("source", "architect"),
        "target": kw.get("target", "probe"),
        "ts": "2024-01-01T00:00:00",
        "params": params or {},
        "content": kw.get("content", ""),
    }


def _capture_summary(run: dict) -> str:
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        render_run_summary(run)
    finally:
        sys.stdout = old_stdout
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())


class TestAnalyzeLogsRendering:

    def test_renders_informed_and_blind_hit_rates(self) -> None:
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}, source="architect", target="probe"),
            _evt(
                "probe_result",
                {
                    "cluster": "c1", "round": 1, "ops": 1,
                    "hits": 1, "misses": 0, "by_op": "facts=1/0",
                    "chars_used": 10, "run_chars_used": 10,
                    "informed_facts": "1/1", "blind_facts": "0/0",
                },
                source="probe", target="architect",
            ),
        ]
        runs = analyze(events)
        text = _capture_summary(runs["run1"])
        assert "facts sequencing" in text
        assert "informed" in text and "100% (1/1)" in text

    def test_omits_the_line_when_no_facts_asks_exist(self) -> None:
        """AC-F4-3: a run that only ever asked `module` (informed_facts and
        blind_facts both 0/0) must not print a misleading 0% line."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt(
                "probe_result",
                {
                    "cluster": "c1", "round": 1, "ops": 1,
                    "hits": 1, "misses": 0, "by_op": "module=1/0",
                    "chars_used": 10, "run_chars_used": 10,
                    "informed_facts": "0/0", "blind_facts": "0/0",
                },
                source="probe", target="architect",
            ),
        ]
        runs = analyze(events)
        text = _capture_summary(runs["run1"])
        assert "facts sequencing" not in text

    def test_pre_auto_f4_trace_renders_without_the_line_or_crashing(self) -> None:
        """A trace from before this ticket carries no informed_facts /
        blind_facts params at all — must not crash and must not print the
        new line."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt(
                "probe_result",
                {
                    "cluster": "c1", "round": 1, "ops": 1,
                    "hits": 1, "misses": 0, "by_op": "facts=1/0",
                    "chars_used": 10, "run_chars_used": 10,
                },
                source="probe", target="architect",
            ),
        ]
        runs = analyze(events)
        text = _capture_summary(runs["run1"])
        assert "facts sequencing" not in text

    def test_sums_across_multiple_clusters_using_only_the_latest_per_cluster(self) -> None:
        """Each probe_result restates a growing per-batch total — summing
        every event would double-count a batch's early rounds. Only the
        LAST event per cluster should contribute, then totals are summed
        across distinct clusters."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt("probe_result", {
                "cluster": "c1", "round": 1, "ops": 1, "hits": 1, "misses": 0,
                "by_op": "facts=1/0", "chars_used": 10, "run_chars_used": 10,
                "informed_facts": "1/1", "blind_facts": "0/0",
            }, source="probe", target="architect"),
            # Round 2 of the SAME cluster/batch — a growing cumulative total.
            _evt("probe_result", {
                "cluster": "c1", "round": 2, "ops": 1, "hits": 1, "misses": 0,
                "by_op": "facts=1/0", "chars_used": 20, "run_chars_used": 20,
                "informed_facts": "1/1", "blind_facts": "1/1",
            }, source="probe", target="architect"),
            # A second, independent cluster.
            _evt("probe_request", {"cluster": "c2", "ops": 1}),
            _evt("probe_result", {
                "cluster": "c2", "round": 1, "ops": 1, "hits": 0, "misses": 1,
                "by_op": "facts=0/1", "chars_used": 10, "run_chars_used": 10,
                "informed_facts": "0/0", "blind_facts": "0/1",
            }, source="probe", target="architect"),
        ]
        runs = analyze(events)
        # c1's final (latest-round) total: informed 1/1, blind 1/1.
        # c2's total: blind 0/1. Correct grand total: informed 1/1,
        # blind 1/2 — NOT c1's 1/1 blind plus a double-counted round 1.
        text = _capture_summary(runs["run1"])
        assert "informed" in text and "100% (1/1)" in text
        assert "blind" in text and "50% (1/2)" in text
