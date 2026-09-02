"""tests/test_auto_p1_arch_probe.py — AUTO-P1: the probe → digest → re-ask
loop inside ClusterReviewer, plus the AUTO-P3 startup lint and the
analyze_logs.py reporting the decision gate is measured with.

Where test_auto_p1_probe_parser.py unit-tests the parser and the executor in
isolation, this file drives the whole loop through ``review_clusters`` with a
patched LLM and a fake CollectBridge, because the properties that matter are
end-to-end: what actually reaches the model on each call, how many calls a
batch costs, and whether the caps hold.

  AC-P1-1   probe_enabled=false → prompt byte-identical to pre-AUTO-P, one call.
  AC-P1-2   A probe reply → a second call carrying "## Probe results".
  AC-P1-3   The re-ask returns JSON → those candidates are used.
  AC-P1-4   Still probing after the round cap → one forced final call carrying
            FORCED_SUFFIX, and no further probe rounds.
  AC-P1-5   The forced call yields nothing → 0 candidates, no exception, and
            the batch is NOT checkpointed.
  AC-P1-6   No collect artifact → PROBE_INSTRUCTIONS never offered, and a
            probe reply is not re-asked into the void.
  AC-P1-7   probe_max_rounds=3 → exactly three digests accumulate, and the
            digest grows monotonically (each round keeps the previous one).
  AC-P1-8   probe_max_total_chars is reached before the round cap and ends
            the loop.
  AC-P1-9   A probe reply that ALSO carried grounded tasks is a complete
            answer — no forced call is spent on it.
  AC-P1-10  Probe events reach the trace with the params analyze_logs reads.
  AC-P1-11  analyze_logs.analyze() counts them and render_summary prints them.
  AC-P1-12  AUTO-P3 lint fires on probe_enabled without collect, and on a
            4k-sized num_ctx, and stays silent on a healthy config.

All LLM calls are patched; no network and no real sleep I/O occurs.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto import arch_probe
from tools.auto.architect import ClusterReviewer
from tools.auto.controller import _lint_probe_config
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**architect_overrides: str) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    architect = {
        "temperature": "0.2",
        "max_tokens": "512",
        "probe_enabled": "true",
        "probe_max_rounds": "1",
        "retry_delays_sec": "",
    }
    architect.update(architect_overrides)
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
            "api_format": "openai",
        },
        "architect": architect,
        "loop":      {"timeout_seconds": "10"},
    })
    return c


class _FakeBridge:
    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}

    def pull_symbol(self, name: str) -> str:
        return self._answers.get(name, "")


def _reviewer(cfg, bridge=None) -> ClusterReviewer:
    r = ClusterReviewer(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )
    # Pre-seed the lazily-built probe so no collect artifact is needed. None
    # models "collect off/absent", which _get_probe returns for real.
    r._probe_built = True
    r._probe = (
        arch_probe.ArchProbe(
            bridge,
            max_chars=cfg.getint("architect", "probe_max_chars", fallback=2000),
            max_total_chars=cfg.getint(
                "architect", "probe_max_total_chars", fallback=6000
            ),
        )
        if bridge is not None
        else None
    )
    return r


@pytest.fixture()
def cluster_and_base(tmp_path: Path) -> tuple[RepoCluster, Path]:
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    cl = RepoCluster(name="agents", patterns=["tools/*"], files=["tools/example.py"])
    return cl, tmp_path


def _task(title: str) -> dict:
    return {
        "title": title,
        "instruction": "Do the fix.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {
            "file": "tools/example.py", "symbol": "fn",
            "line_start": 1, "line_end": 1,
        },
    }


def _good(*titles: str) -> str:
    return json.dumps([_task(t) for t in titles])


_PROBE = "ARCH_PROBE: facts fn"
_FACTS = {"fn": "module: tools/example.py\nsymbol: fn\nsignature: fn()"}


def _msgs(mock_llm) -> list[str]:
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        messages = payload.get("messages", [])
        out.append(next((m["content"] for m in messages if m.get("role") == "user"), ""))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AC-P1-1 / AC-P1-6 — the feature stays invisible when it should
# ─────────────────────────────────────────────────────────────────────────────

class TestOffAndUnavailable:

    def test_disabled_leaves_the_prompt_untouched(self, cluster_and_base) -> None:
        """AC-P1-1: this is the property that lets AUTO-P ship on by default
        nowhere and break nothing anywhere."""
        cluster, base_dir = cluster_and_base
        off = _reviewer(_cfg(probe_enabled="false"), bridge=_FakeBridge(_FACTS))
        with patch(
            "tools.llm_stream.request_completion", return_value=_good("T")
        ) as mock_llm:
            off.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        msg = _msgs(mock_llm)[0]
        assert arch_probe.PROBE_INSTRUCTIONS not in msg
        assert "ARCH_PROBE" not in msg

    def test_no_collect_artifact_never_offers_probing(self, cluster_and_base) -> None:
        """AC-P1-6: instructions are only appended when something can answer."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=None)
        with patch(
            "tools.llm_stream.request_completion", return_value=_good("T")
        ) as mock_llm:
            r.review_clusters([cluster], base_dir, goal="improve code")

        assert "ARCH_PROBE" not in _msgs(mock_llm)[0]

    def test_probe_without_artifact_goes_straight_to_forced(
        self, cluster_and_base
    ) -> None:
        """AC-P1-6: if the model probes anyway, re-asking with an empty digest
        would reproduce the identical reply forever — force instead."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=None)
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE, _good("Forced task")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert arch_probe.FORCED_SUFFIX in _msgs(mock_llm)[1]
        assert [x.title for x in results] == ["Forced task"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-P1-2 / AC-P1-3 — the happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeRoundTrip:

    def test_probe_reply_gets_facts_and_replans(self, cluster_and_base) -> None:
        """AC-P1-2 / AC-P1-3"""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=_FakeBridge(_FACTS))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE, _good("Grounded task")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        first, second = _msgs(mock_llm)
        assert arch_probe.PROBE_INSTRUCTIONS in first
        assert "## Probe results" not in first
        assert "## Probe results" in second
        assert "signature: fn()" in second
        assert arch_probe.FORCED_SUFFIX not in second, (
            "a within-budget re-ask must not be presented as the last chance"
        )
        assert [x.title for x in results] == ["Grounded task"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-P1-4 / AC-P1-5 / AC-P1-7 / AC-P1-8 — the budgets
# ─────────────────────────────────────────────────────────────────────────────

class TestBudgets:

    def test_round_cap_forces_a_final_call(self, cluster_and_base) -> None:
        """AC-P1-4: the model never stops asking; the harness stops it."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="1"), bridge=_FakeBridge(_FACTS))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE, _PROBE, _good("Forced task")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _msgs(mock_llm)
        assert mock_llm.call_count == 3  # probe, re-ask, forced
        assert arch_probe.FORCED_SUFFIX not in msgs[1]
        assert arch_probe.FORCED_SUFFIX in msgs[2]
        assert arch_probe.PROBE_INSTRUCTIONS not in msgs[2]
        assert [x.title for x in results] == ["Forced task"]

    def test_forced_call_still_empty_yields_zero_not_a_crash(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P1-5: and the batch must not be checkpointed, or a bad plan
        phase would be replayed from cache forever."""
        cluster, base_dir = cluster_and_base
        ckpt = tmp_path / "architect_checkpoint.json"
        r = _reviewer(_cfg(probe_max_rounds="1"), bridge=_FakeBridge(_FACTS))
        with patch("tools.llm_stream.request_completion", return_value=_PROBE):
            results = r.review_clusters(
                [cluster], base_dir, goal="improve code", checkpoint_path=ckpt
            )

        assert results == []
        assert not ckpt.exists(), "an empty batch result must not be cached"

    def test_three_rounds_accumulate_monotonically(self, cluster_and_base) -> None:
        """AC-P1-7: each round keeps the previous digest — dropping it would
        make the model re-ask for what it was already told."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({"a": "FACT-A", "b": "FACT-B", "c": "FACT-C"})
        r = _reviewer(_cfg(probe_max_rounds="3"), bridge=bridge)
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[
                "ARCH_PROBE: facts a",
                "ARCH_PROBE: facts b",
                "ARCH_PROBE: facts c",
                _good("Grounded task"),
            ],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _msgs(mock_llm)
        assert mock_llm.call_count == 4
        assert "FACT-A" in msgs[1]
        assert "FACT-A" in msgs[2] and "FACT-B" in msgs[2]
        assert all(f in msgs[3] for f in ("FACT-A", "FACT-B", "FACT-C"))
        assert arch_probe.FORCED_SUFFIX not in msgs[3], (
            "the 3rd round is within a cap of 3 — not the forced call"
        )
        assert [x.title for x in results] == ["Grounded task"]

    def test_total_char_budget_ends_the_loop_before_the_round_cap(
        self, cluster_and_base
    ) -> None:
        """AC-P1-8: the char cap is the one that actually protects the
        window, so it has to win when both are configured."""
        cluster, base_dir = cluster_and_base
        bridge = _FakeBridge({"a": "X" * 900, "b": "Y" * 900, "c": "Z" * 900})
        r = _reviewer(
            _cfg(
                probe_max_rounds="5",
                probe_max_chars="500",
                probe_max_total_chars="500",
                # AUTO-P8 added a budget-escalation ladder that now sits
                # between exhaustion and the forced call. This test is about
                # the CAP ending the loop, not about the ladder, so the ladder
                # is switched off here; AUTO-P8's own tests cover it.
                probe_budget_escalations="0",
            ),
            bridge=bridge,
        )
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[
                "ARCH_PROBE: facts a",
                "ARCH_PROBE: facts b",
                _good("Forced task"),
            ],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 3, "budget must stop this well short of 5 rounds"
        assert arch_probe.FORCED_SUFFIX in _msgs(mock_llm)[2]
        assert [x.title for x in results] == ["Forced task"]

    def test_probe_with_usable_tasks_spends_no_forced_call(
        self, cluster_and_base
    ) -> None:
        """AC-P1-9: a reply that grounded a task has already answered the
        question; another call would buy nothing."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), bridge=_FakeBridge(_FACTS))
        mixed = _good("Kept task") + "\n" + _PROBE
        with patch(
            "tools.llm_stream.request_completion", return_value=mixed
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        assert [x.title for x in results] == ["Kept task"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-P1-10 / AC-P1-11 — the decision gate can actually be measured
# ─────────────────────────────────────────────────────────────────────────────

class TestObservability:

    def test_probe_events_reach_the_trace(self, cluster_and_base, tmp_path) -> None:
        """AC-P1-10: without these, "is this feature used?" is unanswerable
        and the whole Phase 0 gate is unmeasurable."""
        from tools.agent_trace import tracer

        cluster, base_dir = cluster_and_base
        trace_path = tmp_path / "trace.jsonl"
        r = _reviewer(_cfg(), bridge=_FakeBridge(_FACTS))

        tracer.configure(enabled=True, path=str(trace_path), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=[_PROBE, _good("Grounded task")],
            ):
                r.review_clusters([cluster], base_dir, goal="improve code")
        finally:
            tracer.configure(enabled=False)

        assert trace_path.exists(), "tracer wrote no trace file"
        events = [
            json.loads(l) for l in trace_path.read_text().splitlines() if l.strip()
        ]
        kinds = [e.get("kind") for e in events]
        assert "probe_request" in kinds
        assert "probe_result" in kinds

        req = next(e for e in events if e.get("kind") == "probe_request")
        res = next(e for e in events if e.get("kind") == "probe_result")
        # analyze_logs.py reads exactly these keys — assert the contract.
        # agent_trace stringifies every params value (_truncate_params), which
        # is why analyze_logs coerces with int(...) rather than trusting the
        # type it emitted. Assert against the on-disk form, not the intent.
        assert req["params"]["cluster"] == "agents"
        assert int(req["params"]["ops"]) == 1
        assert int(res["params"]["round"]) == 1
        assert int(res["params"]["chars_used"]) > 0

    def test_analyze_logs_counts_and_renders_probes(self, capsys) -> None:
        """AC-P1-11: the reporting path, driven from a synthetic trace so it
        does not depend on a live run."""
        import analyze_logs

        events = [
            {"run_id": "r1", "kind": "run_start", "ts": "2026-01-01T00:00:00Z",
             "source": "controller", "target": "auto", "params": {"goal": "g"}},
            {"run_id": "r1", "kind": "probe_request", "ts": "2026-01-01T00:00:01Z",
             "source": "architect", "target": "probe", "content": "facts fn",
             "params": {"cluster": "agents", "ops": 2}},
            {"run_id": "r1", "kind": "probe_result", "ts": "2026-01-01T00:00:02Z",
             "source": "probe", "target": "architect", "content": "module: x",
             "params": {"cluster": "agents", "round": 1, "ops": 2, "chars_used": 120}},
            {"run_id": "r1", "kind": "probe_request", "ts": "2026-01-01T00:00:03Z",
             "source": "architect", "target": "probe", "content": "facts zzz",
             "params": {"cluster": "io", "ops": 1}},
            # AUTO-P4a: the reason the second request went unanswered is now
            # recorded explicitly instead of being inferred from the
            # requests-minus-results gap. See the assertion note below.
            {"run_id": "r1", "kind": "probe_declined", "ts": "2026-01-01T00:00:04Z",
             "source": "architect", "target": "probe", "content": "facts zzz",
             "params": {"cluster": "io", "reason": "unresolved", "ops": 1,
                        "round": 0}},
        ]
        data = analyze_logs.analyze(events)
        run = data["r1"]

        assert len(run["probe_requests"]) == 2
        assert len(run["probe_results"]) == 1
        assert sum(r["ops"] for r in run["probe_requests"]) == 3

        analyze_logs.render_run_summary(run)
        out = capsys.readouterr().out
        assert "Architect probes" in out
        assert "2 request(s)" in out
        # AUTO-P4a superseded the bare "N unresolved" label this originally
        # asserted. The intent is unchanged and is now served better: the gap
        # is still visible, but attributed to a cause. The old label was
        # actively misleading — it claimed every unanswered request was a
        # collect miss, when on the first real probing run all of them were
        # the round cap and none were collect misses.
        assert "1 declined" in out
        assert "collect had no answer" in out

    def test_analyze_logs_silent_without_probes(self, capsys) -> None:
        """A pre-AUTO-P trace must render exactly as before."""
        import analyze_logs

        events = [
            {"run_id": "r1", "kind": "run_start", "ts": "2026-01-01T00:00:00Z",
             "source": "controller", "target": "auto", "params": {"goal": "g"}},
        ]
        analyze_logs.render_run_summary(analyze_logs.analyze(events)["r1"])
        assert "Architect probes" not in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# AC-P1-12 — AUTO-P3 startup lint
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeConfigLint:

    def _cfg(self, **over) -> configparser.ConfigParser:
        c = configparser.ConfigParser()
        base = {
            "api":       {"active": "local"},
            "api_local": {"num_ctx": "32768"},
            "architect": {"probe_enabled": "true"},
            "collect":   {"use_in_auto": "true"},
        }
        for sec, vals in over.items():
            base.setdefault(sec, {}).update(vals)
        c.read_dict(base)
        return c

    def test_healthy_config_is_silent(self) -> None:
        assert _lint_probe_config(self._cfg()) == []

    def test_probe_without_collect_warns(self) -> None:
        w = _lint_probe_config(self._cfg(collect={"use_in_auto": "false"}))
        assert len(w) == 1 and "use_in_auto" in w[0]

    def test_small_num_ctx_warns(self) -> None:
        w = _lint_probe_config(self._cfg(api_local={"num_ctx": "4096"}))
        assert len(w) == 1 and "num_ctx=4096" in w[0]

    def test_both_traps_warn_independently(self) -> None:
        w = _lint_probe_config(
            self._cfg(collect={"use_in_auto": "false"}, api_local={"num_ctx": "4096"})
        )
        assert len(w) == 2

    def test_probe_disabled_is_always_silent(self) -> None:
        """The lint must not nag about a feature nobody turned on."""
        cfg = self._cfg(
            architect={"probe_enabled": "false"},
            collect={"use_in_auto": "false"},
            api_local={"num_ctx": "4096"},
        )
        assert _lint_probe_config(cfg) == []
        assert _lint_probe_config(None) == []
