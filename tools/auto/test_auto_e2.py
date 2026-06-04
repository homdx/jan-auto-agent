"""tests/test_auto_e2.py — AUTO-E2: separate auto-mode metrics stream.

Story AC (from the Jira backlog):
  * Auto-run metrics go to their own store (.agent/metrics.json) so they
    don't pollute the interactive optimizer's signal.
  * AC: interactive metrics.json untouched by auto runs.

Coverage
--------
AutoMetricsStream construction
  * metrics_path is agent_dir/metrics.json, never the CWD default.
  * __init__ creates agent_dir (and parents) if they do not exist.  [from draft]
  * from_agent_dir() also creates the directory.
  * Raises ValueError when agent_dir is the project root (collision guard).
  * .collector property returns a MetricsCollector pointed at the right path.
  * .metrics_path property matches the underlying collector path.

record_gate2() write path
  * Approved record written correctly (status, iterations, intent).
  * Rejected record written correctly (status, feedback).
  * improvement_json_ok is always None (never pollutes interactive rate).
  * prompt_version recorded from PromptStore when supplied.
  * prompt_version falls back to "auto" when no store supplied.
  * Multiple records accumulate correctly.
  * Errors in the underlying collector are swallowed (fail-closed).

flush()
  * flush() does not raise and does not wipe existing records.  [from draft pattern]

Contamination guard  [from draft: TestSourceGuard]
  * Warns (but does not crash) if file has non-None improvement_json_ok records.
  * Corrupt / non-array JSON in the file does not crash __init__.

Thread safety  [from draft: TestThreadSafety / AC-7]
  * Concurrent record_gate2() calls produce a consistent total — no lost writes,
    no file corruption.

Isolation — the core E2 guarantee
  * Interactive metrics.json untouched after recording to auto stream.
  * Auto stream path never equals Path("metrics.json") resolved.
  * Writing to interactive stream has no effect on auto stream.
  * summarize_failures() on auto stream excludes improvement_json_ok=None
    records from json_parse_failure_rate.
  * Auto records do not affect interactive summarize_failures (None flag).

module-level record_gate2_result (backward-compat)
  * Writes to an arbitrary MetricsCollector.
  * improvement_json_ok always None.
  * Swallows collector errors.

auto_tuner.py re-export
  * record_gate2_result importable from tools.auto.auto_tuner (back-compat).
  * Same object as the auto_metrics version.
  * make_auto_tuner uses agent_dir/metrics.json (E2 contract preserved).
"""

from __future__ import annotations

import configparser
import json
import sys
import threading
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
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        assert stream.metrics_path == tmp_path / ".agent" / "metrics.json"

    def test_collector_property_has_correct_path(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        assert stream.collector.metrics_path == tmp_path / ".agent" / "metrics.json"

    def test_metrics_path_is_not_interactive_default(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        assert stream.metrics_path != Path("metrics.json")

    # ── from draft: __init__ must create missing directories ──────────────

    def test_init_creates_agent_dir_if_missing(self, tmp_path):
        """__init__ (not just from_agent_dir) must create the directory."""
        missing = tmp_path / "brand_new" / ".agent"
        assert not missing.exists()
        AutoMetricsStream(missing)
        assert missing.exists()
        assert (missing / "metrics.json").parent.exists()

    def test_init_creates_nested_parents(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / ".agent"
        AutoMetricsStream(deep)   # must not raise
        assert deep.exists()

    def test_from_agent_dir_creates_directory(self, tmp_path):
        agent_dir = tmp_path / "brand_new" / ".agent"
        assert not agent_dir.exists()
        stream = AutoMetricsStream.from_agent_dir(agent_dir)
        assert agent_dir.exists()
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_from_agent_dir_idempotent(self, tmp_path):
        agent_dir = _agent_dir(tmp_path)
        AutoMetricsStream.from_agent_dir(agent_dir)
        stream = AutoMetricsStream.from_agent_dir(agent_dir)
        assert stream.metrics_path == agent_dir / "metrics.json"

    def test_raises_when_path_collides_with_interactive(self, tmp_path, monkeypatch):
        """Guard: refuse to use the same path as the interactive metrics.json."""
        monkeypatch.chdir(tmp_path)
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
        for r in stream.collector.load_recent(10):
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


# ── flush() ──────────────────────────────────────────────────────────────────

class TestFlush:
    """Extracted from draft pattern — flush() should be safe and idempotent."""

    def test_flush_does_not_raise(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.flush()   # must not raise

    def test_flush_does_not_wipe_records(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="ok", attempts=1)
        stream.flush()
        records = stream.collector.load_recent(10)
        assert len(records) == 1

    def test_flush_idempotent(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.flush()
        stream.flush()
        stream.flush()


# ── Contamination guard  [from draft: TestSourceGuard] ───────────────────────

class TestContaminationGuard:
    def test_warns_when_file_has_non_none_json_ok(self, tmp_path, caplog):
        """
        If .agent/metrics.json contains interactive records (improvement_json_ok
        not None), __init__ should log a warning.
        """
        agent_dir = _agent_dir(tmp_path)
        contaminated = [
            {
                "timestamp": "2024-01-01T00:00:00+00:00",
                "intent": "improve",
                "prompt_version": "v0",
                "iterations_used": 1,
                "validator_status": "approved",
                "validator_feedback": "",
                "improvement_json_ok": True,   # interactive record
                "elapsed_seconds": 0.5,
            }
        ]
        (agent_dir / "metrics.json").write_text(json.dumps(contaminated))

        import logging
        with caplog.at_level(logging.WARNING, logger="tools.auto.auto_metrics"):
            AutoMetricsStream(agent_dir)

        assert any("contamination" in m.lower() or "improvement_json_ok" in m
                   for m in caplog.messages), (
            "Expected a contamination warning; got: " + str(caplog.messages)
        )

    def test_no_warning_for_clean_auto_records(self, tmp_path, caplog):
        """Auto records (improvement_json_ok=None) must not trigger the warning."""
        agent_dir = _agent_dir(tmp_path)
        clean = [
            {
                "timestamp": "2024-01-01T00:00:00+00:00",
                "intent": "AUTO-T1",
                "prompt_version": "auto",
                "iterations_used": 1,
                "validator_status": "approved",
                "validator_feedback": "",
                "improvement_json_ok": None,
                "elapsed_seconds": 0.0,
            }
        ]
        (agent_dir / "metrics.json").write_text(json.dumps(clean))

        import logging
        with caplog.at_level(logging.WARNING, logger="tools.auto.auto_metrics"):
            AutoMetricsStream(agent_dir)

        assert not any("contamination" in m.lower() for m in caplog.messages)

    def test_corrupt_json_does_not_crash_init(self, tmp_path):
        """[from draft: test_corrupt_json_starts_fresh] — corrupt file must not raise."""
        agent_dir = _agent_dir(tmp_path)
        (agent_dir / "metrics.json").write_text("not json {{{{")
        AutoMetricsStream(agent_dir)   # must not raise

    def test_non_array_json_does_not_crash_init(self, tmp_path):
        """A metrics.json that is a dict (wrong format) must not crash __init__."""
        agent_dir = _agent_dir(tmp_path)
        (agent_dir / "metrics.json").write_text(json.dumps({"source": "interactive"}))
        AutoMetricsStream(agent_dir)   # must not raise


# ── Thread safety  [from draft: TestThreadSafety / AC-7] ─────────────────────

class TestThreadSafety:
    def test_concurrent_record_gate2_consistent_total(self, tmp_path):
        """
        [from draft] Concurrent record_gate2() calls must produce a consistent
        total with no lost writes and no file corruption.

        Without the threading.Lock in AutoMetricsStream, MetricsCollector's
        unprotected read-modify-write produces a race that drops >95% of writes.
        """
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        n_threads = 20
        records_each = 50
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for i in range(records_each):
                    stream.record_gate2(
                        f"AUTO-T{i}",
                        approved=(i % 2 == 0),
                        feedback="x",
                        attempts=1,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        records = stream.collector.load_recent(n_threads * records_each + 100)
        assert len(records) == n_threads * records_each, (
            f"Expected {n_threads * records_each} records, "
            f"got {len(records)} — lock may be missing"
        )

    def test_concurrent_all_improvement_json_ok_none(self, tmp_path):
        """Thread-safe writes must all preserve the improvement_json_ok=None invariant."""
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        n = 10

        def worker():
            for i in range(n):
                stream.record_gate2(f"T{i}", approved=True, feedback="", attempts=1)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        for r in stream.collector.load_recent(1000):
            assert r.improvement_json_ok is None


# ── Isolation — the core E2 guarantee ────────────────────────────────────────

class TestMetricsIsolation:
    def test_interactive_file_untouched_after_auto_record(self, tmp_path):
        """AC: interactive metrics.json must not exist after an auto run writes."""
        interactive_path = tmp_path / "metrics.json"
        assert not interactive_path.exists()

        stream = AutoMetricsStream(_agent_dir(tmp_path))
        stream.record_gate2("AUTO-T1", approved=True, feedback="ok", attempts=1)

        assert not interactive_path.exists(), (
            "interactive metrics.json was created by an auto-mode record — "
            "AUTO-E2 isolation violated"
        )

    def test_auto_file_distinct_from_interactive(self, tmp_path):
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        assert stream.metrics_path.resolve() != (tmp_path / "metrics.json").resolve()

    def test_writing_to_interactive_stream_does_not_affect_auto(self, tmp_path):
        from tools.metrics_collector import RunRecord
        from datetime import datetime, timezone

        interactive_mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
        auto_stream = AutoMetricsStream(_agent_dir(tmp_path))

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

        assert auto_stream.collector.load_recent(10) == []

    def test_json_ok_none_excluded_from_failure_rate(self, tmp_path):
        """
        improvement_json_ok=None records don't inflate json_parse_failure_rate
        in summarize_failures() — the invariant that makes auto records safe
        for AutoTuner's signal check.
        """
        stream = AutoMetricsStream(_agent_dir(tmp_path))
        for i in range(10):
            stream.record_gate2(f"AUTO-T{i}", approved=True, feedback="", attempts=1)

        summary = stream.collector.summarize_failures(10)
        assert summary["json_parse_failure_rate"] == 0.0

    def test_auto_records_do_not_affect_interactive_summarize(self, tmp_path):
        """
        Belt-and-suspenders: even if two collectors share a path (misconfigured),
        the None flag prevents the interactive optimizer from seeing auto records.
        """
        shared = tmp_path / "shared.json"
        interactive_mc = MetricsCollector(metrics_path=shared)
        auto_mc = MetricsCollector(metrics_path=shared)

        from tools.metrics_collector import RunRecord
        from datetime import datetime, timezone

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

        for i in range(9):
            record_gate2_result(
                auto_mc, f"AUTO-T{i}", approved=True, feedback="", attempts_used=1
            )

        summary = interactive_mc.summarize_failures(10)
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
        assert mc.load_recent(5)[0].validator_status == "rejected"

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


# ── auto_tuner.py re-export (back-compat) ────────────────────────────────────

class TestAutoTunerReexport:
    def test_record_gate2_result_importable_from_auto_tuner(self):
        from tools.auto.auto_tuner import record_gate2_result as fn
        assert callable(fn)

    def test_auto_tuner_reexport_is_same_object(self):
        from tools.auto.auto_tuner import record_gate2_result as from_tuner
        from tools.auto.auto_metrics import record_gate2_result as from_metrics
        assert from_tuner is from_metrics

    def test_make_auto_tuner_uses_agent_dir_metrics(self, tmp_path):
        from tools.auto.auto_tuner import make_auto_tuner

        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {"enabled": "true"}
        cfg["prompt_store"] = {"store_path": str(tmp_path / "prompts.json")}
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {
            "base_url": "http://localhost:1337/v1", "api_key": "",
            "model": "m", "api_format": "openai",
        }
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)
        assert tuner.metrics.metrics_path == agent_dir / "metrics.json"

    def test_make_auto_tuner_metrics_not_at_interactive_default(self, tmp_path):
        from tools.auto.auto_tuner import make_auto_tuner

        cfg = configparser.ConfigParser()
        cfg["prompt_optimizer"] = {"enabled": "true"}
        cfg["prompt_store"] = {"store_path": str(tmp_path / "prompts.json")}
        cfg["api"] = {"active": "local", "verify_ssl": "false"}
        cfg["api_local"] = {
            "base_url": "http://localhost:1337/v1", "api_key": "",
            "model": "m", "api_format": "openai",
        }
        cfg["loop"] = {"max_iterations": "3", "timeout_seconds": "60"}

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        tuner = make_auto_tuner(cfg, agent_dir)
        assert tuner.metrics.metrics_path != Path("metrics.json")
        assert ".agent" in str(tuner.metrics.metrics_path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
