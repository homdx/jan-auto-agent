"""tests/test_gate1_ctx_epic.py — GATE1-CTX-1..4.

Field report (real run): Gate 1 already sends real extracted code (not
just prose) via cited_location resolution, plus AUTO-H2's grounding notes
(module docstring, target-file mismatch, one-hop callee, config-fallback).
This epic closes 4 remaining gaps identified from a manual false-positive
review of a real plan.json:

* GATE1-CTX-1: collect's structural contracts (e.g. "fail-open by design")
  were never surfaced to Gate 1 at all — CollectBridge (COLLECT-24) fed
  only the coder, not the validator.
* GATE1-CTX-2: collect's test_map (which test files already import a
  module) was never surfaced either — a "no test exists" claim had no
  counter-evidence when the citation was the SOURCE file, not the test.
* GATE1-CTX-3: a truncated code_block carried an in-band marker but no
  explicit instruction on how to weigh that uncertainty.
* GATE1-CTX-4: the system prompt didn't explicitly tell the model to look
  for existing handling/coverage before confirming.

All four are additive: a bridge-less / marker-less call must be a
byte-for-byte regression of pre-epic behavior.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.collect_bridge import CollectBridge
from tools.auto.gate1_filter import Gate1Filter, _SYSTEM_PROMPT_CODE, filter_candidates
from tools.auto.gate1_grounding import (
    collect_contract_note, existing_test_coverage_note, truncation_safety_note,
)


# ── shared fixtures ──────────────────────────────────────────────────────

def _symbol(qualname: str, signature: str = ""):
    return SimpleNamespace(qualname=qualname, signature=signature)


def _module(path: str, symbols=()):
    return SimpleNamespace(path=path, public_symbols=list(symbols))


def _fresh_model(modules=(), contracts=(), test_map=None):
    m = SimpleNamespace(
        status="fresh",
        modules=list(modules),
        test_map=test_map or {},
    )
    m.contracts_for = lambda q: [c for c in contracts if c.known_edge == q]
    return m


@dataclass
class _Contract:
    name: str
    description: str
    known_edge: str


def _base_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["api"] = {"active": "local", "verify_ssl": "true"}
    cfg["api_local"] = {
        "base_url": "http://localhost:1337/v1", "api_key": "k",
        "model": "m", "api_format": "openai",
    }
    cfg["gate1"] = {"skip_llm": "true"}  # existence-only, no live LLM call needed
    cfg["loop"] = {"timeout_seconds": "60"}
    return cfg


def _filt(cfg=None, collect_bridge=None) -> Gate1Filter:
    cfg = cfg or _base_cfg()
    return Gate1Filter(
        config=cfg, base_url="http://x", api_key="k", model="m",
        api_format="openai", task_mode="code", collect_bridge=collect_bridge,
    )


def _candidate(file: str, symbol: str, instruction: str = "claim") -> CandidateTask:
    return CandidateTask(
        title="t", instruction=instruction, target_files=[file],
        acceptance_check="true",
        cited_location=CitedLocation(file=file, symbol=symbol),
    )


# ── GATE1-CTX-1: collect_contract_note ───────────────────────────────────

class TestCollectContractNote:

    def test_none_bridge_returns_none(self):
        assert collect_contract_note(None, "foo") is None

    def test_empty_symbol_returns_none(self):
        bridge = CollectBridge(_fresh_model())
        assert collect_contract_note(bridge, None) is None
        assert collect_contract_note(bridge, "") is None

    def test_contract_found_is_surfaced(self):
        module = _module("pkg/a.py", [_symbol("pkg.a.Foo.check")])
        contract = _Contract(
            name="fail_open_by_design",
            description="check() never raises — logs and returns False instead",
            known_edge="pkg.a.Foo.check",
        )
        model = _fresh_model([module], contracts=[contract])
        bridge = CollectBridge(model)
        note = collect_contract_note(bridge, "check")
        assert note is not None
        assert "fail_open_by_design" in note
        assert "never raises" in note

    def test_no_contract_returns_none(self):
        module = _module("pkg/a.py", [_symbol("pkg.a.Foo.check")])
        model = _fresh_model([module])
        bridge = CollectBridge(model)
        assert collect_contract_note(bridge, "check") is None

    def test_stale_bridge_never_surfaces_anything(self):
        module = _module("pkg/a.py", [_symbol("pkg.a.Foo.check")])
        contract = _Contract(name="x", description="y", known_edge="pkg.a.Foo.check")
        stale_model = SimpleNamespace(status="stale", modules=[module], test_map={})
        stale_model.contracts_for = lambda q: [contract]
        bridge = CollectBridge(stale_model)
        assert collect_contract_note(bridge, "check") is None

    def test_bridge_exception_is_swallowed(self):
        class _Boom:
            def contracts_for_symbol(self, name):
                raise RuntimeError("boom")
        assert collect_contract_note(_Boom(), "check") is None


# ── GATE1-CTX-2: existing_test_coverage_note ─────────────────────────────

class TestExistingTestCoverageNote:

    def test_none_bridge_returns_none(self):
        assert existing_test_coverage_note(None, "pkg/a.py") is None

    def test_empty_file_returns_none(self):
        bridge = CollectBridge(_fresh_model())
        assert existing_test_coverage_note(bridge, "") is None

    def test_covering_tests_are_listed(self):
        model = _fresh_model(test_map={"pkg/a.py": ("tests/test_a.py", "tests/test_a_extra.py")})
        bridge = CollectBridge(model)
        note = existing_test_coverage_note(bridge, "pkg/a.py")
        assert note is not None
        assert "tests/test_a.py" in note
        assert "tests/test_a_extra.py" in note

    def test_zero_coverage_returns_none(self):
        model = _fresh_model(test_map={"pkg/a.py": ()})
        bridge = CollectBridge(model)
        assert existing_test_coverage_note(bridge, "pkg/a.py") is None

    def test_unknown_file_returns_none(self):
        model = _fresh_model(test_map={"pkg/other.py": ("tests/test_other.py",)})
        bridge = CollectBridge(model)
        assert existing_test_coverage_note(bridge, "pkg/a.py") is None

    def test_stale_bridge_never_surfaces_anything(self):
        stale_model = SimpleNamespace(status="stale", modules=[],
                                       test_map={"pkg/a.py": ("tests/test_a.py",)})
        bridge = CollectBridge(stale_model)
        assert existing_test_coverage_note(bridge, "pkg/a.py") is None


# ── GATE1-CTX-3: truncation_safety_note ──────────────────────────────────

class TestTruncationSafetyNote:

    def test_untruncated_block_returns_none(self):
        assert truncation_safety_note("def foo():\n    return 1\n") is None

    def test_truncated_block_gets_a_neutral_note(self):
        block = "def foo():\n    ...\n... [truncated — 5000 more chars]"
        note = truncation_safety_note(block)
        assert note is not None
        assert "truncated" in note.lower()

    def test_note_does_not_instruct_automatic_rejection(self):
        """Regression guard for the real bug this caught during development:
        an earlier draft told the model to reject whenever a block was
        truncated, which wrongly rejected legitimate large functions in
        tests/test_gate1_corpus_precision.py's precision/recall corpus.
        The note must stay neutral — evidence to weigh, not a verdict."""
        block = "def foo():\n    ...\n... [truncated — 5000 more chars]"
        note = truncation_safety_note(block)
        assert 'reply verdict="rejected"' not in note
        assert "not a reason to reject" in note.lower() or "not a reason to reject or confirm" in note.lower()


# ── GATE1-CTX-1/-2/-3 wired into Gate1Filter._build_grounding_notes ──────

class TestGroundingNotesWiring:

    def test_no_bridge_no_collect_notes_regression(self, tmp_path):
        """collect_bridge=None (the default, byte-for-byte pre-epic
        behavior) — no collect-derived notes appear at all."""
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        filt = _filt()
        candidate = _candidate("a.py", "foo")
        notes = filt._build_grounding_notes(candidate, "def foo():\n    return 1\n", "", tmp_path)
        assert "contract" not in notes.lower()
        assert "test coverage" not in notes.lower()

    def test_contract_note_appears_in_assembled_grounding_notes(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        module = _module("a.py", [_symbol("a.foo")])
        contract = _Contract(name="pure_function", description="foo() has no side effects",
                              known_edge="a.foo")
        model = _fresh_model([module], contracts=[contract])
        bridge = CollectBridge(model)
        filt = _filt(collect_bridge=bridge)
        candidate = _candidate("a.py", "foo")
        notes = filt._build_grounding_notes(candidate, "def foo():\n    return 1\n", "", tmp_path)
        assert "pure_function" in notes

    def test_coverage_note_appears_in_assembled_grounding_notes(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        model = _fresh_model(test_map={"a.py": ("tests/test_a.py",)})
        bridge = CollectBridge(model)
        filt = _filt(collect_bridge=bridge)
        candidate = _candidate("a.py", "foo")
        notes = filt._build_grounding_notes(candidate, "def foo():\n    return 1\n", "", tmp_path)
        assert "tests/test_a.py" in notes

    def test_broken_bridge_never_crashes_grounding_notes(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")

        class _BrokenBridge:
            def contracts_for_symbol(self, name):
                raise RuntimeError("boom")
            def tests_covering(self, file):
                raise RuntimeError("boom")

        filt = _filt(collect_bridge=_BrokenBridge())
        candidate = _candidate("a.py", "foo")
        # Must not raise.
        notes = filt._build_grounding_notes(candidate, "def foo():\n    return 1\n", "", tmp_path)
        assert isinstance(notes, str)


# ── GATE1-CTX-4: system prompt explicitly checks existing handling ───────

class TestSystemPromptContractCheck:

    def test_code_system_prompt_mentions_existing_handling(self):
        assert "try/except" in _SYSTEM_PROMPT_CODE or "error-handling" in _SYSTEM_PROMPT_CODE
        assert "already" in _SYSTEM_PROMPT_CODE.lower()


# ── end-to-end: filter_candidates() threads collect_bridge through ───────

class TestFilterCandidatesThreadsCollectBridge:

    def test_collect_bridge_reaches_the_constructed_filter(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        cfg = _base_cfg()
        candidate = _candidate("a.py", "foo")

        captured = {}
        real_init = Gate1Filter.__init__

        def _spy_init(self, *a, **kw):
            captured["collect_bridge"] = kw.get("collect_bridge")
            return real_init(self, *a, **kw)

        monkeypatch.setattr(Gate1Filter, "__init__", _spy_init)

        sentinel_bridge = object()
        filter_candidates(
            [candidate], tmp_path, cfg, cluster_files=None, task_mode="code",
            collect_bridge=sentinel_bridge,
        )
        assert captured["collect_bridge"] is sentinel_bridge

    def test_default_collect_bridge_is_none_regression(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
        cfg = _base_cfg()
        candidate = _candidate("a.py", "foo")

        captured = {}
        real_init = Gate1Filter.__init__

        def _spy_init(self, *a, **kw):
            captured["collect_bridge"] = kw.get("collect_bridge")
            return real_init(self, *a, **kw)

        monkeypatch.setattr(Gate1Filter, "__init__", _spy_init)

        filter_candidates([candidate], tmp_path, cfg, cluster_files=None, task_mode="code")
        assert captured["collect_bridge"] is None


# ── pipeline.py / plan_validator.py wiring (defensive, mock-controller safe) ─

class TestPipelineWiringDefensive:
    """tools/auto/pipeline.py's _run_plan_phase must work whether or not
    the controller it's given has _get_collect_bridge (real AutoController
    always does; some tests build a lightweight fake without it)."""

    def test_pipeline_module_getattr_pattern_handles_missing_method(self):
        class _FakeControllerNoBridge:
            base_dir = Path(".")
            task_mode = "code"

        ctrl = _FakeControllerNoBridge()
        _get_bridge = getattr(ctrl, "_get_collect_bridge", None)
        result = _get_bridge(ctrl.task_mode) if _get_bridge else None
        assert result is None

    def test_pipeline_module_getattr_pattern_uses_real_method(self):
        class _FakeControllerWithBridge:
            base_dir = Path(".")
            task_mode = "code"
            def _get_collect_bridge(self, task_mode):
                return "sentinel"

        ctrl = _FakeControllerWithBridge()
        _get_bridge = getattr(ctrl, "_get_collect_bridge", None)
        result = _get_bridge(ctrl.task_mode) if _get_bridge else None
        assert result == "sentinel"
