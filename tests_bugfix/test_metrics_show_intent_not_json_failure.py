"""tests_bugfix/test_metrics_show_intent_not_json_failure.py

RunRecord types ``improvement_json_ok`` as ``Optional[bool]`` and documents
None as "not applicable (show/show_imports); excluded from rate";
MetricsCollector.summarize_failures honours that with an ``is not None``
filter before computing json_parse_failure_rate.

run_pipeline violated its own contract: it always passed a bool, so a
show/show_imports run — which never invokes the improvement agent at all —
was recorded as False and counted as a JSON parse failure. Ten "show" runs
in a row drove json_parse_failure_rate to 1.0 and fired the PromptOptimizer
against the validator prompt on evidence that did not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.metrics_collector import MetricsCollector, RunRecord


def _record(collector: MetricsCollector, intent: str, json_ok) -> None:
    collector.record(RunRecord(
        timestamp="2026-01-01 00:00:00",
        intent=intent,
        prompt_version="v1",
        iterations_used=1,
        validator_status="skipped" if json_ok is None else "passed",
        validator_feedback="",
        improvement_json_ok=json_ok,
        elapsed_seconds=0.1,
    ))


def test_none_is_excluded_from_json_failure_rate(tmp_path):
    """The contract the fix relies on: None runs never move the rate."""
    c = MetricsCollector(metrics_path=tmp_path / "m.jsonl")
    for _ in range(9):
        _record(c, "show", None)
    _record(c, "improve", True)
    assert c.summarize_failures(n=10)["json_parse_failure_rate"] == 0.0


def test_false_still_counts_as_a_failure(tmp_path):
    """Guard against over-correcting: a real improvement run that produced
    nothing parseable must still register."""
    c = MetricsCollector(metrics_path=tmp_path / "m.jsonl")
    _record(c, "improve", False)
    _record(c, "improve", True)
    assert c.summarize_failures(n=10)["json_parse_failure_rate"] == 0.5


def test_run_pipeline_records_none_for_show_intents():
    """main.py must actually pass None, not False, for the non-improvement
    intents — the bug was in the caller, not in the collector."""
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "_improvement_ran = parsed.intent in (\"improve\", \"explain\", \"show_and_improve\")" in source
    assert "if _improvement_ran else None" in source
