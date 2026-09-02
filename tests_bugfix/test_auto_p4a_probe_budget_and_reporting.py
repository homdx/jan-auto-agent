"""tests/test_auto_p4a_probe_budget_and_reporting.py — AUTO-P4a: three
defects found by reading a real run's trace, not by reading the code.

The run in question ("remove duplicated retry logic across the codebase",
202 architect batches, 12 probe requests) produced a summary line that was
wrong in one place, a budget that was silently wrong in another, and a
silence that could not be distinguished from the feature not existing.

  1. **Per-run digest budget.** `ArchProbe` is memoized on the
     `ClusterReviewer` for the whole run, and `_chars_used` was created with
     the instance and never cleared — so `probe_max_total_chars`, documented
     and configured as a PER-BATCH cap, actually accumulated across every
     batch. The real trace shows it: 69 → 154 → 209 → 308 → 378 → 455 → 482
     → 511, monotonically increasing across four different clusters. That
     run stopped at 511 of 6000 so nothing broke, but a longer or
     probe-heavier run would cross the cap and the probe would switch itself
     off for the remainder — appearing in the log as "the model stopped
     asking".

  2. **"N unresolved" conflated four causes.** analyze_logs computed
     requests-minus-results and the runbook documented that gap as "collect
     did not know these symbols". In the real run all 4 were the round cap
     and none were collect misses — collect had answered 8 of 8. The fixes
     for those two readings are opposite (raise probe_max_rounds vs rebuild
     the artifact), so the number was worse than useless.

  3. **Zero probes rendered as silence.** The report printed nothing unless
     at least one probe existed, making an enabled-but-unused run
     byte-identical to a pre-AUTO-P run. For a feature whose Phase 0
     decision gate is precisely "how often is this used", the single most
     important measurement was unobservable.

  AC-P4a-1   chars_used resets between batches; run_chars_used does not.
  AC-P4a-2   reset() does not rebuild or disturb the bridge.
  AC-P4a-3   Two batches, each near the cap, both still probe (the
             regression that defect 1 would reintroduce).
  AC-P4a-4   The digest budget still bites WITHIN one batch.
  AC-P4a-5   probe_config is emitted once when the probe is usable.
  AC-P4a-6   probe_config records usable=false + reason when it is not.
  AC-P4a-7   No probe_config at all when probe_enabled=false.
  AC-P4a-8   probe_declined carries reason="round_cap" when the cap is hit.
  AC-P4a-9   probe_declined carries reason="unresolved" when collect answers
             nothing — the case the old metric claimed was the only one.
  AC-P4a-10  probe_declined carries reason="no_executor".
  AC-P4a-11  analyze_logs distinguishes enabled-and-unused from absent.
  AC-P4a-12  analyze_logs breaks declines down by reason.
  AC-P4a-13  analyze_logs computes the decision-gate request rate.
  AC-P4a-14  A pre-AUTO-P4a trace still renders (no declines, no config).
  AC-P4a-15  Tracing failures never break a batch.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import analyze_logs
from tools.agent_trace import tracer
from tools.auto import arch_probe
from tools.auto.arch_probe import ArchProbe, ProbeOp
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {
        "temperature": "0.2", "max_tokens": "512",
        "probe_enabled": "true", "probe_max_rounds": "1",
        "retry_delays_sec": "",
    }
    arch.update(over)
    c = configparser.ConfigParser()
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai",
        },
        "architect": arch,
        "loop":      {"timeout_seconds": "10"},
    })
    return c


class _FakeBridge:
    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}
        self.calls: list[str] = []

    def pull_symbol(self, name: str) -> str:
        self.calls.append(name)
        return self._answers.get(name, "")


def _reviewer(cfg, bridge=None) -> ClusterReviewer:
    r = ClusterReviewer(
        config=cfg, base_url="http://localhost:1337/v1", api_key="test",
        model="test-model", api_format="openai", verify_ssl=False,
    )
    r._probe_built = True
    r._probe = ArchProbe(
        bridge,
        max_chars=cfg.getint("architect", "probe_max_chars", fallback=2000),
        max_total_chars=cfg.getint("architect", "probe_max_total_chars", fallback=6000),
    ) if bridge is not None else None
    return r


@pytest.fixture()
def cluster_and_base(tmp_path: Path):
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    return RepoCluster(name="agents", patterns=["tools/*"],
                       files=["tools/example.py"]), tmp_path


def _task(title: str) -> dict:
    return {
        "title": title, "instruction": "Do the fix.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {"file": "tools/example.py", "symbol": "fn",
                           "line_start": 1, "line_end": 1},
    }


def _good(*t: str) -> str:
    return json.dumps([_task(x) for x in t])


_PROBE = "ARCH_PROBE: facts fn"
_FACTS = {"fn": "module: tools/example.py\nsignature: fn()"}


def _msgs(mock_llm) -> list[str]:
    """User-message content sent on each call, in call order."""
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        messages = payload.get("messages", [])
        out.append(next((m["content"] for m in messages if m.get("role") == "user"), ""))
    return out


def _events(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _run_traced(reviewer, clusters, base_dir, tmp_path, responses, name="t.jsonl"):
    """Drive review_clusters with tracing on; return the trace events."""
    tp = tmp_path / name
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch("tools.llm_stream.request_completion", side_effect=responses):
            reviewer.review_clusters(clusters, base_dir, goal="improve code")
    finally:
        tracer.configure(enabled=False)
    return _events(tp)


# ─────────────────────────────────────────────────────────────────────────────
# Defect 1 — the per-batch digest budget
# ─────────────────────────────────────────────────────────────────────────────

class TestDigestBudgetIsPerBatch:

    def test_reset_clears_batch_counter_not_run_counter(self) -> None:
        """AC-P4a-1"""
        p = ArchProbe(_FakeBridge({"a": "X" * 300}), max_chars=1000,
                      max_total_chars=1000)
        p.execute([ProbeOp("facts", "a")])
        first = p.chars_used
        assert first > 0 and p.run_chars_used == first

        p.reset()
        assert p.chars_used == 0, "batch budget must start clean"
        assert p.run_chars_used == first, "run total must survive the reset"

        p.execute([ProbeOp("facts", "a")])
        assert p.chars_used == first
        assert p.run_chars_used == 2 * first

    def test_reset_does_not_disturb_the_bridge(self) -> None:
        """AC-P4a-2: make_collect_bridge is build-once-per-run; reset() must
        not become a back door around that."""
        b = _FakeBridge({"a": "X"})
        p = ArchProbe(b, max_chars=500, max_total_chars=500)
        p.execute([ProbeOp("facts", "a")])
        p.reset()
        assert p._bridge is b
        assert p.usable is True
        assert b.calls == ["a"]

    def test_second_batch_still_probes_after_first_neared_the_cap(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P4a-3: the end-to-end shape of defect 1. Without the reset, the
        first batch spends the run's whole budget and every later batch
        silently loses the feature."""
        cluster, base_dir = cluster_and_base
        # One op must EXCEED the cap on its own, so that after batch 1 the
        # budget is spent. A payload that merely approaches the cap leaves
        # budget_exhausted False and the bug stays invisible — which is
        # exactly how an earlier version of this test passed against a
        # mutant with the reset deleted.
        r = _reviewer(
            _cfg(probe_max_chars="500", probe_max_total_chars="500"),
            bridge=_FakeBridge({"fn": "Y" * 900}),
        )
        c2 = RepoCluster(name="agents2", patterns=["tools/*"],
                         files=["tools/example.py"])

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE, _good("A"), _PROBE, _good("B")],
        ) as mock_llm:
            results = r.review_clusters([cluster, c2], base_dir, goal="improve code")

        assert mock_llm.call_count == 4
        assert sorted(x.title for x in results) == ["A", "B"]

        # Call counts alone cannot tell the two behaviours apart — both spend
        # two calls on batch 2. What differs is WHICH second call it is: a
        # re-ask carrying the digest, or a forced "plan with what you have".
        msgs = _msgs(mock_llm)
        assert "## Probe results" in msgs[3], (
            "batch 2's re-ask must carry a freshly resolved digest; a leaked "
            "run-level budget would have blocked the lookup"
        )
        assert arch_probe.FORCED_SUFFIX not in msgs[3], (
            "batch 2 must not have been pushed to a forced call by batch 1's "
            "spending"
        )
        # And the counters themselves: per-batch reset, per-run cumulative.
        assert r._probe.chars_used < r._probe.run_chars_used

    def test_budget_still_bites_within_one_batch(self) -> None:
        """AC-P4a-4: the fix must not disarm the cap it makes per-batch."""
        b = _FakeBridge({n: "Z" * 400 for n in ("a", "b", "c")})
        p = ArchProbe(b, max_chars=400, max_total_chars=400)
        p.execute([ProbeOp("facts", x) for x in ("a", "b", "c")])
        assert b.calls == ["a"], "the cap must stop the remaining ops"
        assert p.budget_exhausted is True


# ─────────────────────────────────────────────────────────────────────────────
# probe_config — telling "never used" from "not installed"
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeConfigEvent:

    def test_emitted_once_when_usable(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-5"""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=_FakeBridge(_FACTS))
        r._probe_built = False  # force the real _get_probe path
        r._probe = None
        with patch(
            "tools.auto.collect_bridge.make_collect_bridge",
            return_value=_FakeBridge(_FACTS),
        ):
            ev = _run_traced(r, [cluster], base_dir, tmp_path,
                             [_good("T")], "cfg_ok.jsonl")

        cfgs = [e for e in ev if e.get("kind") == "probe_config"]
        assert len(cfgs) == 1, "exactly one per run, not one per batch"
        p = cfgs[0]["params"]
        assert p["usable"] == "True" and p["reason"] == "ok"
        assert int(p["max_rounds"]) == 1
        assert p["allowed_ops"] == "facts"

    def test_records_unusable_with_reason(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-6: 'enabled but nothing can answer' is a third state, and
        it explains a zero-probe run without blaming the model."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=None)
        r._probe_built = False
        with patch("tools.auto.collect_bridge.make_collect_bridge", return_value=None):
            ev = _run_traced(r, [cluster], base_dir, tmp_path,
                             [_good("T")], "cfg_bad.jsonl")

        cfgs = [e for e in ev if e.get("kind") == "probe_config"]
        assert len(cfgs) == 1
        assert cfgs[0]["params"]["usable"] == "False"
        assert cfgs[0]["params"]["reason"] == "no_artifact"

    def test_absent_when_feature_is_off(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-7: an old trace and a feature-off trace must stay
        indistinguishable — for a feature that is off, they are."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_enabled="false"), bridge=_FakeBridge(_FACTS))
        r._probe_built = False
        ev = _run_traced(r, [cluster], base_dir, tmp_path,
                         [_good("T")], "cfg_off.jsonl")
        assert [e for e in ev if e.get("kind") == "probe_config"] == []


# ─────────────────────────────────────────────────────────────────────────────
# probe_declined — the four causes the old metric merged
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeDeclined:

    def _declines(self, ev) -> list[dict]:
        return [e for e in ev if e.get("kind") == "probe_declined"]

    def test_round_cap(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-8: this is what all 4 'unresolved' in the real run were."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="1"), bridge=_FakeBridge(_FACTS))
        ev = _run_traced(r, [cluster], base_dir, tmp_path,
                         [_PROBE, _PROBE, _good("Forced")], "d_cap.jsonl")
        d = self._declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "round_cap"
        assert int(d[0]["params"]["round"]) == 1

    def test_unresolved(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-9: collect genuinely had no answer — the ONLY case the old
        'N unresolved' label actually described."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=_FakeBridge({}))  # every lookup misses
        with patch.object(ArchProbe, "execute", return_value=""):
            ev = _run_traced(r, [cluster], base_dir, tmp_path,
                             [_PROBE, _good("Forced")], "d_unres.jsonl")
        d = self._declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "unresolved"

    def test_no_executor(self, cluster_and_base, tmp_path) -> None:
        """AC-P4a-10"""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=None)
        ev = _run_traced(r, [cluster], base_dir, tmp_path,
                         [_PROBE, _good("Forced")], "d_noex.jsonl")
        d = self._declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "no_executor"

    def test_trace_failure_never_breaks_a_batch(
        self, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P4a-15: diagnostics are not control flow.

        Scoped to the probe_* kinds AUTO-P4a introduced. The pre-existing
        llm_request/llm_response events are deliberately left alone — they
        were never wrapped, that is out of this ticket's scope, and widening
        the fault injection to cover them would assert something this patch
        does not claim to have fixed.
        """
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="1"), bridge=_FakeBridge(_FACTS))

        from tools.agent_trace import tracer as _t
        _real = _t.event

        def _boom(*a, **kw):
            if str(kw.get("kind", "")).startswith("probe_"):
                raise RuntimeError("tracer down")
            return _real(*a, **kw)

        monkeypatch.setattr("tools.auto.architect.tracer.event", _boom)
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE, _PROBE, _good("Forced")],
        ):
            out = r.review_clusters([cluster], base_dir, goal="improve code")
        assert [x.title for x in out] == ["Forced"]


# ─────────────────────────────────────────────────────────────────────────────
# analyze_logs.py rendering
# ─────────────────────────────────────────────────────────────────────────────

def _ev(kind: str, **params) -> dict:
    content = params.pop("_content", "")
    return {"run_id": "r1", "kind": kind, "ts": "2026-01-01T00:00:00Z",
            "source": "architect", "target": "probe", "content": content,
            "params": params}


_RUN_START = {"run_id": "r1", "kind": "run_start", "ts": "2026-01-01T00:00:00Z",
              "source": "controller", "target": "auto", "params": {"goal": "g"}}


def _render(events, capsys) -> str:
    analyze_logs.render_run_summary(analyze_logs.analyze(events)["r1"])
    return capsys.readouterr().out


class TestReporting:

    def test_enabled_but_never_used_is_visible(self, capsys) -> None:
        """AC-P4a-11: the whole point of AUTO-P4a's reporting half."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts"),
        ], capsys)
        assert "Architect probes" in out
        assert "0 requests" in out
        assert "never asked" in out

    def test_enabled_but_unavailable_is_visible(self, capsys) -> None:
        """AC-P4a-11: and it must not be blamed on the model."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="False", reason="no_artifact",
                max_rounds=1, max_total_chars=6000, allowed_ops="facts"),
        ], capsys)
        assert "enabled but unavailable" in out
        assert "use_in_auto" in out

    def test_declines_broken_down_by_reason(self, capsys) -> None:
        """AC-P4a-12: 'N unresolved' becomes 'N hit round cap'."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/4)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/4)", round=1, ops=2,
                chars_used=100, run_chars_used=100),
            _ev("probe_request", cluster="agents (batch 1/4)", ops=2),
            _ev("probe_declined", cluster="agents (batch 1/4)", reason="round_cap",
                ops=2, round=1),
            _ev("probe_declined", cluster="io (batch 2/4)", reason="unresolved",
                ops=1, round=0),
        ], capsys)
        assert "2 declined" in out
        assert "hit round cap" in out
        assert "collect had no answer" in out
        assert "unresolved" not in out.replace("collect had no answer", ""), (
            "the bare, ambiguous 'unresolved' label must be gone"
        )

    def test_request_rate_is_computed(self, capsys) -> None:
        """AC-P4a-13: the decision-gate numerator/denominator, so it does not
        have to be reconstructed by hand from the trace."""
        events = [
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/4)", ops=1),
            _ev("probe_result", cluster="agents (batch 1/4)", round=1, ops=1,
                chars_used=50, run_chars_used=50),
        ]
        for i in range(1, 5):
            events.append({
                "run_id": "r1", "kind": "llm_request", "ts": "2026-01-01T00:00:00Z",
                "source": "architect", "target": "llm", "content": "",
                "params": {"cluster": f"agents (batch {i}/4)"},
            })
        out = _render(events, capsys)
        assert "request rate: 25.0% (1/4 batches)" in out

    def test_pre_p4a_trace_still_renders(self, capsys) -> None:
        """AC-P4a-14: traces recorded before this ticket have no
        probe_config and no probe_declined — they must render, without a
        decline line and without crashing."""
        out = _render([
            _RUN_START,
            _ev("probe_request", cluster="agents (batch 1/4)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/4)", round=1, ops=2,
                chars_used=100),
        ], capsys)
        assert "1 request(s)" in out
        assert "declined" not in out

    def test_no_probe_activity_at_all_prints_nothing(self, capsys) -> None:
        """A pre-AUTO-P run, or one with the feature off, is untouched."""
        assert "Architect probes" not in _render([_RUN_START], capsys)
