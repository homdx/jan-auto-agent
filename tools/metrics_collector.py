import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

METRICS_PATH = Path("metrics.json")


@dataclass
class RunRecord:
    timestamp: str
    intent: str
    prompt_version: str
    iterations_used: int
    validator_status: str
    validator_feedback: str
    improvement_json_ok: Optional[bool]  # None = not applicable (show/show_imports); excluded from rate
    elapsed_seconds: float


class MetricsCollector:
    def __init__(self, metrics_path: Path = METRICS_PATH):
        self.metrics_path = metrics_path

    def record(self, run: RunRecord) -> None:
        """Append a RunRecord to metrics.json, creating the file if needed."""
        records = self._load_all()
        records.append(asdict(run))
        try:
            with open(self.metrics_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        except Exception as e:
            logger.error(f"MetricsCollector failed to write metrics: {e}")

    def load_recent(self, n: int) -> List[RunRecord]:
        """Return the last N RunRecord entries."""
        records = self._load_all()
        recent = records[-n:] if len(records) >= n else records
        return [RunRecord(**r) for r in recent]

    def summarize_failures(self, n: int) -> Dict[str, Any]:
        """
        Return a structured failure summary over the last N runs.

        Keys:
          total_runs          - number of records analysed
          avg_iterations      - mean iterations_used across those runs
          json_parse_failure_rate - fraction where improvement_json_ok is False
          common_feedback     - top-3 recurring phrases from validator_feedback
          worst_intent        - intent with the highest avg iteration count
        """
        records = self._load_all()
        window = records[-n:] if len(records) >= n else records
        total = len(window)

        if total == 0:
            return {
                "total_runs": 0,
                "avg_iterations": 0.0,
                "json_parse_failure_rate": 0.0,
                "common_feedback": [],
                "worst_intent": None,
            }

        avg_iterations = sum(r["iterations_used"] for r in window) / total

        # Bug #8 fix: only count runs where improvement was actually attempted
        # (improvement_json_ok is None for show/show_imports — exclude them so
        # non-improvement intents don't inflate the failure rate and trigger
        # the optimizer spuriously).
        improvement_runs = [r for r in window if r.get("improvement_json_ok") is not None]
        if improvement_runs:
            json_failures = sum(1 for r in improvement_runs if not r["improvement_json_ok"])
            json_parse_failure_rate = round(json_failures / len(improvement_runs), 4)
        else:
            json_parse_failure_rate = 0.0

        # Word-frequency count over all non-empty feedback strings
        all_feedback = " ".join(
            r["validator_feedback"] for r in window if r.get("validator_feedback")
        )
        words = re.findall(r"\b[a-zA-Z_][\w_]{3,}\b", all_feedback)  # tokens ≥ 4 chars
        stop_words = {
            "that", "this", "with", "from", "have", "been", "will", "your",
            "more", "than", "also", "into", "some", "code", "block", "function",
            "should", "would", "could", "does", "which", "their",
        }
        meaningful = [w.lower() for w in words if w.lower() not in stop_words]
        top_phrases = [phrase for phrase, _ in Counter(meaningful).most_common(3)]

        # Intent with the highest average iteration count
        intent_iters: Dict[str, List[int]] = {}
        for r in window:
            intent_iters.setdefault(r["intent"], []).append(r["iterations_used"])
        worst_intent = max(intent_iters, key=lambda k: sum(intent_iters[k]) / len(intent_iters[k]))

        return {
            "total_runs": total,
            "avg_iterations": round(avg_iterations, 3),
            "json_parse_failure_rate": json_parse_failure_rate,
            "common_feedback": top_phrases,
            "worst_intent": worst_intent,
        }

    def _load_all(self) -> list:
        if not self.metrics_path.exists():
            return []
        try:
            with open(self.metrics_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"MetricsCollector failed to read metrics: {e}")
            return []
