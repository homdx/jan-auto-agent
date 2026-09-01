"""tests/test_auto_p9_budget_learning.py — AUTO-P9: learn the working digest
cap instead of rediscovering it once per batch.

AUTO-P8 added an escalation ladder for rounds cut short by the digest cap. It
worked — and the measured run showed what it cost: **57 escalations across 63
probing batches**, 35 at step 1 and 22 at step 2, each one an extra architect
call. Nearly every batch climbed the same ladder from the same configured
floor to rediscover the same answer.

The numbers say why. Configured cap 10 000; median cost of a completed round
**9 582**; p90 **20 028**; max 23 257. Half of all rounds could not fit on
the first try, by construction.

So measure it. Record what each *complete* round actually cost, take the
median once there are enough samples, apply headroom, and start later batches
there. Replaying the run's 216 completed rounds:

    headroom   cap     overflow   escalations avoided
         1.0   10000        48%     0   (the status quo)
         1.5   14373        25%    ~26
         2.0   19165        12%    ~42
         2.5   23956         0%    ~57

2.0 is the shipped default: it removes roughly three quarters of the
escalations while keeping the prompt a third smaller than 2.5 would, and
leaves the 12% tail to the ladder, which is what a ladder is for.

  AC-P9-1   Nothing is seeded during warm-up.
  AC-P9-2   After warm-up the cap is median x headroom.
  AC-P9-3   Median, not mean — one huge round must not drag every batch up.
  AC-P9-4   Only COMPLETE rounds are sampled; truncated ones are not.
  AC-P9-5   The seeded cap never goes below the configured floor.
  AC-P9-6   The ceiling bounds it; 0 means 4x the configured cap.
  AC-P9-7   reset() applies the seeded cap, and samples survive reset().
  AC-P9-8   With a seeded cap the ladder skips the temperature rung.
  AC-P9-9   Without one, the full AUTO-P8 ladder still applies.
  AC-P9-10  warmup=0 disables learning entirely.
  AC-P9-11  The escalation event says which ladder was in force.
  AC-P9-12  End to end: a later batch starts wider than the first did.
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


def _learner(costs=(), *, warmup=3, headroom=2.0, ceiling=0,
             configured=10000) -> ArchProbe:
    p = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=configured)
    p.configure_learning(warmup=warmup, headroom=headroom, ceiling=ceiling)
    p._round_costs = list(costs)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# The estimator
# ─────────────────────────────────────────────────────────────────────────────

class TestSeededCap:

    @pytest.mark.parametrize("n", [0, 1, 2])
    def test_no_seed_during_warmup(self, n: int) -> None:
        """AC-P9-1: two samples is not evidence. Seeding off them would swap
        one guess for another."""
        p = _learner([12000] * n)
        assert p.seeded_cap is None
        assert p.has_seeded_cap is False

    def test_median_times_headroom(self) -> None:
        """AC-P9-2"""
        p = _learner([8000, 10000, 12000], headroom=2.0)
        assert p.seeded_cap == 20000

    def test_median_not_mean(self) -> None:
        """AC-P9-3: the measured distribution is skewed — median 9 582 against
        a 23 257 max. A mean would let one outlier buy every subsequent batch
        a prompt it does not need."""
        costs = [9000, 9500, 10000, 10500, 90000]
        p = _learner(costs, headroom=1.0)
        assert p.seeded_cap == 10000            # the median
        assert p.seeded_cap < sum(costs) / len(costs)

    def test_even_sample_count_averages_the_middle_pair(self) -> None:
        p = _learner([8000, 10000, 12000, 14000], headroom=1.0)
        assert p.seeded_cap == 11000

    def test_never_below_the_configured_floor(self) -> None:
        """AC-P9-5: learning must not quietly shrink a cap the operator set."""
        p = _learner([100, 200, 300], headroom=2.0, configured=10000)
        assert p.seeded_cap == 10000

    def test_ceiling_bounds_the_estimate(self) -> None:
        """AC-P9-6: a pathological run must not be able to learn its way to an
        unbounded prompt."""
        p = _learner([50000] * 3, headroom=2.0, ceiling=25000)
        assert p.seeded_cap == 25000

    def test_default_ceiling_is_four_times_configured(self) -> None:
        """AC-P9-6"""
        p = _learner([50000] * 3, headroom=2.0, ceiling=0, configured=10000)
        assert p.seeded_cap == 40000

    def test_warmup_zero_disables_learning(self) -> None:
        """AC-P9-10: the off switch."""
        p = _learner([9000] * 20, warmup=0)
        assert p.seeded_cap is None


class TestSampling:

    def test_only_complete_rounds_are_sampled(self) -> None:
        """AC-P9-4: the crux. A truncated round tells you what the cap WAS,
        not what the round needed; sampling those pegs the estimate to the
        cap that was already too small — the exact loop this ticket ends."""
        p = ArchProbe(_Bridge(400), max_chars=600, max_total_chars=600)
        p.configure_learning(warmup=1, headroom=1.0, ceiling=0)
        p.execute([ProbeOp("facts", n) for n in ("a", "b", "c")])
        assert p.last_dropped, "this round must be truncated for the test to mean anything"
        assert p._round_costs == []
        assert p.seeded_cap is None

        p.reset()
        p.execute([ProbeOp("facts", "a")])
        assert not p.last_dropped
        assert len(p._round_costs) == 1

    def test_samples_survive_reset_and_the_cap_is_applied(self) -> None:
        """AC-P9-7: reset() is per batch; learning is per run. If reset()
        cleared the samples nothing could ever be learned, and if it ignored
        them the learning would never be used."""
        p = _learner([8000, 10000, 12000], headroom=2.0)
        p.reset()
        assert len(p._round_costs) == 3
        assert p._max_total_chars == 20000

    def test_reset_falls_back_to_configured_during_warmup(self) -> None:
        p = _learner([9000], headroom=2.0, configured=10000)
        p.reset()
        assert p._max_total_chars == 10000


# ─────────────────────────────────────────────────────────────────────────────
# Ladder selection
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {"temperature": "0.2", "max_tokens": "512", "probe_enabled": "true",
            "probe_max_rounds": "5", "probe_allowed_ops": "facts",
            "probe_max_chars": "600", "probe_max_total_chars": "600",
            "probe_budget_escalations": "2", "retry_delays_sec": ""}
    arch.update(over)
    c = configparser.ConfigParser()
    c.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": arch, "loop": {"timeout_seconds": "10"},
    })
    return c


def _reviewer(cfg, *, costs=()) -> ClusterReviewer:
    r = ClusterReviewer(config=cfg, base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    p = ArchProbe(_Bridge(400),
                  max_chars=cfg.getint("architect", "probe_max_chars"),
                  max_total_chars=cfg.getint("architect", "probe_max_total_chars"))
    p.configure_learning(
        warmup=cfg.getint("architect", "probe_budget_warmup", fallback=3),
        headroom=cfg.getfloat("architect", "probe_budget_headroom", fallback=2.0),
        ceiling=cfg.getint("architect", "probe_budget_max_chars", fallback=0),
    )
    p._round_costs = list(costs)
    r._probe = p
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


_BIG = "ARCH_PROBE: facts a, facts b, facts c, facts d"


def _events(tp: Path) -> list[dict]:
    return [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]


def _run(r, cluster, base_dir, tp, responses):
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch("tools.llm_stream.request_completion", side_effect=responses) as m:
            r.review_clusters([cluster], base_dir, goal="g")
            return m
    finally:
        tracer.configure(enabled=False)


class TestLadderSelection:

    def test_ladders_actually_differ(self) -> None:
        """Guards the premise of the next two tests."""
        assert [t for _, t in _BUDGET_ONLY_LADDER] == [0.0, 0.0, 0.0]
        assert any(t for _, t in _BUDGET_ESCALATION_LADDER)

    def test_seeded_run_skips_the_temperature_rung(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P9-8 / AC-P9-11: the temperature rung exists to break a
        deterministic loop when we have no idea how much room a round needs.
        With a measured median we DO know, so the honest remedy is more room —
        a re-roll would ask something different from what the wider budget was
        bought for."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), costs=[500, 600, 700])
        assert r._probe.has_seeded_cap
        tp = tmp_path / "seeded.jsonl"
        m = _run(r, cluster, base_dir, tp, [_BIG] * 6 + [_good("G")])

        temps = []
        for c in m.call_args_list:
            payload = c.kwargs.get("payload") or c.args[2]
            temps.append(payload.get("temperature"))
        assert 0.7 not in temps, f"temperature rung must not fire when seeded: {temps}"

        esc = [e for e in _events(tp) if e.get("kind") == "probe_escalated"]
        assert esc and all(e["params"]["seeded"] == "True" for e in esc)

    def test_unseeded_run_uses_the_full_ladder(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P9-9: AUTO-P8's behaviour is unchanged while still learning."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), costs=[])
        assert not r._probe.has_seeded_cap
        tp = tmp_path / "unseeded.jsonl"
        m = _run(r, cluster, base_dir, tp, [_BIG] * 6 + [_good("G")])

        temps = []
        for c in m.call_args_list:
            payload = c.kwargs.get("payload") or c.args[2]
            temps.append(payload.get("temperature"))
        assert 0.7 in temps, f"the AUTO-P8 ladder must still fire unseeded: {temps}"

        esc = [e for e in _events(tp) if e.get("kind") == "probe_escalated"]
        assert esc and esc[0]["params"]["seeded"] == "False"


def test_later_batch_starts_wider_than_the_first(cluster_and_base) -> None:
    """AC-P9-12: the whole point, end to end. The first batch pays to find
    out; the ones after it do not."""
    cluster, base_dir = cluster_and_base
    c2 = RepoCluster(name="agents2", patterns=["tools/*"],
                     files=["tools/example.py"])
    r = _reviewer(_cfg(probe_budget_warmup="1", probe_budget_headroom="3.0"))
    assert not r._probe.has_seeded_cap

    with patch(
        "tools.llm_stream.request_completion",
        side_effect=["ARCH_PROBE: facts a", _good("A"),
                     "ARCH_PROBE: facts a", _good("B")],
    ):
        results = r.review_clusters([cluster, c2], base_dir, goal="g")

    assert sorted(x.title for x in results) == ["A", "B"]
    assert r._probe.has_seeded_cap, "one complete round should be enough at warmup=1"
    assert r._probe._max_total_chars > 600, (
        "the second batch must start above the configured floor"
    )
