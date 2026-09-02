"""tests/test_auto_f4a_declined_facts_seq.py — AUTO-F4a: the informed/blind
split misses declined rounds.

AUTO-F4 taught `ArchProbe` to tally `informed_facts` / `blind_facts` and put
them on every `probe_result` event. It missed the exact hole AUTO-P6
(`4520393`) already had to close once before, for the general `by_op`
tally: an all-miss round returns an empty digest and emits **no**
`probe_result` at all (AUTO-P4b) — it emits `probe_declined` instead. Any
`facts` asks inside such a round were tallied in-memory (informed vs.
blind, hit vs. miss) but that update never reached a `probe_result` event,
so `analyze_logs.py`'s "facts sequencing" line silently under-counted.

Found reviewing AUTO-F4 against a live run (`trace_e8d5b9fcd4b2`): a
declined "support (batch 11/50)" round asked `facts RetryLoop` and
`facts make_retry_loop` after two `module` misses taught it nothing — both
blind, both misses, both invisible to the reported split. The run's `by_op`
correctly showed `facts 15/22`; the "facts sequencing" line's own
denominator only summed to 20.

  AC-F4a-1  An `unresolved` decline with a blind `facts` miss carries a real
            `blind_facts` count on the `probe_declined` event.
  AC-F4a-2  An `unresolved` decline with an informed-but-still-missed
            `facts` ask carries a real `informed_facts` count (0 hits, the
            ask still counts).
  AC-F4a-3  Every other decline reason (`round_cap`, `no_executor`, ...)
            records "0/0" for both — nothing was looked up, mirroring
            `_decline_by_op`'s existing "0/0" for `by_op`.
  AC-F4a-4  `analyze_logs` includes a declined round's contribution in the
            rendered "facts sequencing" split.
  AC-F4a-5  A pre-AUTO-F4a trace (no informed_facts/blind_facts on
            probe_declined) still renders without crashing.
  AC-F4a-6  Totals sum correctly across multiple clusters when one ends in
            a decline and another in an ordinary result.
"""

from __future__ import annotations

import configparser
import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import analyze_logs
from tools.agent_trace import tracer
from tools.auto.arch_probe import ArchProbe
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Integration harness — mirrors tests/test_auto_p6_decline_tallies.py exactly,
# since this fix sits right next to the code AUTO-P6 already patched once.
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}

    def pull_symbol(self, name: str) -> str:
        return self._answers.get(name, "")

    def module_symbols(self, ref: str) -> str:
        return self._answers.get(ref, "")


def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {"temperature": "0.2", "max_tokens": "512", "probe_enabled": "true",
            "probe_max_rounds": "1", "probe_allowed_ops": "facts, module",
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


def _reviewer(cfg, bridge) -> ClusterReviewer:
    r = ClusterReviewer(config=cfg, base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    r._probe = ArchProbe(bridge, max_chars=2000, max_total_chars=6000) if bridge else None
    return r


@pytest.fixture()
def cluster_and_base(tmp_path: Path):
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    return RepoCluster(name="agents", patterns=["tools/*"],
                       files=["tools/example.py"]), tmp_path


def _good(*t: str) -> str:
    return json.dumps([{
        "title": x, "instruction": "Do it.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {"file": "tools/example.py", "symbol": "fn",
                           "line_start": 1, "line_end": 1},
    } for x in t])


def _run(reviewer, cluster, base_dir, tmp_path, responses, name="t.jsonl"):
    tp = tmp_path / name
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch("tools.llm_stream.request_completion", side_effect=responses):
            reviewer.review_clusters([cluster], base_dir, goal="g")
    finally:
        tracer.configure(enabled=False)
    return [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]


def _declines(ev) -> list[dict]:
    return [e for e in ev if e.get("kind") == "probe_declined"]


class TestDeclineFactsSeq:

    def test_unresolved_decline_with_blind_miss_carries_blind_facts(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-F4a-1: the exact shape found in the live run — a blind guess
        that misses, inside an all-miss round."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), _FakeBridge({}))
        ev = _run(r, cluster, base_dir, tmp_path,
                  ["ARCH_PROBE: facts retry", _good("Forced")], "blind.jsonl")
        d = _declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "unresolved"
        assert d[0]["params"]["blind_facts"] == "0/1", (
            "one blind ask, zero hits — must not be invisible to the split"
        )
        assert d[0]["params"]["informed_facts"] == "0/0"

    def test_unresolved_decline_with_informed_miss_carries_informed_facts(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-F4a-2: a name `module` DID list, asked afterwards, that still
        misses the bridge (collect's symbol table and module's inventory
        can disagree) — informed, but still a miss, and the whole point of
        AUTO-F4a is that this must not vanish from the report either."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({
            "tools/x.py": "module: tools/x.py\n  helper(...)  :1 — a thing",
            # "helper" deliberately absent — module knows the name, the
            # bridge's own pull_symbol does not.
        })
        r = _reviewer(_cfg(probe_max_rounds="2"), bridge)
        ev = _run(
            r, cluster, base_dir, tmp_path,
            ["ARCH_PROBE: module tools/x.py",
             "ARCH_PROBE: facts helper",
             _good("Forced")],
            "informed_miss.jsonl",
        )
        d = _declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "unresolved"
        assert d[0]["params"]["informed_facts"] == "0/1", (
            "module listed 'helper', so the ask is informed — but it still "
            "misses the bridge, so zero hits"
        )
        assert d[0]["params"]["blind_facts"] == "0/0"

    def test_round_cap_and_no_executor_record_zero_for_facts_seq(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-F4a-3: mirrors AC-P6-2/3 exactly — nothing was looked up for
        these reasons, so both fields are '0/0', same as by_op."""
        cluster, base_dir = cluster_and_base

        r = _reviewer(_cfg(probe_max_rounds="1"),
                      _FakeBridge({"fn": "module: x"}))
        ev = _run(r, cluster, base_dir, tmp_path,
                  ["ARCH_PROBE: facts fn", "ARCH_PROBE: facts unknown_fn",
                   _good("Forced")], "cap.jsonl")
        d = _declines(ev)
        assert d[0]["params"]["reason"] == "round_cap"
        assert d[0]["params"]["informed_facts"] == "0/0"
        assert d[0]["params"]["blind_facts"] == "0/0"

        r2 = _reviewer(_cfg(), None)
        ev2 = _run(r2, cluster, base_dir, tmp_path,
                   ["ARCH_PROBE: facts fn, module tools/x.py", _good("F")],
                   "noex.jsonl")
        d2 = _declines(ev2)
        assert d2[0]["params"]["reason"] == "no_executor"
        assert d2[0]["params"]["informed_facts"] == "0/0"
        assert d2[0]["params"]["blind_facts"] == "0/0"


# ─────────────────────────────────────────────────────────────────────────────
# AC-F4a-4/5/6: analyze_logs rendering, against synthetic events (same style
# as tests/test_auto_f4_informed_facts.py's TestAnalyzeLogsRendering).
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
        analyze_logs.render_run_summary(run)
    finally:
        sys.stdout = old_stdout
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())


class TestAnalyzeLogsIncludesDeclines:

    def test_declined_rounds_blind_miss_reaches_the_reported_split(self) -> None:
        """AC-F4a-4: the headline test. Round 1 resolves one informed hit;
        round 2 is an all-miss decline that adds one more blind ask. Before
        this fix, the decline's contribution was invisible and the split
        would under-report as informed 1/1, blind 0/0 — exactly the 20-vs-22
        gap found in the live run."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
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
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt(
                "probe_declined",
                {
                    "cluster": "c1", "reason": "unresolved", "ops": 1,
                    "round": 0, "by_op": "facts=0/1",
                    "informed_facts": "1/1", "blind_facts": "0/1",
                },
            ),
        ]
        runs = analyze_logs.analyze(events)
        text = _capture_summary(runs["run1"])
        assert "facts sequencing" in text
        assert "informed" in text and "100% (1/1)" in text
        assert "blind" in text and "0% (0/1)" in text, (
            "the declined round's blind miss must reach the denominator"
        )

    def test_pre_auto_f4a_decline_still_renders(self) -> None:
        """AC-F4a-5: a decline with no informed_facts/blind_facts params
        (older trace) must not crash and must not corrupt the total that
        the probe_result events already established."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
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
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt(
                "probe_declined",
                {"cluster": "c1", "reason": "unresolved", "ops": 1,
                 "round": 0, "by_op": "facts=0/1"},
                # no informed_facts / blind_facts — pre-AUTO-F4a shape
            ),
        ]
        runs = analyze_logs.analyze(events)
        text = _capture_summary(runs["run1"])
        assert "facts sequencing" in text
        assert "informed" in text and "100% (1/1)" in text, (
            "the decline lacking the field must not overwrite the real "
            "total the prior probe_result established"
        )

    def test_sums_correctly_when_one_cluster_ends_in_a_decline(self) -> None:
        """AC-F4a-6: extends AUTO-F4's own multi-cluster dedup test — c1
        ends in an ordinary result, c2 ends in a decline. Both must
        contribute to the grand total exactly once."""
        events = [
            _evt("probe_request", {"cluster": "c1", "ops": 1}),
            _evt("probe_result", {
                "cluster": "c1", "round": 1, "ops": 1, "hits": 1, "misses": 0,
                "by_op": "facts=1/0", "chars_used": 10, "run_chars_used": 10,
                "informed_facts": "1/1", "blind_facts": "0/0",
            }, source="probe", target="architect"),
            _evt("probe_request", {"cluster": "c2", "ops": 2}),
            _evt("probe_declined", {
                "cluster": "c2", "reason": "unresolved", "ops": 2, "round": 0,
                "by_op": "facts=0/2",
                "informed_facts": "0/0", "blind_facts": "0/2",
            }),
        ]
        runs = analyze_logs.analyze(events)
        text = _capture_summary(runs["run1"])
        # c1: informed 1/1. c2 (declined): blind 0/2. Grand total:
        # informed 1/1, blind 0/2 — not double-counted, not dropped.
        assert "informed" in text and "100% (1/1)" in text
        assert "blind" in text and "0% (0/2)" in text
