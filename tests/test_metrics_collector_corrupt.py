"""tests/test_metrics_collector_corrupt.py — an unusable metrics.json must
not silently destroy prior run history.

The fourth instance of the same defect found this session (ticket store,
plan.json, progress.json, prompt_store.py): _load_all() treated an
unreadable file as "empty", and record() unconditionally appends and
re-saves at the end of every call — so the very next record(), for any run,
overwrote the whole file with just that one new entry:

    BEFORE: 3 real run records on disk
    AFTER one record() call on top of a corrupt file: 1 record(s)

No exception was raised; only a log.error line. _load_all also only
guarded JSONDecodeError/IOError, so a file that PARSES but holds the wrong
shape (a dict, a string, null instead of a list) crashed later inside
record() with an unhelpful AttributeError.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.metrics_collector import MetricsCollector, RunRecord


def _record(timestamp: str) -> RunRecord:
    return RunRecord(
        timestamp=timestamp, intent="i", prompt_version="1", iterations_used=1,
        validator_status="ok", validator_feedback="", improvement_json_ok=True,
        elapsed_seconds=1.0,
    )


def _seeded_metrics(tmp_path: Path) -> Path:
    mp = tmp_path / "metrics.json"
    mp.write_text(json.dumps([
        {"timestamp": "t1", "intent": "a", "prompt_version": "1",
         "iterations_used": 2, "validator_status": "ok", "validator_feedback": "",
         "improvement_json_ok": True, "elapsed_seconds": 1.0},
        {"timestamp": "t2", "intent": "b", "prompt_version": "1",
         "iterations_used": 3, "validator_status": "ok", "validator_feedback": "",
         "improvement_json_ok": True, "elapsed_seconds": 2.0},
        {"timestamp": "t3", "intent": "c", "prompt_version": "2",
         "iterations_used": 1, "validator_status": "ok", "validator_feedback": "",
         "improvement_json_ok": True, "elapsed_seconds": 1.5},
    ]), encoding="utf-8")
    return mp


class TestCorruptFileNoLongerDestroysHistory:
    def test_record_after_corruption_does_not_wipe_prior_runs(self, tmp_path):
        mp = _seeded_metrics(tmp_path)
        mp.write_text("{ corrupted mid-write", encoding="utf-8")

        MetricsCollector(metrics_path=mp).record(_record("t4"))

        result = json.loads(mp.read_text(encoding="utf-8"))
        # The bug: this used to be [{"timestamp": "t4", ...}] with t1-t3 gone.
        assert [r["timestamp"] for r in result] == ["t4"]
        quarantined = list(tmp_path.glob("*.corrupt-*"))
        assert len(quarantined) == 1

    def test_quarantined_file_preserves_original_bytes(self, tmp_path):
        mp = _seeded_metrics(tmp_path)
        original = "{ corrupted, exact bytes matter"
        mp.write_text(original, encoding="utf-8")

        MetricsCollector(metrics_path=mp).record(_record("t4"))

        quarantined = list(tmp_path.glob("*.corrupt-*"))[0]
        assert quarantined.read_text(encoding="utf-8") == original

    def test_valid_file_is_never_quarantined(self, tmp_path):
        mp = _seeded_metrics(tmp_path)
        MetricsCollector(metrics_path=mp).record(_record("t4"))
        assert list(tmp_path.glob("*.corrupt-*")) == []
        result = json.loads(mp.read_text(encoding="utf-8"))
        assert [r["timestamp"] for r in result] == ["t1", "t2", "t3", "t4"]


class TestWrongShapeNoLongerCrashes:
    @pytest.mark.parametrize("content,label", [
        ("{}", "dict"),
        ('"a string"', "string"),
        ("null", "null"),
        ("42", "number"),
        ("", "empty file"),
    ])
    def test_record_survives_wrong_shape(self, tmp_path, content, label):
        mp = tmp_path / "metrics.json"
        mp.write_text(content, encoding="utf-8")
        MetricsCollector(metrics_path=mp).record(_record("t1"))   # must not raise
        result = json.loads(mp.read_text(encoding="utf-8"))
        assert [r["timestamp"] for r in result] == ["t1"], label

    def test_load_recent_survives_wrong_shape(self, tmp_path):
        mp = tmp_path / "metrics.json"
        mp.write_text("{}", encoding="utf-8")
        result = MetricsCollector(metrics_path=mp).load_recent(5)
        assert result == []

    def test_summarize_failures_survives_wrong_shape(self, tmp_path):
        mp = tmp_path / "metrics.json"
        mp.write_text("null", encoding="utf-8")
        result = MetricsCollector(metrics_path=mp).summarize_failures(5)
        assert result["total_runs"] == 0


class TestMissingFileStillWorks:
    def test_first_ever_record_creates_the_file(self, tmp_path):
        mp = tmp_path / "metrics.json"
        assert not mp.exists()
        MetricsCollector(metrics_path=mp).record(_record("t1"))
        assert mp.exists()
        assert list(tmp_path.glob("*.corrupt-*")) == []
