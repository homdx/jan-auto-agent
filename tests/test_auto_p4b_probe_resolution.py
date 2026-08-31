"""tests/test_auto_p4b_probe_resolution.py — AUTO-P4b: the probe could
never resolve anything, and every layer above it reported success.

Three measured runs of the AUTO-P probe produced **60 symbol lookups and 60
misses** — a 100% failure rate — while the run summary reported "8 resolved"
and then "22 resolved". Four separate defects had to line up for that:

  1. **`CollectBridge` never matched a bare symbol name.** Collect writes
     qualnames as ``<path>:<dotted symbol>`` (``tools/llm_stream.py:strip_think``),
     but the matcher treated the whole string as dotted::

         qn == name or qn.endswith("." + name) or qn.split(".")[-1] == name

     ``"tools/llm_stream.py:strip_think".split(".")[-1]`` is
     ``"py:strip_think"``, so a module-level function never matched its own
     name. A *method* matched by accident (the dot in ``.py`` falls left of
     the class dot), and ``Class.method`` never matched at all. Both
     `pull_symbol` and `contracts_for_symbol` carried the bug, so
     ContextBroker Pass 3 and Gate-1 grounding notes were degraded too —
     silently, because every caller fails open on a miss.

  2. **An all-miss round looked like progress.** `execute()` returned a
     digest built entirely of ``(not found)`` lines. Non-empty means "keep
     going" to the loop, so the model re-asked. One real batch asked
     ``facts backoff`` five rounds running and stopped only at the cap.

  3. **Nothing caught a repeated request.** Another batch alternated between
     ``facts _llm_stream, facts strip_think`` and
     ``facts tools.llm_stream, facts tools.llm_stream.strip_think`` for four
     rounds. Defect 2's fix does not cover this: a round that DID resolve
     something still returns a digest.

  4. **"Resolved" counted digests, not symbols.** analyze_logs counted
     `probe_result` events, which is why 60/60 misses rendered as success.

  AC-P4b-1   Bare name matches a module-level function.
  AC-P4b-2   Bare method name matches, and so does Class.method.
  AC-P4b-3   A full path:Qualname still matches exactly.
  AC-P4b-4   A non-existent name still does not match (the fix is not a
             wildcard).
  AC-P4b-5   `pull_symbol` resolves against a realistic collect model.
  AC-P4b-6   `contracts_for_symbol` gets the same fix.
  AC-P4b-7   An all-miss round returns "" so the loop stops.
  AC-P4b-8   A partial-hit round still returns the misses.
  AC-P4b-9   End-to-end: the 5-round `facts backoff` spin costs 2 calls now.
  AC-P4b-10  A repeated op-set ends the loop with reason="repeat".
  AC-P4b-11  A genuinely different follow-up request is NOT treated as a
             repeat.
  AC-P4b-12  probe_result carries hits/misses.
  AC-P4b-13  analyze_logs reports symbols found, and flags a zero-hit run.
  AC-P4b-14  A pre-AUTO-P4b trace (no hit counters) says so instead of
             reporting zero.
  AC-P4b-15  End-to-end happy path: a resolvable symbol reaches the prompt.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import analyze_logs
from tools.agent_trace import tracer
from tools.auto import arch_probe
from tools.auto.arch_probe import ArchProbe, ProbeOp
from tools.auto.architect import ClusterReviewer
from tools.auto.collect_bridge import CollectBridge, _qualname_matches
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Defect 1 — the qualname matcher
# ─────────────────────────────────────────────────────────────────────────────

_FUNC = "tools/llm_stream.py:strip_think"
_METH = "tools/auto/inner_loop.py:InnerLoop.run_task"


class TestQualnameMatcher:

    @pytest.mark.parametrize("name", ["strip_think", _FUNC])
    def test_module_level_function(self, name: str) -> None:
        """AC-P4b-1 / AC-P4b-3: the case that was 100% broken. Every symbol
        the probe asked for in the field was of this shape."""
        assert _qualname_matches(_FUNC, name) is True

    @pytest.mark.parametrize("name", ["run_task", "InnerLoop.run_task", _METH])
    def test_method_forms(self, name: str) -> None:
        """AC-P4b-2: `run_task` used to work by accident; `InnerLoop.run_task`
        never did. Both must work on purpose."""
        assert _qualname_matches(_METH, name) is True

    @pytest.mark.parametrize("name", [
        "request_completion", "InnerLoop", "think", "py", "tools",
        "strip", "llm_stream", "",
    ])
    def test_non_matches(self, name: str) -> None:
        """AC-P4b-4: a suffix-matching fix is one careless `in` away from
        matching everything. `think` must not match `strip_think`, and the
        path components must not match at all."""
        assert _qualname_matches(_FUNC, name) is False

    def test_empty_qualname(self) -> None:
        assert _qualname_matches("", "x") is False
        assert _qualname_matches(_FUNC, "") is False


def _model(*symbols: str, contracts=()):
    """A CollectModel-shaped double: modules → public_symbols → qualname."""
    by_module: dict[str, list] = {}
    for qn in symbols:
        path = qn.split(":", 1)[0]
        by_module.setdefault(path, []).append(
            SimpleNamespace(qualname=qn, signature=f"{qn.split(':')[-1]}(...)")
        )
    return SimpleNamespace(
        modules=[SimpleNamespace(path=p, public_symbols=s)
                 for p, s in by_module.items()],
        contracts_for=lambda qn: [c for c in contracts if c.known_edge == qn],
    )


def _bridge(model) -> CollectBridge:
    b = CollectBridge.__new__(CollectBridge)
    b._model = model
    b.__dict__["usable"] = True
    try:
        object.__setattr__(b, "usable", True)
    except AttributeError:
        pass
    return b


class TestBridgeLookups:

    def test_pull_symbol_resolves_a_bare_name(self, monkeypatch) -> None:
        """AC-P4b-5"""
        b = _bridge(_model(_FUNC, _METH))
        monkeypatch.setattr(type(b), "usable", property(lambda self: True))
        out = b.pull_symbol("strip_think")
        assert "tools/llm_stream.py" in out
        assert "strip_think" in out

    def test_pull_symbol_misses_stay_misses(self, monkeypatch) -> None:
        """AC-P4b-4, at the bridge."""
        b = _bridge(_model(_FUNC))
        monkeypatch.setattr(type(b), "usable", property(lambda self: True))
        assert b.pull_symbol("backoff") == ""

    def test_contracts_for_symbol_gets_the_same_fix(self, monkeypatch) -> None:
        """AC-P4b-6: Gate-1's grounding notes read through this method, so
        the bug degraded Gate-1 too — just as invisibly."""
        c = SimpleNamespace(name="c1", description="d", known_edge=_FUNC)
        b = _bridge(_model(_FUNC, contracts=(c,)))
        monkeypatch.setattr(type(b), "usable", property(lambda self: True))
        assert [x.name for x in b.contracts_for_symbol("strip_think")] == ["c1"]


# ─────────────────────────────────────────────────────────────────────────────
# Defect 2 — an all-miss round is not progress
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBridge:
    usable = True

    def __init__(self, answers: dict | None = None):
        self._answers = answers or {}
        self.calls: list[str] = []

    def pull_symbol(self, name: str) -> str:
        self.calls.append(name)
        return self._answers.get(name, "")


class TestAllMissRound:

    def test_all_miss_returns_empty(self) -> None:
        """AC-P4b-7"""
        p = ArchProbe(_FakeBridge({}))
        assert p.execute([ProbeOp("facts", "backoff")]) == ""
        assert (p.last_hits, p.last_misses) == (0, 1)

    def test_partial_hit_still_reports_the_miss(self) -> None:
        """AC-P4b-8: when the round IS useful, naming what was not found is
        what stops the model asking for it again."""
        p = ArchProbe(_FakeBridge({"strip_think": "module: tools/llm_stream.py"}))
        out = p.execute([ProbeOp("facts", "strip_think"), ProbeOp("facts", "nope")])
        assert "module: tools/llm_stream.py" in out
        assert "(not found)" in out
        assert (p.last_hits, p.last_misses) == (1, 1)

    def test_all_miss_still_charges_the_budget(self) -> None:
        """A model that spends its allowance on names that do not exist must
        not get unlimited retries for free."""
        p = ArchProbe(_FakeBridge({}), max_chars=500, max_total_chars=500)
        p.execute([ProbeOp("facts", "a")])
        assert p.chars_used > 0


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {"temperature": "0.2", "max_tokens": "512", "probe_enabled": "true",
            "probe_max_rounds": "5", "retry_delays_sec": ""}
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
    r._probe = ArchProbe(bridge, max_chars=2000, max_total_chars=6000)
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


def _msgs(mock_llm) -> list[str]:
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        out.append(next((m["content"] for m in payload.get("messages", [])
                         if m.get("role") == "user"), ""))
    return out


class TestLoopNoLongerSpins:

    def test_the_backoff_spin_costs_two_calls(self, cluster_and_base) -> None:
        """AC-P4b-9: the exact field failure. One batch asked `facts backoff`
        five rounds running against a bridge that never had it, burning five
        LLM calls before the cap. With probe_max_rounds=5 it must now cost
        the probe plus one forced call."""
        cluster, base_dir = cluster_and_base

        # A model as stubborn as the one in the field: it would probe forever.
        r = _reviewer(_cfg(probe_max_rounds="5"), _FakeBridge({}))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: facts backoff"] * 8,
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 2, (
            "an all-miss round must end the loop, not license four more; "
            "before AUTO-P4b this same input cost 6 calls (5 rounds + forced)"
        )
        assert arch_probe.FORCED_SUFFIX in _msgs(mock_llm)[1]
        # It kept probing through the forced call too, so the batch legitimately
        # yields nothing — but it does so after 2 calls instead of 6.
        assert results == []

        # And a model that DOES comply on the forced call still gets its plan.
        r2 = _reviewer(_cfg(probe_max_rounds="5"), _FakeBridge({}))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: facts backoff", _good("Forced")],
        ) as mock2:
            out2 = r2.review_clusters([cluster], base_dir, goal="g")
        assert mock2.call_count == 2
        assert [x.title for x in out2] == ["Forced"]

    def test_repeated_request_after_a_hit_is_caught(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P4b-10: the second field spin. The round resolved something, so
        the all-miss fix does not fire; only the repeat detector does."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="5"),
                      _FakeBridge({"strip_think": "module: x"}))
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=["ARCH_PROBE: facts strip_think",
                             "ARCH_PROBE: facts strip_think",
                             _good("Forced")],
            ) as mock_llm:
                r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        assert mock_llm.call_count == 3  # probe, re-ask (repeat), forced
        ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        d = [e for e in ev if e.get("kind") == "probe_declined"]
        assert len(d) == 1 and d[0]["params"]["reason"] == "repeat"

    def test_a_different_follow_up_is_not_a_repeat(self, cluster_and_base) -> None:
        """AC-P4b-11: the detector must not break the feature it protects —
        refining a request is exactly what probing is for."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="5"),
                      _FakeBridge({"a": "module: A", "b": "module: B"}))
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: facts a", "ARCH_PROBE: facts b",
                         _good("Grounded")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="g")

        assert mock_llm.call_count == 3
        msgs = _msgs(mock_llm)
        assert "module: A" in msgs[1]
        assert "module: A" in msgs[2] and "module: B" in msgs[2]
        assert arch_probe.FORCED_SUFFIX not in msgs[2]
        assert [x.title for x in results] == ["Grounded"]

    def test_happy_path_reaches_the_prompt(self, cluster_and_base, tmp_path) -> None:
        """AC-P4b-15 / AC-P4b-12: the thing that has never once happened in a
        real run — a resolved fact arriving in the re-ask."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="1"),
                      _FakeBridge({"strip_think": "module: tools/llm_stream.py\n"
                                                  "signature: strip_think(text)"}))
        tp = tmp_path / "t.jsonl"
        tracer.configure(enabled=True, path=str(tp), console_echo=False)
        try:
            with patch(
                "tools.llm_stream.request_completion",
                # THREE ops, ONE of which resolves. The asymmetry is the point:
                # with ops == hits, a report that echoed the request count back
                # instead of the executor's own tally would look correct.
                side_effect=["ARCH_PROBE: facts strip_think, facts backoff, facts retry",
                             _good("Grounded")],
            ) as mock_llm:
                results = r.review_clusters([cluster], base_dir, goal="g")
        finally:
            tracer.configure(enabled=False)

        prompt = _msgs(mock_llm)[1]
        assert "signature: strip_think(text)" in prompt
        assert "(not found)" in prompt, (
            "a partial-hit round still names what it could not resolve"
        )
        assert [x.title for x in results] == ["Grounded"]

        ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
        res = next(e for e in ev if e.get("kind") == "probe_result")
        assert int(res["params"]["ops"]) == 3
        assert int(res["params"]["hits"]) == 1, (
            "hits must come from the executor's tally, not from the number of "
            "ops requested — that substitution is exactly how 60/60 misses "
            "were reported as successes"
        )
        assert int(res["params"]["misses"]) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Defect 4 — honest reporting
# ─────────────────────────────────────────────────────────────────────────────

_RUN_START = {"run_id": "r1", "kind": "run_start", "ts": "2026-01-01T00:00:00Z",
              "source": "controller", "target": "auto", "params": {"goal": "g"}}


def _ev(kind: str, **params) -> dict:
    return {"run_id": "r1", "kind": kind, "ts": "2026-01-01T00:00:00Z",
            "source": "architect", "target": "probe", "content": "",
            "params": params}


def _render(events, capsys) -> str:
    analyze_logs.render_run_summary(analyze_logs.analyze(events)["r1"])
    return capsys.readouterr().out


class TestReporting:

    def test_zero_hits_is_called_out(self, capsys) -> None:
        """AC-P4b-13: the headline defect. Two real runs reported 60/60
        misses as "8 resolved" and "22 resolved"."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=5,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=2,
                hits=0, misses=2, chars_used=60, run_chars_used=60),
        ], capsys)
        assert "0/2 symbol(s) found" in out
        assert "collect resolved nothing" in out
        assert "resolved (max round" not in out, (
            "the old event-counting phrasing must be gone"
        )

    def test_real_hits_are_reported(self, capsys) -> None:
        """AC-P4b-13"""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=5,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=3),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=3,
                hits=2, misses=1, chars_used=200, run_chars_used=200),
        ], capsys)
        assert "2/3 symbol(s) found" in out
        assert "collect resolved nothing" not in out

    def test_pre_p4b_trace_says_counts_are_missing(self, capsys) -> None:
        """AC-P4b-14: an old trace has no hit counters. Reporting "0 found"
        for it would repeat the original sin in the opposite direction."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=2,
                chars_used=60),
        ], capsys)
        assert "hit counts not recorded" in out
        assert "symbol(s) found" not in out

    def test_repeat_decline_has_a_label(self, capsys) -> None:
        """AC-P4b-10, in the report."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=5,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=1),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=1,
                hits=1, misses=0, chars_used=60, run_chars_used=60),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=1),
            _ev("probe_declined", cluster="agents (batch 1/2)", reason="repeat",
                ops=1, round=1),
        ], capsys)
        assert "re-asked an answered probe" in out
