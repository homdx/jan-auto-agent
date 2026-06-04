"""tests/test_auto_e2.py — AUTO-E2: separate auto-mode metrics stream.

Story AC (from the Jira backlog):
  * Auto-run metrics go to their own store (.agent/metrics.json) so they
    don't pollute the interactive optimizer's signal.
  * AC: interactive metrics.json untouched by auto runs.

Coverage
--------
AutoMetricsStream construction
  * metrics_path is agent_dir/metrics.json, never the CWD default.
  * Raises ValueError when agent_dir is the project root (collision guard).
  * from_agent_dir() creates the directory if it does not exist.
  * .collector property returns a MetricsCollector pointed at the right path.
  * .metrics_path property matches the underlying collector path.

record_gate2() write path
  * Approved record written correctly (status, iterations, intent).
  * Rejected record written correctly (status, feedback).
  * improvement_json_ok is always None (never pollutes interactive rate).
  * prompt_version recorded from PromptStore when supplied.
  * prompt_version falls back to "auto" when no store supplied.
  * Errors in the underlying collector are swallowed (fail-closed).

Isolation — the core E2 guarantee
  * Interactive metrics.json untouched after recording to auto stream.
  * Auto stream path never equals Path("metrics.json") resolved.
  * Writing to interactive stream has no effect on auto stream (paths independent).
  * summarize_failures() on auto stream excludes improvement_json_ok=None
    records from json_parse_failure_rate (verified by MetricsCollector semantics).

module-level record_gate2_result (backward-compat)
  * Writes to an arbitrary MetricsCollector (back-compat for auto_tuner callers).
  * improvement_json_ok always None.
  * Swallows collector errors.

auto_tuner.py re-export
  * record_gate2_result importable from tools.auto.auto_tuner (back-compat).
  * make_auto_tuner uses agent_dir/metrics.json (E2 contract preserved).
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.auto_metrics import AutoMetricsStream, record_gate2_result
from tools.metrics_collector import MetricsCollector
from tools.prompt_store import PromptStore


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> PromptStore:
    return PromptStore(store_path=tmp_path / "prompts.json", max_versions=3)


def _agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── AutoMetricsStream construction ───────────────────────────────────────────

class TestAutoMetricsStreamConstruction:
    def test_metrics_path_is_agent_dir_slash_metrics_json(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_collector_property_has_correct_path(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        assert stream.collector.metrics_path == agent_dir / "metrics.json"

    def test_metrics_path_is_not_interactive_default(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        assert stream.metrics_path != Path("metrics.json")

    def test_from_agent_dir_creates_directory(self, tmp_path):
        agent_dir = tmp_path / "brand_new" / ".agent"
        assert not agent_dir.exists()
        stream = AutoMetricsStream.from_agent_dir(agent_dir)
        assert agent_dir.exists()
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_from_agent_dir_idempotent(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        # Calling twice must not raise
        AutoMetricsStream.from_agent_dir(agent_dir)
        stream = AutoMetricsStream.from_agent_dir(agent_dir)
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_raises_when_path_collides_with_interactive(self, tmp_path, monkeypatch):
        """Guard: refuse to use the same path as the interactive metrics.json."""
        # Point CWD at tmp_path so Path("metrics.json").resolve() == tmp_path/metrics.json
        # Then pass tmp_path as agent_dir so agent_dir/metrics.json == that same path.
        monkeypatch.chdir(tmp_path)
        # agent_dir IS tmp_path → agent_dir/metrics.json == tmp_path/metrics.json
        # == Path("metrics.json").resolve()
        with pytest.raises(ValueError, match="collide"):
            AutoMetricsStream(tmp_path)


# ── record_gate2() ────────────────────────────────────────────────────────────

class TestRecordGate2:
    def test_approved_record_written(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="ok", attempts=2)
        records = stream.collector.load_recent(5)
        assert len(records) == 1
        r = records[0]
        assert r.validator_status == "approved"
        assert r.iterations_used == 2
        assert r.intent == "AUTO-T1"

    def test_rejected_record_written(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T2", approved=False, feedback="missing ref", attempts=5)
        records = stream.collector.load_recent(5)
        assert records[0].validator_status == "rejected"
        assert records[0].validator_feedback == "missing ref"
        assert records[0].iterations_used == 5

    def test_improvement_json_ok_is_always_none(self, tmp_path):
        """Core E2 isolation: auto records must not affect interactive json_fail rate."""
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="", attempts=1)
        stream.record_gate2("AUTO-T2", approved=False, feedback="bad", attempts=3)
        records = stream.collector.load_recent(10)
        for r in records:
            assert r.improvement_json_ok is None, (
                f"Record for {r.intent} has improvement_json_ok={r.improvement_json_ok!r}; "
                "must be None to stay out of interactive optimizer signal"
            )

    def test_prompt_version_from_store(self, tmp_path):
        ps = _make_store(tmp_path)
        ps.push("validator", "v1 prompt text", 0.8)
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="", attempts=1,
                            prompt_store=ps)
        records = stream.collector.load_recent(5)
        assert records[0].prompt_version == "v1"

    def test_prompt_version_defaults_to_auto_without_store(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="", attempts=1)
        records = stream.collector.load_recent(5)
        assert records[0].prompt_version == "auto"

    def test_multiple_records_accumulate(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        for i in range(4):
            stream.record_gate2(f"AUTO-T{i}", approved=(i % 2 == 0),
                                feedback="x", attempts=i + 1)
        records = stream.collector.load_recent(10)
        assert len(records) == 4

    def test_collector_error_swallowed(self, tmp_path):
        """Metric write failure must never propagate to the caller."""
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        with patch.object(stream.collector, "record", side_effect=OSError("disk full")):
            stream.record_gate2("AUTO-T1", approved=True, feedback="", attempts=1)
            # No exception raised — test passes if we get here


# ── Isolation — the core E2 guarantee ────────────────────────────────────────

class TestMetricsIsolation:
    def test_interactive_file_untouched_after_auto_record(self, tmp_path):
        """AC: interactive metrics.json must not exist after an auto run writes."""
        interactive_path = tmp_path / "metrics.json"
        assert not interactive_path.exists()

        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        stream.record_gate2("AUTO-T1", approved=True, feedback="ok", attempts=1)

        assert not interactive_path.exists(), (
            "interactive metrics.json was created by an auto-mode record — "
            "AUTO-E2 isolation violated"
        )

    def test_auto_file_distinct_from_interactive(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        assert stream.metrics_path.resolve() != (tmp_path / "metrics.json").resolve()

    def test_writing_to_interactive_stream_does_not_affect_auto(self, tmp_path):
        interactive_mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
        agent_dir = _agent_dir(tmp_path)
        auto_stream = AutoMetricsStream(agent_dir)

        from tools.metrics_collector import RunRecord
        from datetime import datetime, timezone
        interactive_mc.record(RunRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            intent="interactive-task",
            prompt_version="v0",
            iterations_used=1,
            validator_status="approved",
            validator_feedback="",
            improvement_json_ok=True,
            elapsed_seconds=0.5,
        ))

        # Auto stream must be empty — no cross-contamination
        assert auto_stream.collector.load_recent(10) == []

    def test_json_ok_none_excluded_from_failure_rate(self, tmp_path):
        """
        Verify that improvement_json_ok=None records don't inflate
        json_parse_failure_rate in the auto metrics summarize_failures() call.

        This is the MetricsCollector contract that makes auto-mode records
        safe to pass to the existing AutoTuner signal check.
        """
        agent_dir = _agent_dir(tmp_path)
        stream = AutoMetricsStream(agent_dir)
        # Write 10 records — all with improvement_json_ok=None
        for i in range(10):
            stream.record_gate2(f"AUTO-T{i}", approved=True, feedback="", attempts=1)

        summary = stream.collector.summarize_failures(10)
        # None records are excluded from the improvement_runs denominator
        assert summary["json_parse_failure_rate"] == 0.0, (
            f"Expected 0.0 json_parse_failure_rate for all-None records, "
            f"got {summary['json_parse_failure_rate']}"
        )

    def test_auto_records_do_not_affect_interactive_summarize(self, tmp_path):
        """
        Even if two collectors pointed at the same path (misconfigured), the
        None flag on improvement_json_ok prevents contamination of the
        interactive optimizer's failure rate.

        This is the belt-and-suspenders guarantee: path isolation is the primary
        defense; None flag is the secondary defense.
        """
        shared_path = tmp_path / "shared_metrics.json"
        interactive_mc = MetricsCollector(metrics_path=shared_path)
        auto_mc = MetricsCollector(metrics_path=shared_path)

        from tools.metrics_collector import RunRecord
        from datetime import datetime, timezone

        # Write a "clean" interactive record first (json_ok=True)
        interactive_mc.record(RunRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            intent="improve",
            prompt_version="v0",
            iterations_used=1,
            validator_status="approved",
            validator_feedback="",
            improvement_json_ok=True,
            elapsed_seconds=0.5,
        ))

        # Now write auto records via record_gate2_result (improvement_json_ok=None)
        for i in range(9):
            record_gate2_result(
                auto_mc, f"AUTO-T{i}", approved=True, feedback="", attempts_used=1
            )

        summary = interactive_mc.summarize_failures(10)
        # Only the 1 interactive record has json_ok set; it's True → failure rate = 0%
        assert summary["json_parse_failure_rate"] == 0.0


# ── module-level record_gate2_result (backward-compat) ───────────────────────

class TestModuleLevelRecord:
    def test_writes_approved_record(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto.json")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="fine", attempts_used=2)
        records = mc.load_recent(5)
        assert len(records) == 1
        assert records[0].validator_status == "approved"
        assert records[0].iterations_used == 2

    def test_writes_rejected_record(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto.json")
        record_gate2_result(mc, "AUTO-T2", approved=False, feedback="fail", attempts_used=5)
        records = mc.load_recent(5)
        assert records[0].validator_status == "rejected"

    def test_improvement_json_ok_is_none(self, tmp_path):
        mc = MetricsCollector(metrics_path=tmp_path / "auto.json")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1)
        assert mc.load_recent(5)[0].improvement_json_ok is None

    def test_prompt_version_from_store(self, tmp_path):
        ps = _make_store(tmp_path)
        ps.push("validator", "v1 prompt", 0.9)   # first push → label "v1"
        mc = MetricsCollector(metrics_path=tmp_path / "auto.json")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1,
                            prompt_store=ps)
        assert mc.load_recent(5)[0].prompt_version == "v1"

    def test_collector_error_swallowed(self, tmp_path):
        mc = MagicMock()
        mc.record.side_effect = RuntimeError("boom")
        record_gate2_result(mc, "AUTO-T1", approved=True, feedback="", attempts_used=1)
        # No exception — test passes if we reach here


# ── auto_tuner.py re-export (back-compat) ────────────────────────────────────

class TestAutoTunerReexport:
    def test_record_gate2_result_importable_from_auto_tuner(self):
        """Existing callers that import from auto_tuner must not break."""
        from tools.auto.auto_tuner import record_gate2_result as fn
        assert callable(fn)

    def test_auto_tuner_reexport_is_same_object(self):
        from tools.auto.auto_tuner import record_gate2_result as from_tuner
        from tools.auto.auto_metrics import record_gate2_result as from_metrics
        assert from_tuner is from_metrics

    def test_make_auto_tuner_uses_agent_dir_metrics(self, tmp_path):
        """make_auto_tuner must keep the E2 contract: metrics in agent_dir."""
        from tools.auto.auto_tuner import make_auto_tuner

        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {"enabled": "true"}
        cfg["prompt_store"] = {"store_path": str(tmp_path / "prompts.json")}
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {
            "base_url": "http://localhost:1337/v1",
            "api_key": "",
            "model": "m",
            "api_format": "openai",
        }
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)

        expected = agent_dir / "metrics.json"
        assert tuner.metrics.metrics_path == expected

    def test_make_auto_tuner_metrics_not_at_interactive_default(self, tmp_path):
        from tools.auto.auto_tuner import make_auto_tuner

        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {"enabled": "true"}
        cfg["prompt_store"] = {"store_path": str(tmp_path / "prompts.json")}
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {
            "base_url": "http://localhost:1337/v1",
            "api_key": "",
            "model": "m",
            "api_format": "openai",
        }
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)

        assert tuner.metrics.metrics_path != Path("metrics.json")
        assert ".agent" in str(tuner.metrics.metrics_path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
