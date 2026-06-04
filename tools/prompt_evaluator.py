"""
tools/prompt_evaluator.py

STORY-4.1: PromptEvaluator — gated promotion of candidate prompts.

Scoring weights (no LLM call for baseline; pure arithmetic on MetricsLog):
  - avg_iterations lower          40 %
  - improvement_json_ok rate      35 %
  - validator_status == approved  25 %

Shadow mode: ValidatorAgent is called directly with the candidate prompt
injected temporarily via a one-shot duck-typed PromptStore.  Falls back
to a metric-projection heuristic when the LLM is unavailable.

Promotion threshold: candidate_score > current_score + 0.05
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

from tools.metrics_collector import MetricsCollector, RunRecord

if TYPE_CHECKING:
    from tools.prompt_store import PromptStore
    from tools.validator_agent import ValidatorAgent

logger = logging.getLogger(__name__)

# Sentinel used to detect when max_iter is not explicitly passed by the caller.
# Using a sentinel instead of a plain default means callers that omit max_iter
# get a loud warning rather than silently scoring against the wrong ceiling.
_MAX_ITER_ASSUMED = 3
_UNSET = object()

# Synthetic payloads for shadow evaluation.
# These exercise the full validator prompt format without needing stored inputs.
_SHADOW_PAYLOADS: List[dict] = [
    {
        "task": "Validate import resolution for a utility module",
        "iteration": 1,
        "target_block": (
            "import os\nimport sys\n\n"
            "def get_env(key: str, default: str = '') -> str:\n"
            "    return os.environ.get(key, default)\n"
        ),
        "imports": "import os\nimport sys",
        "related_code": {},
        "missing_refs": "None",
    },
    {
        "task": "Check completeness of a logging wrapper function",
        "iteration": 1,
        "target_block": (
            "import logging\n\n"
            "def setup_logger(name: str) -> logging.Logger:\n"
            "    logger = logging.getLogger(name)\n"
            "    logger.setLevel(logging.DEBUG)\n"
            "    return logger\n"
        ),
        "imports": "import logging",
        "related_code": {},
        "missing_refs": "None",
    },
    {
        "task": "Verify config-reader helper is self-contained",
        "iteration": 1,
        "target_block": (
            "import configparser\nfrom pathlib import Path\n\n"
            "def load_config(path: str) -> configparser.ConfigParser:\n"
            "    cfg = configparser.ConfigParser()\n"
            "    cfg.read(Path(path))\n"
            "    return cfg\n"
        ),
        "imports": "import configparser\nfrom pathlib import Path",
        "related_code": {},
        "missing_refs": "None",
    },
]


@dataclass
class EvalResult:
    promoted: bool
    reason: str    # human-readable, logged to console
    score: float   # 0.0–1.0


class PromptEvaluator:
    """
    Evaluates a candidate prompt against real run metrics before promotion.

    Usage::

        evaluator = PromptEvaluator(prompt_store, metrics_collector, validator_agent,
                                    max_iter=config_max_iterations)
        result = evaluator.evaluate("validator_agent", candidate_prompt)
        if result.promoted:
            prompt_store.push("validator_agent", candidate_prompt, result.score)
    """

    def __init__(
        self,
        prompt_store: "PromptStore",
        metrics_collector: MetricsCollector,
        validator_agent: Optional["ValidatorAgent"] = None,
        max_iter: int = _UNSET,  # type: ignore[assignment]
    ) -> None:
        self.prompt_store = prompt_store
        self.metrics_collector = metrics_collector
        self.validator_agent = validator_agent
        if max_iter is _UNSET:
            logger.warning(
                "PromptEvaluator: max_iter not supplied — falling back to %d. "
                "Pass max_iter=config max_iterations so iteration scores are "
                "comparable to baseline records.",
                _MAX_ITER_ASSUMED,
            )
            self.max_iter: int = _MAX_ITER_ASSUMED
        else:
            self.max_iter = max_iter  # used for iter_score normalisation

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def evaluate(self, agent_name: str, candidate_prompt: str) -> EvalResult:
        """
        Evaluate *candidate_prompt* for *agent_name*.

        Steps:
        1. Validate the candidate has all required placeholders and no stray braces.
        2. Load last 5 production runs → compute current_score.
        3. Shadow-run the candidate (or project) → compute candidate_score.
        4. Promote iff candidate_score > current_score + 0.05.
        """
        validation_error = _validate_candidate_placeholders(candidate_prompt)
        if validation_error:
            logger.warning(
                "PromptEvaluator: candidate for '%s' rejected — %s",
                agent_name, validation_error,
            )
            return EvalResult(
                promoted=False,
                reason=f"Candidate rejected (malformed template): {validation_error}",
                score=0.0,
            )

        recent: List[RunRecord] = self.metrics_collector.load_recent(5)

        if not recent:
            return EvalResult(
                promoted=False,
                reason="No baseline runs in metrics.json — cannot evaluate safely.",
                score=0.0,
            )

        current_score = self._score_from_records(recent)
        logger.info(
            "PromptEvaluator: current_score=%.4f (n=%d baseline runs)",
            current_score,
            len(recent),
        )

        candidate_score = self._shadow_score(agent_name, candidate_prompt)
        logger.info("PromptEvaluator: candidate_score=%.4f", candidate_score)

        delta = candidate_score - current_score

        if delta >= 0.05:
            reason = (
                f"Score improved by {delta:+.2f} "
                f"({current_score:.2f} → {candidate_score:.2f})"
            )
            return EvalResult(promoted=True, reason=reason, score=candidate_score)

        reason = (
            f"Insufficient improvement: Δ={delta:+.2f} "
            f"(need ≥ 0.05; {current_score:.2f} → {candidate_score:.2f})"
        )
        return EvalResult(promoted=False, reason=reason, score=candidate_score)

    # ------------------------------------------------------------------ #
    # Scoring helpers                                                      #
    # ------------------------------------------------------------------ #

    def _score_from_records(self, records: List[RunRecord]) -> float:
        """
        Weighted composite score from production RunRecords.

          avg_iterations lower          → 40 %
          improvement_json_ok rate      → 35 %
          validator_status == approved  → 25 %
        """
        n = len(records)
        if n == 0:
            return 0.0

        avg_iter = sum(r.iterations_used for r in records) / n
        # 1 iteration → 1.0; self.max_iter iterations → 0.0
        iter_score = max(0.0, 1.0 - (avg_iter - 1.0) / max(1, self.max_iter - 1))

        json_ok_rate = sum(1 for r in records if r.improvement_json_ok) / n

        approved_rate = (
            sum(1 for r in records if r.validator_status == "approved") / n
        )

        return round(
            0.40 * iter_score + 0.35 * json_ok_rate + 0.25 * approved_rate,
            4,
        )

    def _shadow_score(self, agent_name: str, candidate_prompt: str) -> float:
        """
        Run the candidate prompt against synthetic payloads via ValidatorAgent.

        The agent's prompt_store is temporarily swapped for a one-shot
        _FixedPromptStore so the candidate is used for these calls only.
        The original store is always restored in the finally block.

        Falls back to _projected_score() when:
        - No ValidatorAgent is wired in, or
        - All shadow validate() calls raise (LLM unreachable / timeout).
        """
        if self.validator_agent is None:
            logger.debug(
                "PromptEvaluator: no ValidatorAgent wired — using projection fallback"
            )
            return self._projected_score()

        results: List[dict] = []
        original_store = self.validator_agent.prompt_store
        original_max_iter = self.validator_agent.max_iter
        self.validator_agent.prompt_store = _FixedPromptStore(candidate_prompt)
        self.validator_agent.max_iter = self.max_iter

        try:
            for payload in _SHADOW_PAYLOADS:
                try:
                    result = self.validator_agent.validate(dict(payload))
                    results.append(result)
                except Exception as exc:
                    logger.warning(
                        "PromptEvaluator: shadow validate() raised %s: %s",
                        type(exc).__name__,
                        exc,
                    )
        finally:
            # Always restore the original store and max_iter — even on unexpected exception
            self.validator_agent.prompt_store = original_store
            self.validator_agent.max_iter = original_max_iter

        if not results:
            logger.warning(
                "PromptEvaluator: all shadow calls failed — falling back to projection"
            )
            return self._projected_score()

        n = len(results)

        # Shadow runs are single-shot: approved → 1 iteration simulated; else 2
        sim_iters = [1 if r.get("status") == "approved" else 2 for r in results]
        avg_iter = sum(sim_iters) / n
        iter_score = max(0.0, 1.0 - (avg_iter - 1.0) / max(1, self.max_iter - 1))

        json_ok_rate = sum(1 for r in results if not r.get("_api_error")) / n

        approved_rate = (
            sum(1 for r in results if r.get("status") == "approved") / n
        )

        score = round(
            0.40 * iter_score + 0.35 * json_ok_rate + 0.25 * approved_rate,
            4,
        )
        logger.debug(
            "PromptEvaluator shadow: iter_score=%.3f json_ok=%.3f approved=%.3f → %.4f",
            iter_score,
            json_ok_rate,
            approved_rate,
            score,
        )
        return score

    def _projected_score(self) -> float:
        """
        Heuristic projection used when shadow mode is unavailable.

        Loads the last 5 runs, computes the base score, then applies a
        conservative uplift (capped at +0.10) for each signal below
        typical healthy thresholds.  This avoids spurious promotions
        while still allowing the candidate to pass the 0.05 gate when
        the current prompt is clearly struggling.
        """
        recent = self.metrics_collector.load_recent(5)
        if not recent:
            return 0.0

        base = self._score_from_records(recent)
        n = len(recent)

        json_ok_rate = sum(1 for r in recent if r.improvement_json_ok) / n
        approved_rate = sum(1 for r in recent if r.validator_status == "approved") / n

        uplift = 0.0
        if json_ok_rate < 0.70:   # JSON failures are the most fixable signal
            uplift += 0.07
        if approved_rate < 0.60:  # Low approval → secondary uplift
            uplift += 0.05

        projected = round(min(1.0, base + min(uplift, 0.10)), 4)
        logger.debug(
            "PromptEvaluator projection: base=%.4f uplift=%.4f → %.4f",
            base,
            min(uplift, 0.10),
            projected,
        )
        return projected


# Required placeholders that every validator_agent prompt must contain.
_REQUIRED_PLACEHOLDERS = frozenset([
    "{task}", "{iteration}", "{max_iter}",
    "{target_block}", "{imports}", "{related_code}", "{missing_refs}",
])


def _validate_candidate_placeholders(candidate: str) -> Optional[str]:
    """
    Check that *candidate* contains all required placeholders and has no
    unmatched/stray curly braces that would crash str.format().

    Returns None if valid, or a human-readable error string if not.
    """
    import string
    # Check all required keys are present
    missing = [p for p in _REQUIRED_PLACEHOLDERS if p not in candidate]
    if missing:
        return f"missing required placeholders: {', '.join(sorted(missing))}"

    # Check brace balance — str.format() raises ValueError on lone { or }
    try:
        # Parse with string.Formatter to catch unmatched braces
        list(string.Formatter().parse(candidate))
    except (ValueError, KeyError) as exc:
        return f"unmatched or invalid braces: {exc}"

    return None


# ------------------------------------------------------------------ #
# Internal helper                                                      #
# ------------------------------------------------------------------ #

class _FixedPromptStore:
    """
    Minimal duck-typed PromptStore that always returns one fixed prompt.

    Used to temporarily override ValidatorAgent.prompt_store during
    shadow evaluation without touching the real store.
    """

    def __init__(self, prompt: str) -> None:
        self._prompt = prompt

    def get_current(self, agent_name: str) -> str:  # noqa: ARG002
        return self._prompt
