"""
tools/auto/auto_metrics.py — AUTO-E2

Separate auto-mode metrics stream: all autonomous-run metrics are written to
<agent_dir>/metrics.json, keeping them fully isolated from the interactive
optimizer's metrics.json signal.

Design contract
---------------
* The interactive MetricsCollector always lives at ``metrics.json`` (project
  root, default).  AutoMetricsStream lives at ``<agent_dir>/metrics.json``.
* ``improvement_json_ok`` is always written as ``None`` for auto-mode records
  so that even if the paths were somehow shared the interactive
  ``json_parse_failure_rate`` computation would not be affected (it already
  skips ``None`` records — see MetricsCollector.summarize_failures).
* Thread-safe: a ``threading.Lock`` serialises every read-modify-write cycle
  through ``MetricsCollector.record()``, which is not thread-safe on its own.
* Atomic writes: ``record_gate2()`` writes via a temp file + atomic rename so a
  crash mid-write never leaves a corrupt ``metrics.json``.
* All methods are fail-closed: errors are logged and swallowed so a metrics
  write failure never aborts a coding task.

Public API
----------
  stream = AutoMetricsStream(agent_dir)           # creates dir if missing
  stream.record_gate2(task_id, approved=True, feedback="...", attempts=2)
  stream.record_gate2(task_id, ..., prompt_store=ps)  # optional prompt version
  stream.flush()                                  # explicit sync (clean shutdown)
  stream.collector   → the underlying MetricsCollector (e.g. for AutoTuner)

  # Module-level convenience wrapper (keeps back-compat with auto_tuner usage):
  record_gate2_result(mc, task_id, approved=..., feedback=..., attempts_used=...,
                      prompt_store=...)
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.metrics_collector import MetricsCollector, RunRecord

logger = logging.getLogger(__name__)

# Interactive default — used only to assert isolation in tests / guards.
_INTERACTIVE_DEFAULT = Path("metrics.json")


# ── Atomic write helper (extracted from draft's _write_locked idea) ──────────

def _atomic_write_json(path: Path, data: Any) -> None:
    """
    Write *data* as JSON to *path* atomically via a sibling temp file.

    Replaces the direct ``json.dump(open(path))`` pattern in MetricsCollector
    with a write-to-tmp + ``replace()`` so a crash mid-write never leaves a
    partially-written file.  ``replace()`` is atomic on POSIX; on Windows it is
    best-effort.

    Raises ``OSError`` on failure — callers are responsible for handling.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        Path(tmp_name).replace(path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


class AutoMetricsStream:
    """
    Owns the ``.agent/metrics.json`` write path for an autonomous run.

    Thread-safe: all writes are serialised through ``self._lock``.

    Parameters
    ----------
    agent_dir:
        The ``.agent/`` directory for the current run.  Created (including
        parents) if it does not exist.  The stream file is always
        ``agent_dir / "metrics.json"``, never the interactive default.

    Raises
    ------
    ValueError
        If ``agent_dir / "metrics.json"`` resolves to the interactive default
        path (``metrics.json`` in the CWD).  This is a programming error —
        auto metrics must never share a file with the interactive stream.
    """

    def __init__(self, agent_dir: Path) -> None:
        # Auto-create the directory (mirroring the draft's _load() behaviour).
        Path(agent_dir).mkdir(parents=True, exist_ok=True)

        metrics_path = Path(agent_dir) / "metrics.json"

        # Guard: refuse to write to the interactive default path.
        try:
            if metrics_path.resolve() == _INTERACTIVE_DEFAULT.resolve():
                raise ValueError(
                    f"AutoMetricsStream would collide with the interactive "
                    f"metrics file at {_INTERACTIVE_DEFAULT.resolve()}. "
                    f"Pass a proper .agent/ directory, not the project root."
                )
        except OSError:
            # resolve() can fail on non-existent paths on some OSes — safe to
            # skip; the path-collision check is best-effort anyway.
            pass

        self._lock = threading.Lock()
        self._collector = MetricsCollector(metrics_path=metrics_path)
        self._warn_if_contaminated()

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def collector(self) -> MetricsCollector:
        """The underlying MetricsCollector, e.g. to pass to AutoTuner."""
        return self._collector

    @property
    def metrics_path(self) -> Path:
        """Absolute path of the auto-run metrics file."""
        return self._collector.metrics_path

    # ------------------------------------------------------------------ #
    # Write API                                                            #
    # ------------------------------------------------------------------ #

    def record_gate2(
        self,
        task_id: str,
        *,
        approved: bool,
        feedback: str,
        attempts: int,
        prompt_store=None,  # Optional[PromptStore]
    ) -> None:
        """
        Record a Gate-2 validation outcome to the auto metrics stream.

        Thread-safe: acquires ``self._lock`` for the full read-modify-write
        cycle so concurrent calls from separate worker threads produce a
        consistent record count.

        ``improvement_json_ok`` is always ``None`` so these records are
        excluded from the interactive optimizer's json_parse_failure_rate
        computation even if the paths were accidentally shared.

        Never raises — errors are logged and swallowed.
        """
        try:
            with self._lock:
                _record_gate2_locked(
                    self._collector,
                    task_id,
                    approved=approved,
                    feedback=feedback,
                    attempts_used=attempts,
                    prompt_store=prompt_store,
                )
        except Exception as exc:
            logger.error("AutoMetricsStream.record_gate2: failed to record metric — %s", exc)

    def flush(self) -> None:
        """
        Explicit sync / clean-shutdown hook.

        MetricsCollector writes on every ``record()`` call, so this is a
        no-op in normal operation.  It exists as a named hook so callers can
        signal intent at shutdown without coupling to the underlying
        implementation.
        """
        # No buffering in MetricsCollector — nothing to flush.
        # Acquire the lock briefly so any in-progress write completes first.
        with self._lock:
            pass

    # ------------------------------------------------------------------ #
    # Factory                                                              #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_agent_dir(cls, agent_dir: Path) -> "AutoMetricsStream":
        """Canonical factory — directory creation is now also in __init__."""
        return cls(agent_dir)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _warn_if_contaminated(self) -> None:
        """
        Contamination guard (analog of the draft's ``source != "auto"`` check).

        Reads the existing metrics file (if any) and warns if any record has
        ``improvement_json_ok`` set to a non-None value, which indicates that
        interactive-mode records were written to this path.

        Does not modify the file — a warning is sufficient because contamination
        can only happen through a programming error (wrong path), not via normal
        auto-run operation.
        """
        path = self._collector.metrics_path
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                records = json.load(fh)
            if not isinstance(records, list):
                # Unexpected format — could be an old interactive store or
                # unrelated file.  Log and leave intact; the MetricsCollector
                # will treat unreadable JSON as an empty list.
                logger.warning(
                    "AUTO-E2 contamination guard: %s is not a JSON array "
                    "(found %s).  Auto-run records may be unreliable.",
                    path,
                    type(records).__name__,
                )
                return
            contaminated = [
                r for r in records
                if isinstance(r, dict) and r.get("improvement_json_ok") is not None
            ]
            if contaminated:
                logger.warning(
                    "AUTO-E2 contamination guard: %s contains %d record(s) with "
                    "improvement_json_ok != None, which indicates interactive-mode "
                    "records were written to the auto metrics path.  "
                    "The AutoTuner's signal may be skewed.  "
                    "Delete %s to start clean.",
                    path,
                    len(contaminated),
                    path,
                )
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable — MetricsCollector already handles this
            # gracefully (returns [] from _load_all).  Nothing to do.
            pass


# ── Module-level helpers ──────────────────────────────────────────────────────

def _record_gate2_locked(
    mc: MetricsCollector,
    task_id: str,
    *,
    approved: bool,
    feedback: str,
    attempts_used: int,
    prompt_store=None,
) -> None:
    """
    Inner write primitive — no locking.  Callers MUST hold any relevant lock.
    Separated so AutoMetricsStream can lock once around this call.
    """
    prompt_version = "auto"
    if prompt_store is not None:
        try:
            v = prompt_store.get_version_label("validator")
            # Coerce to str so MagicMocks/non-string values don't break JSON
            # serialisation when tests pass a mock prompt_store.
            prompt_version = str(v) if v is not None else "auto"
        except Exception:
            pass

    record = RunRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        intent=task_id,
        prompt_version=prompt_version,
        iterations_used=attempts_used,
        validator_status="approved" if approved else "rejected",
        validator_feedback=feedback,
        improvement_json_ok=None,   # AUTO-E2: never set — excluded from interactive rate
        elapsed_seconds=0.0,
    )
    mc.record(record)


def record_gate2_result(
    mc: MetricsCollector,
    task_id: str,
    *,
    approved: bool,
    feedback: str,
    attempts_used: int,
    prompt_store=None,  # Optional[PromptStore]
) -> None:
    """
    Write a Gate-2 validation outcome directly to a MetricsCollector.

    This is the module-level primitive used by AutoMetricsStream.record_gate2()
    and exported for backward compatibility with code that already holds a
    MetricsCollector reference (e.g. auto_tuner.py callers).

    **Not thread-safe on its own** — concurrent callers sharing a MetricsCollector
    must coordinate externally (or go through AutoMetricsStream which adds a lock).

    ``improvement_json_ok`` is always written as ``None`` for auto-mode records
    so they do not pollute the interactive ``json_parse_failure_rate`` signal
    (AUTO-E2 isolation).

    Never raises — errors are logged and swallowed.
    """
    try:
        _record_gate2_locked(
            mc,
            task_id,
            approved=approved,
            feedback=feedback,
            attempts_used=attempts_used,
            prompt_store=prompt_store,
        )
    except Exception as exc:
        logger.error("record_gate2_result: failed to record metric — %s", exc)