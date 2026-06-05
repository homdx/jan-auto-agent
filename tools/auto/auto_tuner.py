"""
tools/auto/auto_tuner.py — AUTO-E1

Wire the existing prompt_optimizer / prompt_evaluator / prompt_store into
autonomous mode.

AUTO-E2 isolation (separate metrics stream) is owned by
tools/auto/auto_metrics.py.  The ``record_gate2_result`` helper is imported
from there so callers can use either module as an entry point.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

from tools.metrics_collector import MetricsCollector
from tools.prompt_store import PromptStore
from tools.auto.auto_metrics import record_gate2_result  # noqa: F401 — re-exported for back-compat

if TYPE_CHECKING:
    from tools.prompt_optimizer import PromptOptimizer
    from tools.prompt_evaluator import PromptEvaluator

logger = logging.getLogger(__name__)


# ── TuneOutcome ───────────────────────────────────────────────────────────────

@dataclass
class TuneOutcome:
    agent_name: str
    reason: str
    triggered: bool = False
    promoted: bool = False
    new_prompt_score: float = 0.0

    def summary(self) -> str:
        if not self.triggered:
            return f"[auto-tuner/{self.agent_name}] not triggered — {self.reason}"
        if self.promoted:
            return (
                f"[auto-tuner/{self.agent_name}] promoted "
                f"(score={self.new_prompt_score:.2f}) — {self.reason}"
            )
        return (
            f"[auto-tuner/{self.agent_name}] discarded "
            f"(score={self.new_prompt_score:.2f}) — {self.reason}"
        )


# ── AutoTuner ─────────────────────────────────────────────────────────────────

class AutoTuner:
    """
    Wraps PromptOptimizer + PromptEvaluator with auto-run-specific logic.

    Call maybe_tune() after each task completes; it checks the metrics signal
    and, when triggered, attempts to promote an improved prompt via the
    evaluator gate.  rollback() undoes the last promotion.
    """

    def __init__(
        self,
        prompt_store: PromptStore,
        metrics_collector: MetricsCollector,
        prompt_optimizer: "PromptOptimizer",
        prompt_evaluator: "PromptEvaluator",
        agent_name: str = "validator",
        reload_agents_fn: Optional[Callable[[], None]] = None,
        enabled: bool = True,
        min_runs: int = 5,
        trigger_avg_iter: float = 2.0,
        trigger_json_fail_rate: float = 0.30,
    ) -> None:
        self.prompt_store = prompt_store
        self.metrics = metrics_collector
        self.optimizer = prompt_optimizer
        self.evaluator = prompt_evaluator
        self.agent_name = agent_name
        self.reload_agents_fn = reload_agents_fn
        self.enabled = enabled
        self.min_runs = min_runs
        self.trigger_avg_iter = trigger_avg_iter
        self.trigger_json_fail_rate = trigger_json_fail_rate

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def maybe_tune(self) -> TuneOutcome:
        """
        Check metrics signal and, if triggered, run the optimizer/evaluator pipeline.

        Returns a TuneOutcome in all cases — never raises.
        """
        if not self.enabled:
            return TuneOutcome(agent_name=self.agent_name, reason="disabled")

        # Check we have enough data
        summary = self.metrics.summarize_failures(self.min_runs)
        total = summary.get("total_runs", 0)
        if total < self.min_runs:
            return TuneOutcome(
                agent_name=self.agent_name,
                reason=f"{total} runs — not enough data (need {self.min_runs})",
            )

        # Check signal thresholds.
        # A threshold of 0 means "disabled" — treat as always-above-threshold so
        # test code (and any caller that wants to force a tuning cycle) can set
        # trigger_avg_iter=0 / trigger_json_fail_rate=0 to bypass the guard.
        avg_iter = summary.get("avg_iterations", 0.0)
        json_fail = summary.get("json_parse_failure_rate", 0.0)
        above_avg  = (self.trigger_avg_iter       <= 0) or (avg_iter  > self.trigger_avg_iter)
        above_json = (self.trigger_json_fail_rate <= 0) or (json_fail > self.trigger_json_fail_rate)
        if not (above_avg or above_json):
            return TuneOutcome(
                agent_name=self.agent_name,
                reason=(
                    f"below threshold "
                    f"(avg_iter={avg_iter:.2f}≤{self.trigger_avg_iter}, "
                    f"json_fail={json_fail:.2%}≤{self.trigger_json_fail_rate:.2%})"
                ),
            )

        # Triggered — wrap everything in try/except (fail-closed)
        try:
            current_prompt = self.prompt_store.get_current(self.agent_name)
            candidate = self.optimizer.generate_candidate(
                self.agent_name, current_prompt, summary
            )
            eval_result = self.evaluator.evaluate(self.agent_name, candidate)
        except Exception as exc:
            logger.error("AutoTuner: fail-closed — optimizer/evaluator error: %s", exc)
            return TuneOutcome(
                agent_name=self.agent_name,
                reason=f"fail-closed: error during tuning — {exc}",
            )

        if eval_result.promoted:
            # Push to store
            self.prompt_store.push(self.agent_name, candidate, eval_result.score)
            # Fire reload — fail-open (error here must not prevent a promoted outcome)
            if self.reload_agents_fn is not None:
                try:
                    self.reload_agents_fn()
                except Exception as exc:
                    logger.warning("AutoTuner: reload_agents_fn raised (ignored): %s", exc)
            return TuneOutcome(
                agent_name=self.agent_name,
                triggered=True,
                promoted=True,
                reason=eval_result.reason,
                new_prompt_score=eval_result.score,
            )
        else:
            return TuneOutcome(
                agent_name=self.agent_name,
                triggered=True,
                promoted=False,
                reason=eval_result.reason,
                new_prompt_score=eval_result.score,
            )

    def rollback(self) -> bool:
        """
        Roll back the active prompt to the previous version.

        Returns True if a version was rolled back (and reload_agents_fn fired),
        False if already at hardcoded baseline (no reload fired).
        """
        rolled = self.prompt_store.rollback(self.agent_name)
        if not rolled:
            return False
        if self.reload_agents_fn is not None:
            try:
                self.reload_agents_fn()
            except Exception as exc:
                logger.warning("AutoTuner: reload_agents_fn raised during rollback (ignored): %s", exc)
        return True


# ── Factory ───────────────────────────────────────────────────────────────────

def make_auto_tuner(
    config: configparser.ConfigParser,
    agent_dir: Path,
    reload_fn: Optional[Callable[[], None]] = None,
    *,
    prompt_store: Optional[PromptStore] = None,
    metrics_collector: Optional[MetricsCollector] = None,
) -> AutoTuner:
    """
    Build an AutoTuner from agents.ini config + the agent run directory.

    Auto metrics are rooted at <agent_dir>/metrics.json, not the interactive
    metrics.json, fulfilling the AUTO-E2 isolation requirement.
    """
    # Prompt store
    if prompt_store is None:
        store_path_str = config.get("prompt_store", "store_path", fallback="prompts.json")
        max_versions = config.getint("prompt_store", "max_versions", fallback=3)
        prompt_store = PromptStore(store_path=Path(store_path_str), max_versions=max_versions)

    # Auto-metrics — isolated from interactive metrics.json (AUTO-E2)
    if metrics_collector is None:
        metrics_collector = MetricsCollector(metrics_path=agent_dir / "metrics.json")

    # Tuner settings
    enabled = config.getboolean("prompt_optimizer", "enabled", fallback=True)
    min_runs = config.getint("prompt_optimizer", "min_runs_before_optimize", fallback=5)
    trigger_avg_iter = config.getfloat("prompt_optimizer", "trigger_avg_iterations", fallback=2.0)
    trigger_json_fail = config.getfloat("prompt_optimizer", "trigger_json_fail_rate", fallback=0.30)

    # Build PromptOptimizer from api config
    from tools.prompt_optimizer import PromptOptimizer
    active_api = config.get("api", "active", fallback="local")
    api_section = f"api_{active_api}"
    base_url = config.get(api_section, "base_url", fallback="http://localhost:1337/v1")
    api_key = config.get(api_section, "api_key", fallback="")
    model = config.get(api_section, "model", fallback="")
    optimizer = PromptOptimizer(model=model, base_url=base_url, api_key=api_key)

    # Build PromptEvaluator — no shadow ValidatorAgent in auto mode (safe default)
    from tools.prompt_evaluator import PromptEvaluator
    max_iter = config.getint("loop", "max_iterations", fallback=3)
    evaluator = PromptEvaluator(
        prompt_store=prompt_store,
        metrics_collector=metrics_collector,
        validator_agent=None,
        max_iter=max_iter,
    )

    return AutoTuner(
        prompt_store=prompt_store,
        metrics_collector=metrics_collector,
        prompt_optimizer=optimizer,
        prompt_evaluator=evaluator,
        agent_name="validator",
        reload_agents_fn=reload_fn,
        enabled=enabled,
        min_runs=min_runs,
        trigger_avg_iter=trigger_avg_iter,
        trigger_json_fail_rate=trigger_json_fail,
    )


# record_gate2_result is imported from tools.auto.auto_metrics (AUTO-E2) and
# re-exported above via the import line.  Canonical implementation lives there.
