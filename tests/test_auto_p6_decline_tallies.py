"""tests/test_auto_p6_decline_tallies.py — AUTO-P6: two reporting holes.

**1. `by_op` could not see a fully-missed round.** AUTO-P5 recorded per-op
hit/miss tallies on `probe_result`. But AUTO-P4b made an all-miss round
return an empty digest, and an empty digest emits **no** `probe_result` — it
emits `probe_declined` instead. So every lookup that missed *entirely* was
invisible to the breakdown. A real run reported `facts=23/0` when the true
figure was 23 hits and 17 misses.

With one op that is merely wrong. With two it is worse than useless: it
systematically flatters whichever op tends to miss completely, which is
exactly the comparison the next scope decision (`refs`? `read`?) rests on.
Fixing this before that decision is the point of the ticket.

**2. `test_entry_points_include_main_py_on_real_repo` filtered one test
directory by name.** The `tests_bugfix/` reorganisation moved three files
that `import main` into a directory the filter did not know about, so
`main.py` gained three importers and stopped being an entry point. The
failure message names neither the moved files nor the filter.

  AC-P6-1   An `unresolved` decline carries the executor's real tallies.
  AC-P6-2   A `round_cap` decline records 0/0 — nothing was looked up, so
            the collect artifact must not be blamed for it.
  AC-P6-3   Same for `repeat`, `digest_budget`, `no_executor`.
  AC-P6-4   analyze_logs sums results AND declines.
  AC-P6-5   The end-to-end figure matches reality on a mixed run.
  AC-P6-6   A pre-AUTO-P6 trace (no by_op on declines) still renders.
  AC-P6-7   The entry-points filter covers every test directory.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import analyze_logs
from tools.agent_trace import tracer
from tools.auto.arch_probe import ArchProbe
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


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


class TestDeclineTallies:

    def test_unresolved_decline_carries_real_tallies(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P6-1: the whole hole. This round looked two things up and found
        neither, and before AUTO-P6 that fact reached no report at all."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), _FakeBridge({}))
        ev = _run(r, cluster, base_dir, tmp_path,
                  ["ARCH_PROBE: facts backoff, module tools/nope.py",
                   _good("Forced")], "unres.jsonl")
        d = _declines(ev)
        assert len(d) == 1
        assert d[0]["params"]["reason"] == "unresolved"
        assert d[0]["params"]["by_op"] == "facts=0/1 module=0/1"

    def test_round_cap_decline_records_no_lookup(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P6-2: nothing was looked up here — the harness spent the
        budget. Counting these as misses would blame the collect artifact
        for a decision the harness made."""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(probe_max_rounds="1"), _FakeBridge({"fn": "module: x"}))
        ev = _run(r, cluster, base_dir, tmp_path,
                  ["ARCH_PROBE: facts fn", "ARCH_PROBE: facts fn",
                   _good("Forced")], "cap.jsonl")
        d = _declines(ev)
        assert len(d) == 1 and d[0]["params"]["reason"] == "round_cap"
        assert d[0]["params"]["by_op"] == "facts=0/0"

    def test_no_executor_decline_records_no_lookup(
        self, cluster_and_base, tmp_path
    ) -> None:
        """AC-P6-3"""
        cluster, base_dir = cluster_and_base
        r = _reviewer(_cfg(), None)
        ev = _run(r, cluster, base_dir, tmp_path,
                  ["ARCH_PROBE: facts fn, module tools/x.py", _good("F")],
                  "noex.jsonl")
        d = _declines(ev)
        assert d[0]["params"]["reason"] == "no_executor"
        assert d[0]["params"]["by_op"] == "facts=0/0 module=0/0"


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
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

    def test_declines_are_summed_into_the_breakdown(self, capsys) -> None:
        """AC-P6-4 / AC-P6-5: the shape of the real run this ticket came
        from — `module` resolving everything, `facts` missing a third of the
        time, most of those misses arriving as all-miss declines."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=3,
                max_total_chars=6000, allowed_ops="facts, module"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=2,
                hits=2, misses=0, chars_used=200, run_chars_used=200,
                by_op="module=2/0"),
            _ev("probe_request", cluster="agents (batch 2/2)", ops=3),
            _ev("probe_declined", cluster="agents (batch 2/2)",
                reason="unresolved", ops=3, round=0, by_op="facts=0/3"),
        ], capsys)
        assert "by op:" in out
        assert "module 2/2" in out
        assert "facts 0/3" in out, (
            "an all-miss round emits only a decline; summing results alone "
            "would report facts as flawless"
        )

    def test_pre_p6_trace_still_renders(self, capsys) -> None:
        """AC-P6-6: older traces have no by_op on declines."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts, module"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=2,
                hits=1, misses=1, chars_used=100, by_op="facts=1/1"),
            _ev("probe_declined", cluster="agents (batch 1/2)",
                reason="round_cap", ops=1, round=1),
        ], capsys)
        assert "1/2 symbol(s) found" in out
        assert "hit round cap" in out


def test_entry_points_filter_covers_every_test_directory() -> None:
    """AC-P6-7: guard the filter itself, not just its current output.

    `test_entry_points_include_main_py_on_real_repo` excluded `tests/` by a
    single literal. Moving three `import main` files into `tests_bugfix/`
    made `main.py` stop being an entry point, and the resulting failure
    names neither the moved files nor the filter that missed them. Asserting
    the filter's shape here means the next directory rename fails with a
    message that points at the cause.
    """
    src = Path(__file__).resolve().parent / "test_collect_graph.py"
    text = src.read_text(encoding="utf-8")
    assert "_TEST_DIRS" in text, "the filter must be a named tuple of prefixes"
    for d in ("tests/", "tests_bugfix/", "tests_slow/"):
        assert f'"{d}"' in text, f"{d} missing from the entry-points filter"
