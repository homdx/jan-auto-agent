"""tests/test_auto_p12_round_cap_unbounded.py — AUTO-P12: don't discard the
request that hit the round cap.

Every decline branch in the probe loop (``digest_budget`` after its ladder is
spent, ``round_cap``, ``repeat``, ``no_executor``) throws away the pending
``ARCH_PROBE`` request and forces a final plan call with whatever digest had
already accumulated. For ``round_cap`` specifically that is needlessly
wasteful: a real run showed the model ask for exactly the file
(``tools/backoff.py:80-120``) that would have grounded its plan, get cut off
by the round cap with that request still unanswered, and come back with zero
candidates on the forced call.

The forced call was always going to happen — this does not add an LLM
round-trip. It answers the one pending request in full, with the per-op and
total digest caps both lifted (there is no further round left for an
oversized answer to blow, and no future round for a dropped op to be asked
about again), and folds the result into the same forced call.

  AC-P12-1  ArchProbe.execute(..., unbounded=True) ignores the per-op cap.
  AC-P12-2  ... and ignores the total digest budget, even if already spent.
  AC-P12-3  An unbounded round is not recorded into AUTO-P9's round_costs —
            it is not an honest sample of what a normal round needs.
  AC-P12-4  unbounded doesn't fabricate answers: an all-miss request still
            returns "".
  AC-P12-5  chars_used/run_chars_used still tally an unbounded call.
  AC-P12-6  End to end: hitting the round cap with a pending request answers
            it in full (no truncation) and folds it into the forced call's
            prompt, at no extra LLM call.
  AC-P12-7  The digest from earlier, normal rounds is preserved alongside it.
  AC-P12-8  A request that is genuinely unresolvable even unbounded falls
            back to the pre-AUTO-P12 behaviour: decline("round_cap") and a
            forced call with nothing added.
  AC-P12-9  The unbounded answer reaches the trace as a normal probe_result,
            marked unbounded=True, and no probe_declined(round_cap) fires
            for the batch it resolved.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.agent_trace import tracer
from tools.auto.arch_probe import ArchProbe, ProbeOp
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Unit level — ArchProbe.execute(..., unbounded=True)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}

    def pull_symbol(self, name: str) -> str:
        return self._answers.get(name, "")


def _ops(*names: str) -> list[ProbeOp]:
    return [ProbeOp("facts", n) for n in names]


class TestUnboundedExecute:

    def test_unbounded_bypasses_per_op_cap(self) -> None:
        """AC-P12-1: a normal call truncates an oversized single result;
        unbounded must return it whole."""
        bridge = _FakeBridge({"big": "z" * 5000})

        capped = ArchProbe(bridge, max_chars=500).execute(_ops("big"))
        assert "truncated" in capped

        full = ArchProbe(bridge, max_chars=500).execute(
            _ops("big"), unbounded=True
        )
        assert "truncated" not in full
        assert "z" * 5000 in full

    def test_unbounded_bypasses_total_budget(self) -> None:
        """AC-P12-2: an already-spent total budget would normally drop
        every remaining op outright; unbounded must still answer all of
        them."""
        bridge = _FakeBridge({"a": "AAA", "b": "BBB"})
        probe = ArchProbe(bridge, max_chars=100, max_total_chars=10)
        # Simulate a batch that already spent its whole digest budget in
        # earlier rounds, exactly the state the round-cap branch finds it in.
        probe._chars_used = probe._max_total_chars
        assert probe.budget_exhausted is True

        out = probe.execute(_ops("a", "b"), unbounded=True)
        assert "AAA" in out and "BBB" in out
        assert probe.last_dropped == []

    def test_unbounded_round_not_recorded_for_learning(self) -> None:
        """AC-P12-3: AUTO-P9 seeds later batches' caps from completed-round
        costs. A round with the cap lifted is not an honest sample of what
        a normal budget needs and must not feed that estimate."""
        probe = ArchProbe(
            _FakeBridge({"a": "AAA"}), max_chars=100, max_total_chars=100
        )
        probe.execute(_ops("a"), unbounded=True)
        assert probe._round_costs == []

    def test_unbounded_all_miss_still_returns_empty(self) -> None:
        """AC-P12-4: the cap is lifted, not the honesty check — a request
        that cannot be resolved is still nothing to act on."""
        probe = ArchProbe(_FakeBridge({}))
        assert probe.execute(_ops("nope"), unbounded=True) == ""

    def test_unbounded_still_tallies_chars(self) -> None:
        """AC-P12-5: the cap is lifted, not the observability."""
        probe = ArchProbe(
            _FakeBridge({"a": "AAA"}), max_chars=1, max_total_chars=1
        )
        probe.execute(_ops("a"), unbounded=True)
        assert probe.chars_used > 0


# ─────────────────────────────────────────────────────────────────────────────
# End to end — the round-cap branch inside ClusterReviewer.review_clusters
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {
        "temperature": "0.2", "max_tokens": "512", "probe_enabled": "true",
        "probe_max_rounds": "1", "probe_max_chars": "40",
        "probe_max_total_chars": "9999", "probe_budget_escalations": "0",
        "retry_delays_sec": "",
    }
    arch.update(over)
    c = configparser.ConfigParser()
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": arch, "loop": {"timeout_seconds": "10"},
    })
    return c


def _reviewer(cfg, bridge) -> ClusterReviewer:
    r = ClusterReviewer(
        config=cfg, base_url="http://localhost:1337/v1", api_key="t",
        model="m", api_format="openai", verify_ssl=False,
    )
    r._probe_built = True
    r._probe = ArchProbe(
        bridge,
        max_chars=cfg.getint("architect", "probe_max_chars"),
        max_total_chars=cfg.getint("architect", "probe_max_total_chars"),
    )
    return r


@pytest.fixture()
def cluster_and_base(tmp_path: Path) -> tuple[RepoCluster, Path]:
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    cl = RepoCluster(name="agents", patterns=["tools/*"], files=["tools/example.py"])
    return cl, tmp_path


def _good(title: str) -> str:
    return json.dumps([{
        "title": title, "instruction": "Do it.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {"file": "tools/example.py", "symbol": "fn",
                            "line_start": 1, "line_end": 1},
    }])


def _msgs(mock_llm) -> list[str]:
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        messages = payload.get("messages", [])
        out.append(next((m["content"] for m in messages if m.get("role") == "user"), ""))
    return out


class TestRoundCapAnswersLastRequest:

    def test_pending_request_answered_in_full_before_forcing(
        self, cluster_and_base
    ) -> None:
        """AC-P12-6 / AC-P12-7: the headline. probe_max_chars=40 would
        normally truncate the 200-char answer hard; hitting the round cap
        with it still pending must deliver it whole, on top of what earlier
        rounds already dug up — at no extra LLM call."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({"round1": "R1-ANSWER", "final": "F" * 200})
        r = _reviewer(_cfg(), bridge)
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[
                "ARCH_PROBE: facts round1",
                "ARCH_PROBE: facts final",
                _good("Forced task"),
            ],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 3, "no extra round-trip was spent"
        forced_prompt = _msgs(mock_llm)[2]
        assert "F" * 200 in forced_prompt, "the pending request must not be truncated"
        assert "truncated" not in forced_prompt
        assert "R1-ANSWER" in forced_prompt, "earlier rounds' digest must survive"
        assert [x.title for x in results] == ["Forced task"]

    def test_unresolvable_pending_request_falls_back_to_decline(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P12-8: unbounded only removes the cap, not the requirement
        that something actually resolves. A symbol that does not exist
        must still fall back to the pre-AUTO-P12 forced call."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({"round1": "R1-ANSWER"})  # "final" is unknown
        r = _reviewer(_cfg(), bridge)
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=[
                    "ARCH_PROBE: facts round1",
                    "ARCH_PROBE: facts final",
                    _good("Forced task"),
                ],
            ) as mock_llm:
                results = r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        assert mock_llm.call_count == 3
        forced_prompt = _msgs(mock_llm)[2]
        from tools.auto import arch_probe
        assert arch_probe.FORCED_SUFFIX in forced_prompt
        assert [x.title for x in results] == ["Forced task"]

        events = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        declines = [e for e in events if e.get("kind") == "probe_declined"]
        assert any(e["params"]["reason"] == "round_cap" for e in declines), (
            "an unresolvable pending request must still be declined, as before"
        )

    def test_resolved_pending_request_reaches_trace_and_skips_decline(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P12-9: a resolved unbounded answer is observable like any other
        probe_result, and — since it WAS answered — must not also show up as
        a round_cap decline."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({"round1": "R1-ANSWER", "final": "F" * 200})
        r = _reviewer(_cfg(), bridge)
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=[
                    "ARCH_PROBE: facts round1",
                    "ARCH_PROBE: facts final",
                    _good("Forced task"),
                ],
            ):
                r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        events = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        results = [e for e in events if e.get("kind") == "probe_result"]
        assert any(e["params"].get("unbounded") == "True" for e in results)
        declines = [e for e in events if e.get("kind") == "probe_declined"]
        assert not any(e["params"]["reason"] == "round_cap" for e in declines)