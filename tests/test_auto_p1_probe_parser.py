"""tests/test_auto_p1_probe_parser.py — AUTO-P1: the ARCH_PROBE protocol
parser and the ArchProbe read-only executor.

Pure unit tests. No LLM, no network, no filesystem — the CollectBridge the
executor reads from is a hand-rolled double, because what is under test is
the parser's strictness and the executor's budget arithmetic, not collect.

Parser (``extract_probe_request``)
  AC-P1P-1   A well-formed request yields its ops, in order.
  AC-P1P-2   Absent prefix, empty text and None yield [].
  AC-P1P-3   An op outside allowed_ops is dropped; a request made ONLY of
             such ops yields [] — i.e. "not a probe", not a half-honoured one.
  AC-P1P-4   An op with no argument is dropped.
  AC-P1P-5   Duplicates collapse, order preserved.
  AC-P1P-6   More than _MAX_OPS ops are capped.
  AC-P1P-7   Case and surrounding whitespace/punctuation are tolerated.
  AC-P1P-8   The prefix is recognised on its own line anywhere in the reply,
             including after a <think> preamble.
  AC-P1P-9   An empty allowed_ops list disables parsing entirely.
  AC-P1P-10  An arg containing a space survives the first-whitespace split
             (forward-compat with Phase 1's "read <path> <range>").

Executor (``ArchProbe``)
  AC-P1E-1   Not usable without a bridge, or with an unusable bridge.
  AC-P1E-2   A hit produces a "## Probe results" digest naming the op.
  AC-P1E-3   A miss produces "(not found)" rather than an exception or a
             silently dropped op — the model must be able to see its guess
             was wrong and stop asking.
  AC-P1E-4   An oversized result is truncated with a visible notice.
  AC-P1E-5   The total-chars budget stops further ops mid-request.
  AC-P1E-6   A bridge that raises is contained; other ops still run.
  AC-P1E-7   An op with no executor degrades to "(not found)".
  AC-P1E-8   An empty op list, and an all-miss request, are distinguishable:
             the former returns "", the latter a digest of "(not found)".
"""

from __future__ import annotations

import pytest

from tools.auto.arch_probe import (
    ArchProbe,
    ProbeOp,
    PROBE_PREFIX,
    _MAX_OPS,
    extract_probe_request,
)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractProbeRequest:

    def test_well_formed_request(self) -> None:
        """AC-P1P-1"""
        out = extract_probe_request("ARCH_PROBE: facts alpha, facts beta")
        assert out == [ProbeOp("facts", "alpha"), ProbeOp("facts", "beta")]

    @pytest.mark.parametrize("text", ["", None, "here is your JSON: []", "   "])
    def test_no_probe(self, text) -> None:
        """AC-P1P-2"""
        assert extract_probe_request(text) == []

    def test_unknown_op_is_dropped(self) -> None:
        """AC-P1P-3: a mixed request keeps only the allowed ops…"""
        out = extract_probe_request("ARCH_PROBE: facts alpha, bash rm -rf /")
        assert out == [ProbeOp("facts", "alpha")]

    def test_request_of_only_unknown_ops_is_not_a_probe(self) -> None:
        """AC-P1P-3: …and a request made entirely of them is not a probe at
        all, so the caller routes it to normal unparseable handling instead
        of spending a round on nothing."""
        assert extract_probe_request("ARCH_PROBE: read /etc/passwd") == []
        assert extract_probe_request("ARCH_PROBE: refs alpha") == []

    def test_op_without_argument_is_dropped(self) -> None:
        """AC-P1P-4"""
        assert extract_probe_request("ARCH_PROBE: facts") == []
        assert extract_probe_request("ARCH_PROBE: facts, facts beta") == [
            ProbeOp("facts", "beta")
        ]

    def test_duplicates_collapse_order_preserved(self) -> None:
        """AC-P1P-5"""
        out = extract_probe_request(
            "ARCH_PROBE: facts beta, facts alpha, facts beta"
        )
        assert out == [ProbeOp("facts", "beta"), ProbeOp("facts", "alpha")]

    def test_op_cap(self) -> None:
        """AC-P1P-6: a model asking for 20 symbols has not understood the
        question; answer the first _MAX_OPS and let it re-ask."""
        many = ", ".join(f"facts sym{i}" for i in range(20))
        assert len(extract_probe_request(f"ARCH_PROBE: {many}")) == _MAX_OPS

    def test_case_and_punctuation_tolerance(self) -> None:
        """AC-P1P-7"""
        out = extract_probe_request('  arch_probe:  FACTS   "alpha".  ')
        assert out == [ProbeOp("facts", "alpha")]

    def test_prefix_after_a_think_preamble(self) -> None:
        """AC-P1P-8: reasoning models emit a preamble before the payload."""
        text = "Let me consider the cluster...\nI need more.\nARCH_PROBE: facts alpha"
        assert extract_probe_request(text) == [ProbeOp("facts", "alpha")]

    def test_empty_allowed_ops_disables_parsing(self) -> None:
        """AC-P1P-9: a misconfigured probe_allowed_ops fails closed."""
        assert extract_probe_request(
            "ARCH_PROBE: facts alpha", allowed_ops=()
        ) == []

    def test_arg_with_space_survives(self) -> None:
        """AC-P1P-10: the split is on the FIRST whitespace run only, so a
        Phase 1 'read <path> <range>' arg reaches its executor intact."""
        out = extract_probe_request(
            "ARCH_PROBE: read tools/x.py 10:20", allowed_ops=("read",)
        )
        assert out == [ProbeOp("read", "tools/x.py 10:20")]

    def test_prefix_constant_is_what_the_parser_matches(self) -> None:
        assert extract_probe_request(f"{PROBE_PREFIX} facts alpha") == [
            ProbeOp("facts", "alpha")
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    """Stands in for CollectBridge — only `usable` and `pull_symbol` matter."""

    def __init__(self, answers: dict, *, usable: bool = True, raises=()):
        self._answers = answers
        self.usable = usable
        self._raises = set(raises)
        self.calls: list[str] = []

    def pull_symbol(self, name: str) -> str:
        self.calls.append(name)
        if name in self._raises:
            raise RuntimeError(f"bridge exploded on {name}")
        return self._answers.get(name, "")


def _ops(*names: str) -> list[ProbeOp]:
    return [ProbeOp("facts", n) for n in names]


class TestArchProbeExecutor:

    def test_unusable_without_bridge(self) -> None:
        """AC-P1E-1"""
        assert ArchProbe(None).usable is False
        assert ArchProbe(_FakeBridge({}, usable=False)).usable is False
        assert ArchProbe(_FakeBridge({})).usable is True

    def test_unusable_probe_returns_empty_digest(self) -> None:
        """AC-P1E-1: and an empty digest is the caller's signal to force a
        final plan call rather than re-ask into the void."""
        assert ArchProbe(None).execute(_ops("alpha")) == ""

    def test_hit_produces_digest(self) -> None:
        """AC-P1E-2"""
        probe = ArchProbe(_FakeBridge({"alpha": "module: x\nsignature: f()"}))
        out = probe.execute(_ops("alpha"))
        assert out.startswith("## Probe results")
        assert "### facts alpha" in out
        assert "signature: f()" in out
        assert probe.chars_used > 0

    def test_miss_is_reported_not_dropped(self) -> None:
        """AC-P1E-3: silence would leave the model guessing that its request
        was simply ignored, and asking again next round."""
        out = ArchProbe(_FakeBridge({})).execute(_ops("nope"))
        assert "### facts nope" in out
        assert "(not found)" in out

    def test_oversized_result_is_truncated_visibly(self) -> None:
        """AC-P1E-4"""
        probe = ArchProbe(_FakeBridge({"big": "x" * 5000}), max_chars=500)
        out = probe.execute(_ops("big"))
        assert "truncated" in out
        assert len(out) < 1200

    def test_total_budget_stops_remaining_ops(self) -> None:
        """AC-P1E-5: the total cap is what actually protects the context
        window — the per-op cap alone would let eight hot symbols through."""
        bridge = _FakeBridge({n: "y" * 400 for n in ("a", "b", "c", "d")})
        probe = ArchProbe(bridge, max_chars=400, max_total_chars=500)
        probe.execute(_ops("a", "b", "c", "d"))
        assert bridge.calls == ["a", "b"], "must stop once the budget is spent"
        assert probe.budget_exhausted is True

    def test_raising_bridge_is_contained(self) -> None:
        """AC-P1E-6: one bad symbol must not cost the whole round."""
        bridge = _FakeBridge({"good": "module: ok"}, raises=("bad",))
        out = ArchProbe(bridge).execute(_ops("bad", "good"))
        assert bridge.calls == ["bad", "good"], "the raise must not abort the loop"
        assert "### facts good" in out and "module: ok" in out
        assert "### facts bad" not in out, "a raising op is skipped, not reported"

    def test_unknown_op_degrades_to_not_found(self) -> None:
        """AC-P1E-7: defensive — unreachable while the parser enforces the
        allow-list, but a future op added to the parser and not here must not
        raise mid-batch."""
        out = ArchProbe(_FakeBridge({})).execute([ProbeOp("refs", "alpha")])
        assert "(not found)" in out

    def test_empty_and_all_miss_are_distinguishable(self) -> None:
        """AC-P1E-8: '' means 'stop, nothing to add'; an all-miss digest is
        real information and must NOT collapse to ''."""
        probe = ArchProbe(_FakeBridge({}))
        assert probe.execute([]) == ""
        assert probe.execute(_ops("nope")) != ""
