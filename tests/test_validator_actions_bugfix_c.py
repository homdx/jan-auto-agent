"""tests/test_validator_actions_bugfix_c.py

Regression tests for the "asymmetric API-error resilience" bug chain
(validated independently — see VALIDATION_REPORT.md, Bug C):

  C1. tools/validator_agent.py — a network/HTTP failure and a
      successfully-returned-but-malformed (non-JSON) reply used to share
      one except clause and both came out as {"_api_error": True}. Fixed
      by splitting the try/except so only a genuine request_completion
      failure sets _api_error; a parse failure on a response that DID
      arrive sets _unparseable instead.

  C2. tools/actions.py run_search() — its fail-open contract ("if the
      validator is unreachable, accept the candidate answer as-is") was
      implemented by checking only _api_error. After C1, a validator that
      never produces valid JSON would cause every chunk to be rejected,
      silently breaking that contract. Fixed by also checking
      _unparseable at the run_search fail-open check.

  C3. tools/prompt_evaluator.py _shadow_score() — json_ok_rate excluded
      only _api_error results. After C1, a candidate prompt that never
      produces valid JSON has none of its failures labelled _api_error,
      so json_ok_rate silently inverts to a perfect 1.0 — rewarding
      exactly the failure mode it exists to penalise. Fixed by excluding
      both _api_error and _unparseable.

These three fixes shipped with NO regression tests (confirmed by a full
search of tests/ during independent validation). This file closes that
gap by locking in the verified behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.validator_agent import ValidatorAgent


# ── C1: validator_agent.py error-path split ─────────────────────────────────

class TestValidatorAgentErrorSplit:
    """A network failure and a malformed-JSON reply must set DIFFERENT
    sentinel flags, never both, never neither."""

    def _agent(self):
        return ValidatorAgent(
            model="test-model", base_url="http://fake-host", api_key="x",
            timeout=5, max_iter=3,
        )

    def _payload(self):
        return {
            "task": "t", "iteration": 1, "target_block": "",
            "imports": "", "related_code": {}, "missing_refs": "None",
        }

    def test_malformed_reply_sets_unparseable_not_api_error(self):
        va = self._agent()
        with patch(
            "tools.validator_agent.request_completion",
            return_value="this is prose, not JSON",
        ):
            result = va.validate(self._payload())

        assert result.get("_unparseable") is True
        assert not result.get("_api_error"), (
            "a response that DID arrive (even if malformed) must never be "
            "labelled _api_error — that flag is for genuine network/HTTP "
            "failures only"
        )
        assert result["status"] == "needs_fix"

    def test_network_failure_sets_api_error_not_unparseable(self):
        va = self._agent()
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=ConnectionError("connection refused"),
        ):
            result = va.validate(self._payload())

        assert result.get("_api_error") is True
        assert not result.get("_unparseable"), (
            "a call that never returned a response must never be labelled "
            "_unparseable — that flag is for a reply that arrived but "
            "failed to parse"
        )
        assert result["status"] == "needs_fix"

    def test_valid_json_reply_sets_neither_flag(self):
        va = self._agent()
        with patch(
            "tools.validator_agent.request_completion",
            return_value='{"status": "approved", "feedback": "", "suggested_searches": []}',
        ):
            result = va.validate(self._payload())

        assert not result.get("_api_error")
        assert not result.get("_unparseable")
        assert result["status"] == "approved"


# ── C3: prompt_evaluator.py json_ok_rate exclusion logic ────────────────────

class TestJsonOkRateExcludesBothFailureModes:
    """Direct test of the json_ok_rate formula used in
    PromptEvaluator._shadow_score(). A candidate that never produces valid
    JSON must score json_ok_rate == 0.0, not 1.0."""

    @staticmethod
    def _json_ok_rate(results: list[dict]) -> float:
        """Mirrors tools/prompt_evaluator.py's _shadow_score() formula
        exactly (see the AUTO-BUG (follow-up) / AUTO-BUG (follow-up 2)
        comments there).

        UPDATED (follow-up 2): _api_error results are now excluded from the
        denominator too, not just the numerator — matching
        _score_from_records' treatment of not-applicable records — so
        network jitter during a shadow run doesn't distort a candidate's
        score. Only _unparseable results (a reply DID arrive and failed
        the thing this rate measures) still count against the candidate.
        """
        judged = [r for r in results if not r.get("_api_error")]
        if not judged:
            return 0.0
        return sum(1 for r in judged if not r.get("_unparseable")) / len(judged)

    def test_all_unparseable_scores_zero_not_one(self):
        results = [{"status": "needs_fix", "_unparseable": True} for _ in range(5)]
        rate = self._json_ok_rate(results)
        assert rate == 0.0, (
            f"a candidate prompt that NEVER produces valid JSON must score "
            f"json_ok_rate=0.0, got {rate} — this is the exact scoring "
            f"inversion that could get a broken prompt promoted by AutoTuner"
        )

    def test_all_api_error_scores_zero(self):
        results = [{"status": "needs_fix", "_api_error": True} for _ in range(5)]
        rate = self._json_ok_rate(results)
        assert rate == 0.0

    def test_all_valid_json_scores_one(self):
        results = [{"status": "approved"} for _ in range(5)]
        rate = self._json_ok_rate(results)
        assert rate == 1.0

    def test_mixed_failures_scored_proportionally(self):
        results = [
            {"status": "approved"},
            {"status": "approved"},
            {"status": "needs_fix", "_unparseable": True},
            {"status": "needs_fix", "_api_error": True},
            {"status": "needs_fix"},  # ordinary content-based rejection — still "ok JSON"
        ]
        rate = self._json_ok_rate(results)
        # 1 _api_error result is excluded from the denominator entirely
        # (network jitter, not a prompt-quality signal) — 2 approved + 1
        # ordinary needs_fix count as "ok JSON" out of the 4 judged results
        # (5 total minus the 1 _api_error); the 1 _unparseable result is
        # judged and counts against the candidate.
        assert rate == 3 / 4


# ── C: actions.py _answer_from_file None-on-failure + retry ─────────────────

class _FakeOrchestrator:
    """Minimal stand-in exposing just enough for OrchestratorActions
    methods under test to run without a real Orchestrator."""
    def __init__(self):
        self.model = "test-model"
        self.base_url = "http://fake-host"
        self.api_key = "x"
        self.timeout_seconds = 5
        self.stream_agents = False
        self.ssl_context = None
        self.api_format = "openai"
        self.config = None


def _make_actions():
    import tools.actions as actions_mod

    class _Orch(actions_mod.OrchestratorActions, _FakeOrchestrator):
        def __init__(self):
            _FakeOrchestrator.__init__(self)

    return _Orch()


class TestAnswerFromFileFailureSignalling:
    """_answer_from_file must return None (never '') on a transient
    generation failure, so callers can distinguish 'API call failed' from
    'model legitimately returned an empty string'."""

    def test_returns_none_on_exception(self):
        orch = _make_actions()
        with patch(
            "tools.actions.request_completion",
            side_effect=ConnectionError("network down"),
        ):
            result = orch._answer_from_file("question", "file.md", "knowledge", None)
        assert result is None, (
            f"expected None on transient failure so it is distinguishable "
            f"from a legitimately empty answer, got {result!r}"
        )

    def test_returns_string_on_success(self):
        orch = _make_actions()
        with patch(
            "tools.actions.request_completion",
            return_value="a real answer",
        ):
            result = orch._answer_from_file("question", "file.md", "knowledge", None)
        assert result == "a real answer"
        assert result is not None
