import json
import logging
import os
import re
import tempfile
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
        # Perf cache for record(): avoids a full read+parse of the whole
        # file on every single call. Without this, N sequential record()
        # calls (e.g. from many worker threads serialised through a caller's
        # lock, as in AutoMetricsStream) cost O(N^2) instead of O(N), which
        # is slow enough under CPU contention (parallel test runners, etc.)
        # to look like a hang/deadlock even though no lock is actually
        # circularly held.
        # Invalidated automatically if the file's mtime changes underneath
        # us (e.g. another process writes it), so correctness for
        # multi-process access is unaffected — this only skips the re-read
        # when we already know the in-memory copy matches disk.
        self._cache: Optional[list] = None
        self._cache_mtime: Optional[float] = None

    def _load_all_cached(self) -> list:
        try:
            mtime = self.metrics_path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        records = self._load_all()
        self._cache = records
        self._cache_mtime = mtime
        return records

    def record(self, run: RunRecord) -> None:
        """Append a RunRecord to metrics.json, creating the file if needed.

        The write is atomic: data is serialised to a sibling temp file first,
        then renamed over the target with os.replace().  A crash mid-write
        therefore leaves the previous metrics.json intact rather than producing
        a truncated file that would silence the prompt optimizer on the next run.
        """
        records = self._load_all_cached()
        records = list(records)  # don't mutate the cached list in place
        records.append(asdict(run))
        try:
            dir_ = self.metrics_path.parent
            dir_.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_, prefix=".metrics_tmp_", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(records, f, indent=2)
            except Exception:
                os.unlink(tmp_path)
                raise
            os.replace(tmp_path, self.metrics_path)
            self._cache = records
            try:
                self._cache_mtime = self.metrics_path.stat().st_mtime
            except OSError:
                self._cache = None
                self._cache_mtime = None
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
        """Load all recorded runs, or [] if the file is missing or unusable.

        BUGFIX: an unusable file used to be silently treated as "empty", and
        record() unconditionally appends and re-saves at the end of every
        call — so the VERY NEXT record(), for any run, overwrote the whole
        file with just that one new entry, destroying every prior run's
        metrics with no exception raised and only a log.error line easy to
        miss over a long autonomous session:

            BEFORE: 3 real run records on disk
            AFTER one record() call on top of a corrupt file: 1 record(s)

        The same defect as PromptStore._load (tools/prompt_store.py) — same
        author, same era, and this module's own docstring is what
        prompt_store's atomic-write comment cites as the pattern to imitate
        for the WRITE side; the read side had the identical gap.  This is
        the fourth instance of this exact shape found this session (ticket
        store, plan.json, progress.json, prompt_store.py); it is a systemic
        pattern in this codebase's hand-rolled JSON stores, not an isolated
        mistake, and other JSON-backed stores are worth the same check.

        Metrics feed the prompt optimizer's summarize_failures(), not live
        task/commit state, so history here (like prompt_store's) is not
        derivable from anywhere else and not correctness-critical — the fix
        quarantines rather than raising, so a damaged file degrades the
        optimizer's view instead of crashing a run over telemetry.

        Also widens the exception match to catch a file that PARSES but
        holds the wrong shape (a dict, a string, null instead of a list),
        which used to sail through here and crash later, inside record(),
        with an unhelpful AttributeError:

            dict   RAISED AttributeError: 'dict' object has no attribute 'append'
            string RAISED AttributeError: 'str' object has no attribute 'append'
        """
        if not self.metrics_path.exists():
            return []
        try:
            with open(self.metrics_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            self._quarantine(str(e))
            return []
        if not isinstance(data, list):
            self._quarantine(f"expected a JSON array, got {type(data).__name__}")
            return []
        return data

    def _quarantine(self, reason: str) -> None:
        """Move an unusable metrics.json aside and log why.

        Same pattern as TicketStore.get() / PromptStore._load: rename rather
        than delete, so the damaged original is preserved for inspection,
        and rename rather than leave-in-place, so the very next record()
        starts a fresh file instead of landing on top of (and destroying)
        whatever was still recoverable in the original.
        """
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
        dest  = self.metrics_path.with_suffix(f".json.corrupt-{stamp}")
        # Same-second collision guard — see TicketStore._quarantine for the
        # reasoning and the reproduction. Narrower here than the ticket case
        # (there is only one metrics_path, not one per id), but reproducible
        # the same way: two quarantines of this file within the same
        # wall-clock second otherwise silently overwrite each other.
        suffix_n = 0
        while dest.exists():
            suffix_n += 1
            dest = self.metrics_path.with_suffix(f".json.corrupt-{stamp}-{suffix_n:03d}")
        try:
            self.metrics_path.rename(dest)
            logger.warning(
                "MetricsCollector: %s is unusable (%s) — quarantined as %s; "
                "starting a fresh metrics file (prior history is preserved "
                "in the quarantined file, not lost)",
                self.metrics_path.name, reason, dest.name,
            )
        except OSError as exc:
            logger.error(
                "MetricsCollector: %s is unusable (%s) and could not be "
                "quarantined (%s) — the next record() will overwrite it",
                self.metrics_path.name, reason, exc,
            )
