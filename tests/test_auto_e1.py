"""tests/test_auto_e1.py — AUTO-E1: prompt auto-tuning in autonomous mode.

ACs (from the Jira story):
  * A promoted prompt is applied via reload_agents mid-run; rollback still works.
  * Reuses prompt_optimizer, prompt_evaluator, prompt_store, reload_agents.

Coverage:
  * maybe_tune() returns TuneOutcome(triggered=False) when disabled.
  * maybe_tune() returns TuneOutcome(triggered=False) when fewer runs than min_runs.
  * maybe_tune() returns TuneOutcome(triggered=False) when signal below threshold.
  * maybe_tune() triggers when avg_iterations > trigger threshold.
  * maybe_tune() triggers when json_parse_failure_rate > trigger threshold.
  * When candidate is promoted: prompt_store.push() called; reload_agents_fn called.
  * When candidate is discarded: push() NOT called; reload_agents_fn NOT called.
  * reload_agents_fn raising does NOT abort maybe_tune (fail-open on reload).
  * Any exception in the optimizer/evaluator is caught; run continues (fail-closed).
  * rollback() delegates to PromptStore.rollback() and fires reload_agents_fn.
  * rollback() returns False (and skips reload) when already at hardcoded.
  * make_auto_tuner factory builds an AutoTuner with correct config values.
  * Auto metrics go to <agent_dir>/metrics.json, NOT interactive metrics.json.
  * record_gate2_result() writes a RunRecord to the auto metrics stream.
  * TuneOutcome.summary() returns sensible strings for each outcome.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.auto_tuner import (
    AutoTuner,
    TuneOutcome,
    make_auto_tuner,
    record_gate2_result,
)
from tools.metrics_collector import MetricsCollector, RunRecord
from tools.prompt_store import PromptStore


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_metrics(tmp_path: Path, records: list[dict] | None = None) -> MetricsCollector:
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    if records:
        for r in records:
            mc.record(RunRecord(**r))
    return mc


def _run_record(
    *,
    approved: bool = True,
    iters: int = 1,
    json_ok: Optional[bool] = None,
    intent: str = "AUTO-T1",
) -> dict:
    return dict(
        timestamp="2024-01-01T00:00:00Z",
        intent=intent,
        prompt_version="auto",
        iterations_used=iters,
        validator_status="approved" if approved else "rejected",
        validator_feedback="ok" if approved else "missing ref",
        improvement_json_ok=json_ok,
        elapsed_seconds=1.0,
    )


def _make_prompt_store(tmp_path: Path) -> PromptStore:
    return PromptStore(store_path=tmp_path / "prompts.json", max_versions=3)


def _fake_summary(
    total: int = 10,
    avg_iter: float = 1.0,
    json_fail: float = 0.0,
) -> dict:
    return {
        "total_runs": total,
        "avg_iterations": avg_iter,
        "json_parse_failure_rate": json_fail,
        "common_feedback": [],
        "worst_intent": "AUTO-T1",
    }


def _mock_optimizer(candidate: str = "new prompt") -> MagicMock:
    opt = MagicMock()
    opt.generate_candidate.return_value = candidate
    return opt


def _mock_evaluator(promoted: bool, score: float = 0.85, reason: str = "improved") -> MagicMock:
    from tools.prompt_evaluator import EvalResult
    ev = MagicMock()
    ev.evaluate.return_value = EvalResult(promoted=promoted, score=score, reason=reason)
    return ev


def _make_tuner(
    tmp_path: Path,
    *,
    enabled: bool = True,
    min_runs: int = 5,
    trigger_avg_iter: float = 2.0,
    trigger_json_fail: float = 0.30,
    optimizer_candidate: str = "improved prompt",
    evaluator_promoted: bool = True,
    evaluator_score: float = 0.88,
    records: list[dict] | None = None,
    reload_fn=None,
) -> AutoTuner:
    mc = _make_metrics(tmp_path, records)
    ps = _make_prompt_store(tmp_path)
    opt = _mock_optimizer(optimizer_candidate)
    ev = _mock_evaluator(evaluator_promoted, evaluator_score)
    return AutoTuner(
        prompt_store=ps,
        metrics_collector=mc,
        prompt_optimizer=opt,
        prompt_evaluator=ev,
        reload_agents_fn=reload_fn,
        agent_name="validator",
        enabled=enabled,
        min_runs=min_runs,
        trigger_avg_iter=trigger_avg_iter,
        trigger_json_fail_rate=trigger_json_fail,
    )


# ── TuneOutcome ───────────────────────────────────────────────────────────────

class TestTuneOutcome:
    def test_summary_not_triggered(self):
        o = TuneOutcome(agent_name="validator", reason="disabled")
        s = o.summary()
        assert "validator" in s

    def test_summary_promoted(self):
        o = TuneOutcome(
            triggered=True, promoted=True, agent_name="validator",
            reason="score improved", new_prompt_score=0.88,
        )
        s = o.summary()
        assert "promoted" in s.lower() or "validator" in s
        assert "0.88" in s

    def test_summary_discarded(self):
        o = TuneOutcome(
            triggered=True, promoted=False, agent_name="validator",
            reason="insufficient improvement", new_prompt_score=0.70,
        )
        s = o.summary()
        assert "discarded" in s.lower() or "validator" in s


# ── maybe_tune: disabled ──────────────────────────────────────────────────────

class TestMaybeTuneDisabled:
    def test_returns_not_triggered_when_disabled(self, tmp_path):
        tuner = _make_tuner(tmp_path, enabled=False)
        outcome = tuner.maybe_tune()
        assert outcome.triggered is False

    def test_optimizer_not_called_when_disabled(self, tmp_path):
        tuner = _make_tuner(tmp_path, enabled=False)
        tuner.maybe_tune()
        tuner.optimizer.generate_candidate.assert_not_called()

    def test_push_not_called_when_disabled(self, tmp_path):
        tuner = _make_tuner(tmp_path, enabled=False)
        with patch.object(tuner.prompt_store, "push") as mock_push:
            tuner.maybe_tune()
            mock_push.assert_not_called()


# ── maybe_tune: not enough runs ───────────────────────────────────────────────

class TestMaybeTuneNotEnoughRuns:
    def test_returns_not_triggered_with_too_few_runs(self, tmp_path):
        tuner = _make_tuner(tmp_path, min_runs=5)  # no records
        outcome = tuner.maybe_tune()
        assert outcome.triggered is False

    def test_optimizer_not_called(self, tmp_path):
        tuner = _make_tuner(tmp_path, min_runs=5)
        tuner.maybe_tune()
        tuner.optimizer.generate_candidate.assert_not_called()

    def test_reason_mentions_run_count(self, tmp_path):
        tuner = _make_tuner(tmp_path, min_runs=5)
        outcome = tuner.maybe_tune()
        assert "0" in outcome.reason or "not enough" in outcome.reason.lower()


# ── maybe_tune: signal below threshold ───────────────────────────────────────

class TestMaybeTuneBelowThreshold:
    def _records_below_threshold(self) -> list[dict]:
        # avg_iter=1.0 (<2.0), json_fail=0% (<30%) — no signal
        return [_run_record(approved=True, iters=1) for _ in range(6)]

    def test_not_triggered_below_threshold(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0, trigger_json_fail=0.30,
            records=self._records_below_threshold(),
        )
        outcome = tuner.maybe_tune()
        assert outcome.triggered is False

    def test_optimizer_not_called_below_threshold(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5,
            records=self._records_below_threshold(),
        )
        tuner.maybe_tune()
        tuner.optimizer.generate_candidate.assert_not_called()


# ── maybe_tune: trigger via avg_iterations ───────────────────────────────────

class TestMaybeTuneTriggerAvgIter:
    def _high_iter_records(self) -> list[dict]:
        # avg_iter=3.0 > 2.0 threshold
        return [_run_record(iters=3) for _ in range(6)]

    def test_triggered_when_avg_iter_high(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._high_iter_records(), evaluator_promoted=True,
        )
        outcome = tuner.maybe_tune()
        assert outcome.triggered is True

    def test_optimizer_called_once(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._high_iter_records(),
        )
        tuner.maybe_tune()
        tuner.optimizer.generate_candidate.assert_called_once()

    def test_evaluator_called_with_candidate(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            optimizer_candidate="my shiny prompt",
            records=self._high_iter_records(),
        )
        tuner.maybe_tune()
        tuner.evaluator.evaluate.assert_called_once_with("validator", "my shiny prompt")


# ── maybe_tune: trigger via json_fail_rate ───────────────────────────────────

class TestMaybeTuneTriggerJsonFail:
    def _json_fail_records(self) -> list[dict]:
        # improvement_json_ok=False → json_fail_rate=1.0 > 0.30
        return [
            _run_record(json_ok=False, intent=f"AUTO-T{i}") for i in range(6)
        ]

    def test_triggered_when_json_fail_high(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_json_fail=0.30,
            records=self._json_fail_records(), evaluator_promoted=True,
        )
        outcome = tuner.maybe_tune()
        assert outcome.triggered is True


# ── maybe_tune: promotion path ────────────────────────────────────────────────

class TestMaybeTunePromotion:
    def _records(self) -> list[dict]:
        return [_run_record(iters=3) for _ in range(6)]

    def test_promoted_true_in_outcome(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=True, evaluator_score=0.90,
        )
        outcome = tuner.maybe_tune()
        assert outcome.promoted is True

    def test_score_in_outcome(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=True, evaluator_score=0.90,
        )
        outcome = tuner.maybe_tune()
        assert outcome.new_prompt_score == pytest.approx(0.90)

    def test_push_called_with_candidate(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            optimizer_candidate="super prompt",
            records=self._records(), evaluator_promoted=True, evaluator_score=0.85,
        )
        with patch.object(tuner.prompt_store, "push") as mock_push:
            tuner.maybe_tune()
            mock_push.assert_called_once_with("validator", "super prompt", 0.85)

    def test_reload_agents_called_on_promotion(self, tmp_path):
        reload_fn = MagicMock()
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=True, reload_fn=reload_fn,
        )
        tuner.maybe_tune()
        reload_fn.assert_called_once()

    def test_prompt_persisted_to_store(self, tmp_path):
        """Integration: the pushed prompt is readable from PromptStore."""
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            optimizer_candidate="the new validator prompt",
            records=self._records(), evaluator_promoted=True, evaluator_score=0.88,
        )
        tuner.maybe_tune()
        active = tuner.prompt_store.get_current("validator")
        assert active == "the new validator prompt"


# ── maybe_tune: discarded path ────────────────────────────────────────────────

class TestMaybeTuneDiscarded:
    def _records(self) -> list[dict]:
        return [_run_record(iters=3) for _ in range(6)]

    def test_promoted_false_in_outcome(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=False,
        )
        outcome = tuner.maybe_tune()
        assert outcome.triggered is True
        assert outcome.promoted is False

    def test_push_not_called_when_discarded(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=False,
        )
        with patch.object(tuner.prompt_store, "push") as mock_push:
            tuner.maybe_tune()
            mock_push.assert_not_called()

    def test_reload_not_called_when_discarded(self, tmp_path):
        reload_fn = MagicMock()
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=False, reload_fn=reload_fn,
        )
        tuner.maybe_tune()
        reload_fn.assert_not_called()


# ── fail-closed: errors in optimizer/evaluator ───────────────────────────────

class TestFailClosed:
    def _records(self) -> list[dict]:
        return [_run_record(iters=3) for _ in range(6)]

    def test_optimizer_exception_does_not_raise(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(),
        )
        tuner.optimizer.generate_candidate.side_effect = RuntimeError("LLM down")
        outcome = tuner.maybe_tune()  # must not raise
        assert outcome.triggered is False
        assert "fail-closed" in outcome.reason.lower() or "error" in outcome.reason.lower()

    def test_evaluator_exception_does_not_raise(self, tmp_path):
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(),
        )
        tuner.evaluator.evaluate.side_effect = ConnectionError("timeout")
        outcome = tuner.maybe_tune()  # must not raise
        assert outcome.triggered is False

    def test_reload_exception_does_not_abort_outcome(self, tmp_path):
        """reload_agents_fn raising should not prevent a promoted outcome."""
        def bad_reload():
            raise OSError("socket closed")

        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            records=self._records(), evaluator_promoted=True,
            reload_fn=bad_reload,
        )
        outcome = tuner.maybe_tune()   # must not raise
        assert outcome.promoted is True


# ── rollback ──────────────────────────────────────────────────────────────────

class TestRollback:
    def test_rollback_at_hardcoded_returns_false(self, tmp_path):
        tuner = _make_tuner(tmp_path)
        assert tuner.rollback() is False

    def test_rollback_returns_true_after_push(self, tmp_path):
        tuner = _make_tuner(tmp_path)
        tuner.prompt_store.push("validator", "new prompt", 0.8)
        assert tuner.rollback() is True

    def test_rollback_fires_reload_agents(self, tmp_path):
        reload_fn = MagicMock()
        tuner = _make_tuner(tmp_path, reload_fn=reload_fn)
        tuner.prompt_store.push("validator", "new prompt", 0.8)
        tuner.rollback()
        reload_fn.assert_called_once()

    def test_rollback_at_hardcoded_does_not_fire_reload(self, tmp_path):
        reload_fn = MagicMock()
        tuner = _make_tuner(tmp_path, reload_fn=reload_fn)
        tuner.rollback()
        reload_fn.assert_not_called()

    def test_rollback_reverts_active_prompt(self, tmp_path):
        tuner = _make_tuner(tmp_path)
        tuner.prompt_store.push("validator", "v2 prompt", 0.8)
        tuner.rollback()
        # After rollback to hardcoded, get_current falls back to hardcoded constant
        from tools.validator_agent import VALIDATOR_PROMPT_HARDCODED
        assert tuner.prompt_store.get_current("validator") == VALIDATOR_PROMPT_HARDCODED

    def test_rollback_ac_still_works_after_promotion(self, tmp_path):
        """AC: rollback still works after a mid-run promotion."""
        reload_fn = MagicMock()
        records = [_run_record(iters=3) for _ in range(6)]
        tuner = _make_tuner(
            tmp_path, min_runs=5, trigger_avg_iter=2.0,
            optimizer_candidate="promoted prompt",
            records=records, evaluator_promoted=True,
            reload_fn=reload_fn,
        )
        tuner.maybe_tune()
        assert tuner.prompt_store.get_current("validator") == "promoted prompt"

        ok = tuner.rollback()
        assert ok is True
        reload_fn.assert_called()   # called once on promote, once on rollback
        from tools.validator_agent import VALIDATOR_PROMPT_HARDCODED
        assert tuner.prompt_store.get_current("validator") == VALIDATOR_PROMPT_HARDCODED


# ── make_auto_tuner factory ───────────────────────────────────────────────────

class TestFactory:
    def _config(self, tmp_path: Path) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {
            "enabled": "true",
            "min_runs_before_optimize": "3",
            "trigger_avg_iterations": "1.5",
            "trigger_json_fail_rate": "0.20",
        }
        cfg["prompt_store"] = {
            "store_path": str(tmp_path / "prompts.json"),
            "max_versions": "3",
        }
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {
            "base_url": "http://localhost:1337/v1",
            "api_key": "jan",
            "model": "test-model",
            "api_format": "openai",
        }
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}
        return cfg

    def test_returns_auto_tuner_instance(self, tmp_path):
        cfg = self._config(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent")
        assert isinstance(tuner, AutoTuner)

    def test_enabled_reads_from_config(self, tmp_path):
        cfg = self._config(tmp_path)
        cfg["prompt_optimizer"]["enabled"] = "false"
        tuner = make_auto_tuner(cfg, tmp_path / ".agent")
        assert tuner.enabled is False

    def test_min_runs_reads_from_config(self, tmp_path):
        cfg = self._config(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent")
        assert tuner.min_runs == 3

    def test_trigger_avg_iter_reads_from_config(self, tmp_path):
        cfg = self._config(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent")
        assert tuner.trigger_avg_iter == pytest.approx(1.5)

    def test_auto_metrics_rooted_in_agent_dir(self, tmp_path):
        """AUTO-E2 separation: auto metrics go to .agent/metrics.json."""
        cfg = self._config(tmp_path)
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)
        expected = agent_dir / "metrics.json"
        assert tuner.metrics.metrics_path == expected

    def test_reload_fn_wired(self, tmp_path):
        reload_fn = MagicMock()
        cfg = self._config(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent", reload_fn)
        assert tuner.reload_agents_fn is reload_fn

    def test_injected_prompt_store_used(self, tmp_path):
        cfg = self._config(tmp_path)
        ps = _make_prompt_store(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent", prompt_store=ps)
        assert tuner.prompt_store is ps

    def test_injected_metrics_collector_used(self, tmp_path):
        cfg = self._config(tmp_path)
        mc = _make_metrics(tmp_path)
        tuner = make_auto_tuner(cfg, tmp_path / ".agent", metrics_collector=mc)
        assert tuner.metrics is mc


# ── record_gate2_result ───────────────────────────────────────────────────────

class TestRecordGate2Result:
    def test_writes_to_auto_metrics(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto_metrics.json")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="ok", attempts_used=2)
        records = mc.load_recent(5)
        assert len(records) == 1
        assert records[0].validator_status == "approved"
        assert records[0].iterations_used == 2
        assert records[0].intent == "AUTO-T1"

    def test_rejected_written_correctly(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto_metrics.json")
        record_gate2_result(
            mc, "AUTO-T2", approved=False, feedback="bad output", attempts_used=5
        )
        records = mc.load_recent(5)
        assert records[0].validator_status == "rejected"
        assert records[0].validator_feedback == "bad output"

    def test_improvement_json_ok_is_none(self, tmp_path):
        """Auto-mode records should not pollute the json_ok_rate calculation."""
        mc = MetricsCollector(metrics_path=tmp_path / "auto_metrics.json")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1)
        records = mc.load_recent(5)
        assert records[0].improvement_json_ok is None

    def test_prompt_version_from_store(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto_metrics.json")
        ps = _make_prompt_store(tmp_path)
        ps.push("validator", "v1 prompt", 0.8)
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1, prompt_store=ps)
        records = mc.load_recent(5)
        assert records[0].prompt_version == "v1"

    def test_metrics_not_pollute_interactive(self, tmp_path):
        """Interactive metrics.json must not be touched by auto-run records."""
        interactive_mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
        auto_mc = MetricsCollector(metrics_path=tmp_path / ".agent" / "metrics.json")
        record_gate2_result(auto_mc, "AUTO-T1", approved=True, feedback="", attempts_used=1)
        # Interactive file must still not exist
        assert not (tmp_path / "metrics.json").exists()

    def test_record_error_does_not_raise(self, tmp_path):
        """record_gate2_result must not propagate MetricsCollector errors."""
        mc = MagicMock()
        mc.record.side_effect = OSError("disk full")
        # Should silently log and return, not raise
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1)


# ── AUTO-E2 metric stream isolation ──────────────────────────────────────────

class TestAutoE2MetricIsolation:
    def test_factory_auto_metrics_path_differs_from_interactive(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {"enabled": "true"}
        cfg["prompt_store"] = {"store_path": str(tmp_path / "prompts.json")}
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {"base_url": "http://localhost/v1", "api_key": "", "model": "m", "api_format": "openai"}
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)
        # Auto metrics must NOT be at the interactive default (metrics.json)
        assert tuner.metrics.metrics_path != Path("metrics.json")
        assert ".agent" in str(tuner.metrics.metrics_path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))