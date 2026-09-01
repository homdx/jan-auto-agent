"""tests/test_prompt_evaluator_shadow_iter_score.py

Regression tests for the third and last instance of the "_api_error
distorts a shadow-score sub-term" bug class in
tools/prompt_evaluator.py's PromptEvaluator._shadow_score():

  - json_ok_rate (35% weight) — fixed previously.
  - approved_rate (25% weight) — fixed by three-bugfixes.patch.
  - iter_score (40% weight) — fixed here.

An _api_error result never reached judgment (the shadow call failed
before a verdict came back), but its dict default status="needs_fix"
used to fall into iter_score's `else: 2` branch, scoring it as a
genuine full second revision iteration. iter_score carries the
largest weight of the three sub-scores, so this was the largest
single-call distortion of the three.

These tests exercise the real PromptEvaluator._shadow_score() method
against a mocked ValidatorAgent (matching the JIRA-ticket repro
methodology), not a reimplementation of the formula.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.metrics_collector import RunRecord
from tools.prompt_evaluator import PromptEvaluator


def _evaluator(validator_agent, metrics_collector=None, max_iter=3):
    return PromptEvaluator(
        prompt_store=MagicMock(),
        metrics_collector=metrics_collector or MagicMock(),
        validator_agent=validator_agent,
        max_iter=max_iter,
    )


def _agent(results):
    """A stand-in ValidatorAgent whose validate() returns `results` in
    order, one per call — matching the 3-payload shadow loop."""
    va = MagicMock()
    va.validate = MagicMock(side_effect=results)
    va.prompt_store = MagicMock()
    va.max_iter = 3
    return va


class TestIterScoreExcludesApiError:
    def test_happy_path_all_approved_no_api_error_unchanged(self):
        """No _api_error at all: iter_score must be unaffected by this
        fix (no regression on the path three-bugfixes.patch already
        covered correctly)."""
        results = [{"status": "approved"} for _ in range(3)]
        score = _evaluator(_agent(results))._shadow_score("validator_agent", "prompt")
        assert score == 1.0

    def test_happy_path_mixed_verdicts_no_api_error_unchanged(self):
        """Mixed approved/needs_fix, zero _api_error: matches the
        pre-fix formula exactly, since there is nothing to exclude."""
        results = [
            {"status": "approved"},
            {"status": "approved"},
            {"status": "needs_fix"},
        ]
        score = _evaluator(_agent(results))._shadow_score("validator_agent", "prompt")
        # avg_iter = (1+1+2)/3 = 4/3; iter_score = 1-((4/3-1)/2) = 5/6
        # json_ok_rate = 1.0; approved_rate = 2/3
        expected = round(0.40 * (5 / 6) + 0.35 * 1.0 + 0.25 * (2 / 3), 4)
        assert score == expected

    def test_api_error_excluded_matches_error_free_equivalent(self):
        """The ticket's own repro: 2/3 approved, 1 _api_error. Must score
        identically to a clean 2-good-call run (1.0), not the pre-fix
        0.9333 that blamed the prompt for an infra failure."""
        results = [
            {"status": "approved"},
            {"status": "approved"},
            {"status": "needs_fix", "_api_error": True},
        ]
        score = _evaluator(_agent(results))._shadow_score("validator_agent", "prompt")
        assert score == 1.0, (
            f"expected 1.0 (matches a 2/2-approved error-free run) — got "
            f"{score}, which would mean the _api_error call is still "
            f"being counted as a genuine second-iteration verdict"
        )

    def test_api_error_worst_case_matches_needs_fix_only_equivalent(self):
        """1 approved + 2 _api_error must score iter_score identically to
        a single-call run that was simply approved — not as if two real
        worst-case (needs_fix) iterations had occurred."""
        results = [
            {"status": "approved"},
            {"status": "needs_fix", "_api_error": True},
            {"status": "needs_fix", "_api_error": True},
        ]
        score = _evaluator(_agent(results))._shadow_score("validator_agent", "prompt")
        assert score == 1.0

    def test_unparseable_still_counted_only_api_error_excluded(self):
        """_unparseable is a reply that DID arrive — it must still count
        toward iter_score (as a needs_fix-equivalent iteration), unlike
        _api_error which is excluded entirely."""
        results = [
            {"status": "approved"},
            {"status": "needs_fix", "_unparseable": True},
            {"status": "needs_fix", "_api_error": True},
        ]
        score = _evaluator(_agent(results))._shadow_score("validator_agent", "prompt")
        # _judged = [approved, unparseable] (api_error excluded) → n=2
        # avg_iter = (1+2)/2 = 1.5 → iter_score = 1-((0.5)/2) = 0.75
        # json_ok_rate = 1/2 (the unparseable one fails it) = 0.5
        # approved_rate = 1/2 = 0.5
        expected = round(0.40 * 0.75 + 0.35 * 0.5 + 0.25 * 0.5, 4)
        assert score == expected

    def test_fully_degenerate_all_api_error_falls_back_to_projection(self):
        """Every shadow call returns _api_error: there is no reply at all
        to judge. Per the ticket's recorded decision, fall back to
        _projected_score() instead of scoring a term built from zero
        real signal."""
        results = [{"status": "needs_fix", "_api_error": True} for _ in range(3)]

        metrics_collector = MagicMock()
        metrics_collector.load_recent.return_value = [
            RunRecord(
                timestamp="2026-08-27T00:00:00",
                intent="t",
                prompt_version="v1",
                iterations_used=1,
                validator_status="approved",
                validator_feedback="",
                improvement_json_ok=True,
                elapsed_seconds=1.0,
            )
        ]

        evaluator = _evaluator(_agent(results), metrics_collector=metrics_collector)
        expected = evaluator._projected_score()
        # A fresh, identically-configured evaluator (metrics_collector is
        # a MagicMock, safe to call load_recent() again — it's not
        # exhausted like the validate() side_effect list) confirms the
        # fallback path returns exactly what direct projection returns.
        score = evaluator._shadow_score("validator_agent", "prompt")
        assert score == expected
        assert score == 1.0  # single healthy baseline record → perfect projection

    def test_fully_degenerate_does_not_score_as_zero_or_worst_case(self):
        """Guards against a naive alternative fix that scores the
        degenerate case as 0.0 directly instead of consulting the
        projection fallback."""
        results = [{"status": "needs_fix", "_api_error": True} for _ in range(3)]
        metrics_collector = MagicMock()
        metrics_collector.load_recent.return_value = []  # no baseline → projection is 0.0 by its own contract

        evaluator = _evaluator(_agent(results), metrics_collector=metrics_collector)
        score = evaluator._shadow_score("validator_agent", "prompt")
        # With no baseline runs either, projection itself is 0.0 — but the
        # important assertion is *why*: load_recent must actually have
        # been consulted (i.e. the fallback path ran), not that 0.0 is
        # incidentally correct.
        metrics_collector.load_recent.assert_called_once_with(5)
        assert score == 0.0
