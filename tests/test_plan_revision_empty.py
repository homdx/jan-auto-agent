"""AUTO-FIX — an empty plan revision must not wipe the plan.

`plan_phase` re-runs the architect when `validate_plan` says REVISE. The
re-run's result was assigned unconditionally, so an empty answer replaced a
good plan with nothing — while the *exception* path right above it carefully
kept the previous candidates. That asymmetry cost a whole run.

Observed live on the creative flow: two candidates, `validate_plan: REVISE —
Duplicate task`, the architect answered `[]`, and the run finished with zero
tasks in twelve seconds. Deduplicating two identical tasks should leave one,
never none — and Gate 1's own duplicate check would have removed the overlap
anyway.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.auto.pipeline as pipeline


class _Validator:
    """REVISE once, then accept whatever it is given."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)

    def validate_plan(self, _goal, _candidates):
        return self._verdicts.pop(0) if self._verdicts else (True, "")


def _controller(tmp_path: Path):
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        goal="write a changelog entry",
        base_dir=str(tmp_path),
        task_mode="creative",
        state=SimpleNamespace(agent_dir=agent_dir, log=lambda *_a, **_k: None),
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Drive just the revision loop, with the architect and validator stubbed."""
    calls: list[str] = []

    def _make(review_results):
        results = list(review_results)

        def _review_clusters(*_a, **kwargs):
            calls.append(kwargs.get("goal", ""))
            return results.pop(0)

        monkeypatch.setattr(pipeline, "review_clusters", _review_clusters)
        return calls

    return _make


def _run_revision_loop(cfg, controller, candidates, validator, monkeypatch):
    """Execute the revision loop the way plan_phase does."""
    monkeypatch.setattr(pipeline, "_build_plan_validator", lambda *_a, **_k: validator)
    return pipeline_revision_shim(cfg, controller, candidates)


def pipeline_revision_shim(cfg, controller, candidates):
    """Extracted mirror of plan_phase's revision block, kept honest by
    test_shim_matches_pipeline_source below."""
    validator = pipeline._build_plan_validator(cfg, controller.task_mode)
    if validator is None:
        return candidates
    max_rev = max(1, cfg.getint("architect", "plan_max_revisions", fallback=1))
    revisions = 0
    while revisions < max_rev:
        ok, reason = validator.validate_plan(controller.goal, candidates)
        if ok:
            break
        revisions += 1
        revised = pipeline.review_clusters(
            [], controller.base_dir, cfg,
            goal=controller.goal + f"\n\nPLAN FEEDBACK: {reason}",
            task_mode=controller.task_mode, checkpoint_path=None,
        )
        if not revised and candidates:
            break
        candidates = revised
    return candidates


def _cfg(max_rev: int = 1) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(f"[architect]\nplan_max_revisions = {max_rev}\n")
    return cfg


# ── the bug ──────────────────────────────────────────────────────────────────

def test_empty_revision_keeps_previous_candidates(tmp_path, harness, monkeypatch):
    """Verbatim reproduction: two duplicates in, `[]` back, must not be zero."""
    harness([[]])
    original = [{"title": "Add changelog entry"}, {"title": "Add changelog entry"}]
    result = _run_revision_loop(
        _cfg(), _controller(tmp_path), original,
        _Validator([(False, "Duplicate task")]), monkeypatch,
    )
    assert result == original, "an empty revision wiped the plan"


def test_non_empty_revision_replaces_candidates(tmp_path, harness, monkeypatch):
    """The fix must not freeze the plan — a real revision still applies."""
    revised = [{"title": "Add changelog entry"}]
    harness([revised])
    result = _run_revision_loop(
        _cfg(), _controller(tmp_path), [{"title": "a"}, {"title": "b"}],
        _Validator([(False, "Duplicate task")]), monkeypatch,
    )
    assert result == revised


def test_empty_original_and_empty_revision_stays_empty(tmp_path, harness, monkeypatch):
    """Nothing to preserve — must not invent a plan."""
    harness([[]])
    result = _run_revision_loop(
        _cfg(), _controller(tmp_path), [],
        _Validator([(False, "no tasks")]), monkeypatch,
    )
    assert result == []


def test_approved_plan_is_not_revised(tmp_path, harness, monkeypatch):
    calls = harness([])
    original = [{"title": "a"}]
    result = _run_revision_loop(
        _cfg(), _controller(tmp_path), original, _Validator([(True, "")]), monkeypatch,
    )
    assert result == original
    assert calls == [], "the architect was re-run for an approved plan"


def test_feedback_reaches_the_architect(tmp_path, harness, monkeypatch):
    calls = harness([[{"title": "fixed"}]])
    _run_revision_loop(
        _cfg(), _controller(tmp_path), [{"title": "a"}],
        _Validator([(False, "Duplicate task")]), monkeypatch,
    )
    assert calls and "Duplicate task" in calls[0]


# ── the shim must not drift from the real code ───────────────────────────────

def test_shim_matches_pipeline_source():
    """This file tests a mirror of plan_phase's revision block, so the mirror
    must stay recognisably the same code. Pin the guard, not the whole body."""
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "if not revised and candidates:" in source
    assert "revised = review_clusters(" in source
    assert "candidates = revised" in source


def test_pipeline_logs_the_preservation():
    """A silently preserved plan looks like the revision did nothing."""
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "EMPTY plan" in source
