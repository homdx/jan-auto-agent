"""tests/test_auto_p5_module_probe.py — AUTO-P5: the `module` probe op.

`facts <symbol>` answers "what is this symbol". Across two measured probe
runs, **7 of the 9 unresolved lookups were `facts backoff` or `facts retry`**
— a *file* (`tools/backoff.py`, which defines six functions but no symbol
called `backoff`) and a *concept* that is not an identifier anywhere in the
tree. `pull_symbol` was right to return nothing both times; the Architect had
no way to express the question it actually had, which was "what is in this
file".

AUTO-P4b's response was to forbid those shapes in PROBE_INSTRUCTIONS. That
stopped them looking like a resolver bug but left the need unmet: naming a
restriction does not help when the thing you need cannot be expressed at all.
`module <path>` makes it expressible.

  AC-P5-1   `module tools/backoff.py` lists all six symbols, in source order.
  AC-P5-2   Dotted (`tools.backoff`) and extension-less (`tools/backoff`)
            forms resolve identically to the exact path.
  AC-P5-3   An unknown path misses — and misses honestly, with no
            "closest match" fallback.
  AC-P5-4   A module with no symbols is DISTINCT from a miss.
  AC-P5-5   The listing carries line numbers and docstrings, not just names.
  AC-P5-6   A huge module is cut on a symbol boundary, and says so.
  AC-P5-7   `probe_allowed_ops` gates the op at parse time, no code change.
  AC-P5-8   A mixed `facts X, module Y` request parses as two ops.
  AC-P5-9   `module` shares the per-op char cap with `facts`.
  AC-P5-10  End-to-end: a module inventory reaches the re-ask prompt.
  AC-P5-11  Per-op tallies are recorded and traced (`facts=1/0 module=1/0`).
  AC-P5-12  analyze_logs reports the per-op breakdown once >1 op is in play.
  AC-P5-13  The `facts` path is untouched.
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
from tools.auto.arch_probe import ArchProbe, ProbeOp, extract_probe_request
from tools.auto.architect import ClusterReviewer
from tools.auto.collect_bridge import CollectBridge
from tools.auto.repo_ingest import RepoCluster

_OPS = ("facts", "module")

# The real shape of tools/backoff.py in a collect artifact — the module whose
# absence motivated this ticket. Signatures are elided to `name(...)` by
# collect itself, which is why the listing leans on lineno and docstring.
_BACKOFF = [
    ("backoff_seconds", 45, "Return wait time (s) for the nth consecutive API error (0-indexed)."),
    ("_now", 60, ""),
    ("save_state", 67, "Persist retry state atomically."),
    ("load_state", 114, ""),
    ("clear_state", 140, ""),
    ("sleep_with_interrupt_save", 147, "Sleep, saving state if interrupted."),
]


def _sym(path: str, name: str, lineno: int = 1, doc: str = ""):
    return SimpleNamespace(
        qualname=f"{path}:{name}", signature=f"{name}(...)",
        lineno=lineno, docstring_first_line=doc, module=path,
    )


def _model(modules: dict):
    return SimpleNamespace(
        modules=[SimpleNamespace(path=p, public_symbols=s) for p, s in modules.items()],
        contracts_for=lambda qn: [],
    )


def _bridge(modules: dict, monkeypatch) -> CollectBridge:
    b = CollectBridge.__new__(CollectBridge)
    b._model = _model(modules)
    monkeypatch.setattr(type(b), "usable", property(lambda self: True))
    return b


@pytest.fixture()
def backoff_bridge(monkeypatch) -> CollectBridge:
    return _bridge({
        "tools/backoff.py": [
            _sym("tools/backoff.py", n, ln, d) for n, ln, d in _BACKOFF
        ],
        "tools/llm_stream.py": [_sym("tools/llm_stream.py", "strip_think", 12)],
    }, monkeypatch)


# ─────────────────────────────────────────────────────────────────────────────
# CollectBridge.module_symbols
# ─────────────────────────────────────────────────────────────────────────────

class TestModuleSymbols:

    def test_exact_path_lists_every_symbol_in_order(self, backoff_bridge) -> None:
        """AC-P5-1: the exact lookup the field runs needed and could not make."""
        out = backoff_bridge.module_symbols("tools/backoff.py")
        assert out.startswith("module: tools/backoff.py")
        names = [n for n, _, _ in _BACKOFF]
        for n in names:
            assert n in out
        # Source order, matching public_symbols — same guarantee pull_symbol gives.
        positions = [out.index(n) for n in names]
        assert positions == sorted(positions)

    @pytest.mark.parametrize("ref", ["tools.backoff", "tools/backoff", "tools/backoff.py"])
    def test_all_three_reference_forms_agree(self, backoff_bridge, ref: str) -> None:
        """AC-P5-2: the model writes module references three ways and should
        not have to guess which one the harness accepts."""
        assert backoff_bridge.module_symbols(ref) == \
            backoff_bridge.module_symbols("tools/backoff.py")

    @pytest.mark.parametrize("ref", [
        "tools/nonexistent.py", "backoff", "tools", "tools/backoff.pyc", "",
    ])
    def test_misses_are_honest(self, backoff_bridge, ref: str) -> None:
        """AC-P5-3: exact match only. `backoff` alone must NOT resolve to
        tools/backoff.py — unlike pull_symbol there is deliberately no
        suffix fallback, because answering with a neighbouring module while
        looking successful is worse than answering nothing."""
        assert backoff_bridge.module_symbols(ref) == ""

    def test_no_suffix_fallback_when_two_modules_share_a_basename(
        self, monkeypatch
    ) -> None:
        """AC-P5-3, the case that actually proves the rule.

        A bare-basename fallback looks harmless until two files share one.
        `module backoff.py` would then resolve to whichever module the
        iteration reached first, and the Architect would plan against the
        wrong file while the telemetry recorded a successful lookup — the
        exact failure class AUTO-P4b spent a whole ticket removing.
        """
        b = _bridge({
            "tools/backoff.py":  [_sym("tools/backoff.py", "backoff_seconds", 45)],
            "vendor/backoff.py": [_sym("vendor/backoff.py", "other_thing", 3)],
        }, monkeypatch)
        assert b.module_symbols("backoff.py") == ""
        # Fully qualified, both still resolve — and to different things.
        assert "backoff_seconds" in b.module_symbols("tools/backoff.py")
        assert "other_thing" in b.module_symbols("vendor/backoff.py")

    def test_empty_module_is_not_a_miss(self, monkeypatch) -> None:
        """AC-P5-4: "the file exists and is empty" and "no such file" are
        different facts, and the telemetry must not merge them."""
        b = _bridge({"tools/empty.py": []}, monkeypatch)
        out = b.module_symbols("tools/empty.py")
        assert out != ""
        assert "module: tools/empty.py" in out
        assert "no public top-level symbols" in out

    def test_listing_carries_line_numbers_and_docstrings(self, backoff_bridge) -> None:
        """AC-P5-5: collect elides signatures to `name(...)`, so a name+sig
        listing would tell the model nothing it did not already know. The
        line number also lets the Architect emit a real
        cited_location.line_start instead of guessing one — itself a Gate-1
        rejection reason."""
        out = backoff_bridge.module_symbols("tools/backoff.py")
        lines = out.splitlines()
        # Exact lines, not substrings. collect stores `signature` as
        # "name(...)" rather than "(...)", and a naive name+signature concat
        # renders "backoff_secondsbackoff_seconds(...)" — which a substring
        # assertion happily accepts, because the doubled text still contains
        # the expected fragment. Only a whole-line comparison catches it, and
        # only a live artifact exposed it in the first place.
        assert lines[1] == (
            "  backoff_seconds(...) :45 — Return wait time (s) for the nth "
            "consecutive API error (0-indexed)."
        )
        # A symbol without a docstring still lists cleanly.
        assert lines[2] == "  _now(...) :60"

    def test_large_module_is_cut_on_a_symbol_boundary(self, monkeypatch) -> None:
        """AC-P5-6: the largest module in this tree has 65 symbols. Byte-level
        truncation would leave a partial list the model cannot tell is
        partial; cutting on a boundary and saying so is the difference
        between "some of the file" and a silent lie."""
        many = [_sym("big.py", f"sym_{i}", i) for i in range(65)]
        b = _bridge({"big.py": many}, monkeypatch)
        out = b.module_symbols("big.py")
        limit = CollectBridge._MODULE_SYMBOL_LIMIT
        assert f"and {65 - limit} more symbol(s) not listed" in out
        assert f"sym_{limit - 1}(" in out
        assert f"sym_{limit}(" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Parser gating — no code change needed, per the ticket
# ─────────────────────────────────────────────────────────────────────────────

class TestAllowListGating:

    def test_module_is_rejected_when_not_allowed(self) -> None:
        """AC-P5-7: with the shipped default allow-list the op does not exist,
        so the reply is "not a probe" — exactly like any other unknown op,
        with no new code path."""
        assert extract_probe_request("ARCH_PROBE: module tools/backoff.py") == []
        assert extract_probe_request(
            "ARCH_PROBE: module tools/backoff.py", allowed_ops=("facts",)
        ) == []

    def test_module_parses_when_allowed(self) -> None:
        """AC-P5-7"""
        assert extract_probe_request(
            "ARCH_PROBE: module tools/backoff.py", allowed_ops=_OPS
        ) == [ProbeOp("module", "tools/backoff.py")]

    def test_mixed_request_parses_as_two_ops(self) -> None:
        """AC-P5-8: extract_probe_request is already op-agnostic per item —
        dedup, order and cap need no change for a mixed request."""
        out = extract_probe_request(
            "ARCH_PROBE: facts InnerLoop, module tools/backoff.py, facts InnerLoop",
            allowed_ops=_OPS,
        )
        assert out == [
            ProbeOp("facts", "InnerLoop"),
            ProbeOp("module", "tools/backoff.py"),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# ArchProbe integration
# ─────────────────────────────────────────────────────────────────────────────

class TestArchProbeModuleOp:

    def test_executes_and_counts_per_op(self, backoff_bridge) -> None:
        """AC-P5-11 / AC-P5-13: `module` runs alongside `facts`, and the
        tallies are kept per op — aggregate hits cannot tell you whether a
        newly added op is earning its round-trip."""
        p = ArchProbe(backoff_bridge, max_chars=4000, max_total_chars=8000)
        out = p.execute([
            ProbeOp("facts", "strip_think"),
            ProbeOp("module", "tools/backoff.py"),
            ProbeOp("module", "tools/nope.py"),
        ])
        assert "symbol: tools/llm_stream.py:strip_think" in out
        assert "module: tools/backoff.py" in out
        assert "(not found)" in out
        assert p.last_hits == 2 and p.last_misses == 1
        assert p.last_by_op == {"facts": [1, 0], "module": [1, 1]}
        assert p.last_by_op_str() == "facts=1/0 module=1/1"

    def test_module_respects_the_per_op_cap(self, monkeypatch) -> None:
        """AC-P5-9: same budget machinery as facts, no separate path."""
        many = [_sym("big.py", f"symbol_number_{i}", i, "x" * 40) for i in range(40)]
        b = _bridge({"big.py": many}, monkeypatch)
        p = ArchProbe(b, max_chars=400, max_total_chars=4000)
        out = p.execute([ProbeOp("module", "big.py")])
        assert "truncated" in out
        assert p.last_hits == 1

    def test_all_module_misses_still_end_the_loop(self, monkeypatch) -> None:
        """AC-P5-3 + AUTO-P4b: a miss on the new op behaves exactly like a
        miss on facts — empty digest, so the caller forces a final call."""
        b = _bridge({"tools/x.py": [_sym("tools/x.py", "f")]}, monkeypatch)
        p = ArchProbe(b)
        assert p.execute([ProbeOp("module", "tools/nope.py")]) == ""
        assert p.last_by_op == {"module": [0, 1]}


# ─────────────────────────────────────────────────────────────────────────────
# End to end
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(ops: str = "facts, module") -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": {"temperature": "0.2", "max_tokens": "512",
                      "probe_enabled": "true", "probe_max_rounds": "2",
                      "probe_allowed_ops": ops, "retry_delays_sec": ""},
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


def test_module_inventory_reaches_the_reask(
    cluster_and_base, backoff_bridge, tmp_path
) -> None:
    """AC-P5-10 / AC-P5-11: the whole point — the Architect asks for the file
    it knows it needs, and gets its contents back in the next prompt."""
    cluster, base_dir = cluster_and_base
    r = ClusterReviewer(config=_cfg(), base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    r._probe = ArchProbe(backoff_bridge, max_chars=4000, max_total_chars=8000)

    tp = tmp_path / "t.jsonl"
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: module tools/backoff.py", _good("Grounded")],
        ) as mock_llm:
            results = r.review_clusters([cluster], base_dir, goal="dedupe retries")
    finally:
        tracer.configure(enabled=False)

    reask = _msgs(mock_llm)[1]
    assert "module: tools/backoff.py" in reask
    assert "backoff_seconds(...)" in reask
    assert "sleep_with_interrupt_save" in reask
    assert [x.title for x in results] == ["Grounded"]

    ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    res = next(e for e in ev if e.get("kind") == "probe_result")
    assert int(res["params"]["hits"]) == 1
    assert res["params"]["by_op"] == "module=1/0"


def test_instructions_teach_the_new_op(cluster_and_base, backoff_bridge) -> None:
    """The op is useless if the model is never told it exists — and AUTO-P4b's
    old text actively forbade the shape this ticket now supports."""
    assert "module <path>" in arch_probe.PROBE_INSTRUCTIONS
    assert "tools/backoff.py" in arch_probe.PROBE_INSTRUCTIONS
    assert "a module or import path" not in arch_probe.PROBE_INSTRUCTIONS, (
        "the AUTO-P4b bullet forbidding module references must be gone"
    )


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

    def _events(self, by_op: str):
        return [
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=2,
                max_total_chars=6000, allowed_ops="facts, module"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=4),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=4,
                hits=3, misses=1, chars_used=300, run_chars_used=300,
                by_op=by_op),
        ]

    def test_breakdown_when_more_than_one_op(self, capsys) -> None:
        """AC-P5-12: which op is pulling its weight is the number the next
        scope decision (refs? read?) turns on."""
        out = _render(self._events("facts=1/1 module=2/0"), capsys)
        assert "by op:" in out
        assert "module 2/2" in out
        assert "facts 1/2" in out

    def test_no_breakdown_for_a_single_op(self, capsys) -> None:
        """With one op the line just restates the aggregate."""
        out = _render(self._events("facts=3/1"), capsys)
        assert "by op:" not in out
        assert "3/4 symbol(s) found" in out

    def test_pre_p5_trace_has_no_breakdown(self, capsys) -> None:
        """AC-P5-12: traces from before this ticket carry no by_op field and
        must render without one rather than inventing zeros."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="True", reason="ok", max_rounds=1,
                max_total_chars=6000, allowed_ops="facts"),
            _ev("probe_request", cluster="agents (batch 1/2)", ops=2),
            _ev("probe_result", cluster="agents (batch 1/2)", round=1, ops=2,
                hits=2, misses=0, chars_used=100),
        ], capsys)
        assert "by op:" not in out
        assert "2/2 symbol(s) found" in out
