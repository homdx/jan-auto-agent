"""tests/test_bugfix_prompt_evaluator_strict_threshold.py

Bug: the promotion docstring and module header both document a strict
threshold ("Promote iff candidate_score > current_score + 0.05"), but the
check used `delta >= 0.05`, so a candidate whose improvement lands exactly
on the boundary was wrongly promoted instead of requiring it to exceed the
threshold as documented.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.prompt_evaluator import PromptEvaluator
from tools.metrics_collector import MetricsCollector, RunRecord

CANDIDATE = (
    "{task} {iteration} {max_iter} {target_block} {imports} "
    "{related_code} {missing_refs}"
)


class _FakeMetricsCollector(MetricsCollector):
    def __init__(self):
        pass

    def load_recent(self, n):
        # Any non-empty list works — _score_from_records is overridden below.
        return [object()]


def _evaluator_with_scores(current_score: float, candidate_score: float) -> PromptEvaluator:
    ev = PromptEvaluator(
        prompt_store=None,
        metrics_collector=_FakeMetricsCollector(),
        validator_agent=None,
        max_iter=3,
    )
    ev._score_from_records = lambda recent: current_score
    ev._shadow_score = lambda agent_name, candidate_prompt: candidate_score
    return ev


def test_exact_boundary_delta_is_not_promoted():
    # delta == 0.05 exactly (0.05 - 0.0 is exact in binary float, unlike
    # most other "nice" decimal pairs): docstring requires STRICTLY
    # greater than 0.05.
    ev = _evaluator_with_scores(current_score=0.0, candidate_score=0.05)
    result = ev.evaluate("validator_agent", CANDIDATE)
    assert result.promoted is False


def test_delta_just_above_boundary_is_promoted():
    ev = _evaluator_with_scores(current_score=0.0, candidate_score=0.0501)
    result = ev.evaluate("validator_agent", CANDIDATE)
    assert result.promoted is True


def test_delta_below_boundary_is_not_promoted():
    ev = _evaluator_with_scores(current_score=0.0, candidate_score=0.03)
    result = ev.evaluate("validator_agent", CANDIDATE)
    assert result.promoted is False
