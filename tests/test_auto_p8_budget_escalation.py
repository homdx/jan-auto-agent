"""tests/test_auto_p8_budget_escalation.py — AUTO-P8: stop losing probe
requests to the digest cap.

Once `module` (AUTO-P5) and `read` (AUTO-P7) landed, the per-batch digest cap
became the binding constraint. In the last measured run **15 of 23 declines
were `digest_budget`**, and the log shows what that cost:

    ArchProbe: total digest budget (10000 chars) reached — dropping remaining
    op(s) starting at read main.py:100-200.

The Architect had asked for three ops. Two were served, the third was
dropped, and **nothing in the digest said so**. From the model's side an
unanswered lookup is indistinguishable from one that came back empty — a
completely different fact — so it planned as though `main.py:100-200` had
been checked. Then the harness forced a final plan call and threw the
request away entirely.

Two fixes, and they are independent:

1. **Say what was dropped.** A `[NOT ANSWERED — ...]` block naming the ops
   that never ran, and stating explicitly that they are *not* known to be
   absent.
2. **Escalate before giving up.** A bounded ladder of re-asks with a wider
   cap, mirroring AUTO-H5-ESCALATE-1's shape. Step 1 widens the cap at
   temperature 0 so the model re-issues the *same* request and the new room
   serves the dropped ops; step 2 keeps the room and raises temperature,
   because a byte-identical loop means determinism is the problem, not
   budget; step 3 returns to temperature 0 with more room still.

  AC-P8-1   Dropped ops are recorded, not merely logged.
  AC-P8-2   The digest names them and says they are not known to be absent.
  AC-P8-3   Nothing dropped ⇒ no notice (it must not become boilerplate).
  AC-P8-4   raise_budget multiplies the CONFIGURED cap, so 2x then 4x — not
            2x then 8x.
  AC-P8-5   reset() restores the configured cap between batches.
  AC-P8-6   A budget-truncated round escalates instead of forcing.
  AC-P8-7   The ladder's temperatures reach the LLM call, in order.
  AC-P8-8   The ladder is bounded; after it, the batch forces as before.
  AC-P8-9   probe_budget_escalations = 0 restores pre-AUTO-P8 behaviour.
  AC-P8-10  A `probe_escalated` trace event is emitted per step.
  AC-P8-11  Escalation does not fire for round_cap / unresolved / repeat.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.agent_trace import tracer
from tools.auto import arch_probe
from tools.auto.arch_probe import (
    ArchProbe,
    ProbeOp,
    _BUDGET_ESCALATION_LADDER,
)
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


class _Bridge:
    """Every symbol resolves, and to a fixed-size block — so the number of
    ops that fit in a given cap is arithmetic, not luck."""

    usable = True

    def __init__(self, size: int = 400):
        self._size = size

    def pull_symbol(self, name: str) -> str:
        return f"symbol: {name}\n" + ("x" * self._size)

    def module_symbols(self, ref: str) -> str:
        return f"module: {ref}\n" + ("y" * self._size)


def _ops(*names: str) -> list[ProbeOp]:
    return [ProbeOp("facts", n) for n in names]


# ─────────────────────────────────────────────────────────────────────────────
# Dropped-op reporting
# ─────────────────────────────────────────────────────────────────────────────

class TestDroppedOps:

    def test_dropped_ops_are_recorded(self) -> None:
        """AC-P8-1"""
        # Arithmetic, not luck: each block is ~420 chars, the cap is 600, so
        # op 1 fits, op 2 takes the running total past the cap, and op 3 is
        # dropped. The dropped list must name it exactly.
        p = ArchProbe(_Bridge(400), max_chars=600, max_total_chars=600)
        p.execute(_ops("a", "b", "c"))
        assert [str(o) for o in p.last_dropped] == ["facts c"]

    def test_digest_names_them_and_does_not_call_them_absent(self) -> None:
        """AC-P8-2: the distinction that was being lost. "not answered" and
        "came back empty" are different facts, and the model acted on the
        wrong one."""
        p = ArchProbe(_Bridge(400), max_chars=600, max_total_chars=600)
        out = p.execute(_ops("a", "b", "c"))
        assert "NOT ANSWERED" in out
        assert "facts c" in out
        assert "NOT known to be absent" in out

    def test_no_notice_when_nothing_dropped(self) -> None:
        """AC-P8-3: a notice on every digest would be noise, and noise is
        how the original log line got ignored."""
        p = ArchProbe(_Bridge(100), max_chars=4000, max_total_chars=8000)
        out = p.execute(_ops("a", "b"))
        assert out and "NOT ANSWERED" not in out
        assert p.last_dropped == []

    def test_dropped_list_resets_between_calls(self) -> None:
        p = ArchProbe(_Bridge(400), max_chars=600, max_total_chars=600)
        p.execute(_ops("a", "b", "c"))
        assert p.last_dropped
        p.reset()
        p.execute(_ops("a"))
        assert p.last_dropped == []


class TestBudgetArithmetic:

    def test_raise_budget_multiplies_the_configured_cap(self) -> None:
        """AC-P8-4: multiplying the *current* cap would compound to 2x, 4x,
        16x across the ladder instead of the 2x, 2x, 4x it declares."""
        p = ArchProbe(_Bridge(), max_chars=500, max_total_chars=1000)
        assert p.raise_budget(2.0) == 2000
        assert p.raise_budget(2.0) == 2000
        assert p.raise_budget(4.0) == 4000

    def test_raise_budget_never_drops_below_per_op_cap(self) -> None:
        p = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=2000)
        assert p.raise_budget(0.1) == 2000

    def test_reset_restores_the_configured_cap(self) -> None:
        """AC-P8-5: an escalation is for one batch. Carrying a raised cap
        into the next one would silently re-tune the profile."""
        p = ArchProbe(_Bridge(), max_chars=500, max_total_chars=1000)
        p.raise_budget(4.0)
        p.reset()
        p.execute(_ops("a"))
        assert p._max_total_chars == 1000


# ─────────────────────────────────────────────────────────────────────────────
# The ladder, end to end
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {"temperature": "0.2", "max_tokens": "512", "probe_enabled": "true",
            "probe_max_rounds": "5", "probe_allowed_ops": "facts, module",
            "probe_max_chars": "600", "probe_max_total_chars": "600",
            "retry_delays_sec": ""}
    arch.update(over)
    c = configparser.ConfigParser()
    c.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": arch, "loop": {"timeout_seconds": "10"},
    })
    return c


def _reviewer(cfg, size: int = 400) -> ClusterReviewer:
    r = ClusterReviewer(config=cfg, base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    r._probe = ArchProbe(
        _Bridge(size),
        max_chars=cfg.getint("architect", "probe_max_chars"),
        max_total_chars=cfg.getint("architect", "probe_max_total_chars"),
    )
    return r


@pytest.fixture()
def cluster_and_base(tmp_path: Path):
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    return RepoCluster(name="agents", patterns=["tools/*"],
                       files=["tools/example.py"]), tmp_path


def _good(title: str) -> str:
    return json.dumps([{
        "title": title, "instruction": "Do it.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {"file": "tools/example.py", "symbol": "fn",
                           "line_start": 1, "line_end": 1},
    }])


_BIG = "ARCH_PROBE: facts a, facts b, facts c"


def _temps(mock_llm) -> list:
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        out.append(payload.get("temperature"))
    return out


class TestEscalationLadder:

    def test_budget_truncation_escalates_instead_of_forcing(
        self, cluster_and_base
    ) -> None:
        """AC-P8-6: the headline. Before AUTO-P8 this batch spent one call,
        dropped two ops and forced. Now it re-asks with more room."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_budget_escalations="2"))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_BIG, _BIG, _good("Grounded")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 3
        assert [x.title for x in results] == ["Grounded"]

    def test_ladder_temperatures_reach_the_call_in_order(
        self, cluster_and_base
    ) -> None:
        """AC-P8-7: step 1 pins temperature 0 so the model re-issues the SAME
        request — a re-roll would ask something else and waste the room we
        just bought. Step 2 raises it precisely because a byte-identical
        loop means determinism is the problem."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_budget_escalations="2"))
        with patch(
            "tools.llm_stream.request_completion",
            # Enough stubborn rounds to drive BOTH ladder steps. One
            # escalation is often enough in practice — the wider cap serves
            # the dropped ops and the batch moves on — so a shorter script
            # would silently only exercise step 1.
            side_effect=[_BIG] * 6 + [_good("G")],
        ) as mock_llm:
            r.review_clusters([cluster], base_dir, goal="g")

        temps = _temps(mock_llm)
        # Asserted as an ordered subsequence rather than at fixed indices: the
        # first budget-truncated round is only detected on the NEXT pass of
        # the loop, so an ordinary re-ask sits between the initial call and
        # the first escalation. Pinning indices would encode that spacing and
        # break on any unrelated change to the loop.
        assert temps[0] == pytest.approx(0.2), "first call uses the configured temperature"
        ladder = [t for _, t in _BUDGET_ESCALATION_LADDER[:2]]
        seen, want = [], list(ladder)
        for t in temps[1:]:
            if want and t == pytest.approx(want[0]):
                seen.append(want.pop(0))
        assert seen == ladder, (
            f"ladder temperatures {ladder} must appear in order; saw {temps}"
        )
        assert ladder[0] != ladder[1], "the ladder must actually vary"

    def test_ladder_is_bounded_then_forces(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P8-8: unbounded escalation is a bill with no ceiling. After the
        cap the batch behaves exactly as it did before this ticket."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_budget_escalations="1"))
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=[_BIG] * 5 + [_good("Forced")],
            ) as mock_llm:
                r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        assert len([e for e in ev if e.get("kind") == "probe_escalated"]) == 1, (
            "probe_budget_escalations=1 must buy exactly one escalation"
        )
        reasons = [e["params"]["reason"]
                   for e in ev if e.get("kind") == "probe_declined"]
        assert "digest_budget" in reasons, (
            "after the ladder is spent the batch declines on budget exactly "
            "as it did before this ticket"
        )
        # The last decline is `post_forced`: this stub model probes on the
        # forced call too, which AUTO-P4b already handles. Asserting on the
        # LAST reason would be asserting about the stub, not about the ladder.
        assert reasons[0] == "digest_budget"

    def test_zero_escalations_restores_old_behaviour(
        self, cluster_and_base
    ) -> None:
        """AC-P8-9: the off switch, and the guarantee that this ticket adds
        no cost to anyone who does not want it."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_budget_escalations="0"))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_BIG, _good("Forced")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 2  # probe, then straight to forced
        assert [x.title for x in results] == ["Forced"]

    def test_escalation_event_carries_the_step_and_new_cap(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P8-10: without this the ladder is invisible to analyze_logs and
        its cost cannot be weighed against what it recovers."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_budget_escalations="1"))
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=[_BIG, _BIG, _good("F")],
            ):
                r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        e = next(x for x in ev if x.get("kind") == "probe_escalated")
        assert int(e["params"]["step"]) == 1
        assert int(e["params"]["new_cap"]) == 1200  # 600 configured x 2.0
        assert e["params"]["cluster"] == "agents"

    def test_round_cap_does_not_escalate(self, cluster_and_base) -> None:
        """AC-P8-11: escalation buys room. A round cap is not a room problem,
        and spending calls on it would be spending them on nothing."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(
            _cfg(probe_max_rounds="1", probe_budget_escalations="2",
                 probe_max_chars="4000", probe_max_total_chars="8000"),
            size=50,
        )
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: facts a", "ARCH_PROBE: facts a",
                         _good("Forced")],
        ) as mock_llm:
            r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 3  # probe, re-ask, forced — no extra
