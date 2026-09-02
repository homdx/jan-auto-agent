"""tests/test_auto_p10_escalation_widens.py — AUTO-P10: an escalation must
always widen the budget.

`raise_budget` multiplied the **configured** cap. That was right while every
batch started there, and became wrong the moment AUTO-P9 made batches start
at a *learned* cap instead.

The measured run shows it plainly. Configured cap 10 000, learned cap
16 624, first ladder rung 1.5x:

    escalation 1  ->  10 000 x 1.5  =  15 000     (the batch already had 16 624)

The escalation **shrank** the budget. And the trace records the consequence
with no ambiguity at all:

    escalations: 36    by step: {'1': 18, '2': 18}

Eighteen step-1 escalations, and all eighteen went on to need step 2 —
because step 1 could not possibly have helped. Every one of those was a
wasted architect call, spent making the problem slightly worse.

Neither the AUTO-P8 nor the AUTO-P9 suite caught this. AUTO-P8's
`test_raise_budget_multiplies_the_configured_cap` asserted the buggy
behaviour directly, and it was right to at the time; AUTO-P9 added seeding
without revisiting what seeding meant for the ladder. The tests below are
written against the invariant rather than the arithmetic, so the next change
to either half cannot reintroduce it.

  AC-P10-1  An escalation never returns a cap below the current one.
  AC-P10-2  An escalation never returns a cap below what the batch started
            with — the seeded case, which is the actual regression.
  AC-P10-3  Rungs compose from the batch floor, not multiplicatively.
  AC-P10-4  Unseeded behaviour is unchanged (AUTO-P8's contract).
  AC-P10-5  reset() re-establishes the floor for the new batch.
  AC-P10-6  End to end: a seeded batch's escalation is strictly wider.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.agent_trace import tracer
from tools.auto.arch_probe import (
    ArchProbe,
    ProbeOp,
    _BUDGET_ONLY_LADDER,
)
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


class _Bridge:
    usable = True

    def __init__(self, size: int = 400):
        self._size = size

    def pull_symbol(self, name: str) -> str:
        return f"symbol: {name}\n" + ("x" * self._size)

    def module_symbols(self, ref: str) -> str:
        return f"module: {ref}\n" + ("y" * self._size)


def _probe(costs=(), *, configured=10000, warmup=3, headroom=2.0) -> ArchProbe:
    p = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=configured)
    p.configure_learning(warmup=warmup, headroom=headroom, ceiling=0)
    p._round_costs = list(costs)
    return p


class TestEscalationAlwaysWidens:

    def test_the_exact_regression(self) -> None:
        """AC-P10-2. The numbers are the run's, not invented: configured
        10 000, median round cost 8 312, headroom 2.0 -> seeded 16 624, first
        rung 1.5x. The old code returned 15 000 here."""
        p = _probe([8000, 8312, 9000], configured=10000, headroom=2.0)
        p.reset()
        started_at = p._max_total_chars
        assert started_at == 16624, "seeding must be in play for this to mean anything"

        after = p.raise_budget(_BUDGET_ONLY_LADDER[0][0])  # 1.5
        assert after > started_at, (
            f"escalation shrank the budget: {started_at} -> {after}. An "
            f"escalation that narrows is worse than no escalation at all — "
            f"it costs a call AND removes room."
        )

    @pytest.mark.parametrize("factor", [f for f, _ in _BUDGET_ONLY_LADDER])
    def test_no_rung_ever_narrows(self, factor: float) -> None:
        """AC-P10-1: stated over the whole shipped ladder rather than one
        rung, so adding a rung cannot quietly reintroduce this."""
        p = _probe([8000, 8312, 9000], configured=10000)
        p.reset()
        before = p._max_total_chars
        assert p.raise_budget(factor) >= before

    def test_rungs_compose_from_the_floor_not_multiplicatively(self) -> None:
        """AC-P10-3: the property AUTO-P8 wanted, restated against the batch
        floor. 1.5x then 2.5x must be 1.5x and 2.5x of the start — not 1.5x
        then 3.75x."""
        p = _probe([8000, 8312, 9000], configured=10000)
        p.reset()
        floor = p._max_total_chars
        first = p.raise_budget(1.5)
        second = p.raise_budget(2.5)
        assert first == int(floor * 1.5)
        assert second == int(floor * 2.5)

    def test_unseeded_behaviour_is_unchanged(self) -> None:
        """AC-P10-4: while still learning, the batch starts at the configured
        cap and the ladder behaves exactly as AUTO-P8 shipped it."""
        p = _probe([], configured=10000)
        p.reset()
        assert p._max_total_chars == 10000
        assert p.raise_budget(2.0) == 20000
        assert p.raise_budget(2.0) == 20000   # same rung, same answer
        assert p.raise_budget(4.0) == 40000

    def test_reset_re_establishes_the_floor(self) -> None:
        """AC-P10-5: an escalation is scoped to one batch. The next batch must
        escalate from ITS start, not from a cap the previous batch raised."""
        p = _probe([8000, 8312, 9000], configured=10000)
        p.reset()
        p.raise_budget(2.5)
        raised = p._max_total_chars

        p.reset()
        assert p._max_total_chars == 16624, "new batch starts at the seeded cap"
        assert p._max_total_chars < raised
        assert p.raise_budget(1.5) == int(16624 * 1.5)


# ─────────────────────────────────────────────────────────────────────────────
# End to end
# ─────────────────────────────────────────────────────────────────────────────

def _cfg() -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": {"temperature": "0.2", "max_tokens": "512",
                      "probe_enabled": "true", "probe_max_rounds": "5",
                      "probe_allowed_ops": "facts",
                      "probe_max_chars": "2000", "probe_max_total_chars": "2000",
                      "probe_budget_escalations": "2",
                      "retry_delays_sec": ""},
        "loop": {"timeout_seconds": "10"},
    })
    return c


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


def test_seeded_escalation_is_strictly_wider(cluster_and_base, tmp_path) -> None:
    """AC-P10-6: read from the trace, because that is where the regression was
    visible in production and where anyone re-checking it will look."""
    cluster, base_dir = cluster_and_base
    r = ClusterReviewer(config=_cfg(), base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    # Blocks of ~1 215 chars against a seeded cap of 3 600: three fit, the
    # fourth is dropped, and the next pass of the loop escalates. Sized so the
    # budget actually binds — an end-to-end test that never escalates would
    # pass no matter what raise_budget did.
    p = ArchProbe(_Bridge(1200), max_chars=2000, max_total_chars=2000)
    p.configure_learning(warmup=1, headroom=4.0, ceiling=0)
    p._round_costs = [900]          # seeded cap = 3 600, above the 2 000 floor
    r._probe = p

    tp = tmp_path / "t.jsonl"
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: facts a, facts b, facts c, facts d"] * 6
            + [_good("G")],
        ):
            r.review_clusters([cluster], base_dir, goal="g")
    finally:
        tracer.configure(enabled=False)

    ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    esc = [e for e in ev if e.get("kind") == "probe_escalated"]
    assert esc, "the run must actually escalate for this to test anything"
    for e in esc:
        assert int(e["params"]["new_cap"]) > 3600, (
            f"escalation to {e['params']['new_cap']} is not wider than the "
            f"seeded cap of 3600 it started from"
        )
