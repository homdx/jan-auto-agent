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
* Thread-safe: ``MetricsCollector.record()`` serialises itself internally
  (FL-1, round 84) — AutoMetricsStream holds no lock of its own around it.
* Atomic writes: ``record_gate2()`` writes via a temp file + atomic rename so a
  crash mid-write never leaves a corrupt ``metrics.json``.
* All methods are fail-closed: errors are logged and swallowed so a metrics
  write failure never aborts a coding task.

Public API
----------
  stream = AutoMetricsStream(agent_dir)           # creates dir if missing
  stream.record_gate2(task_id, approved=True, feedback="...", attempts_used=2)
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
from datetime import datetime, timezone
from pathlib import Path

from tools.metrics_collector import MetricsCollector, RunRecord

logger = logging.getLogger(__name__)

# Interactive default — used only to assert isolation in tests / guards.
_INTERACTIVE_DEFAULT = Path("metrics.json")


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
        # Create the directory; permission, read-only and name-collision errors name the directory (guarded).
        try:
            Path(agent_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"AutoMetricsStream: could not create agent directory "
                f"{agent_dir!s} ({exc}). Check permissions and that the "
                f"path does not already exist as a file."
            ) from exc

        metrics_path = Path(agent_dir) / "metrics.json"

        # Guard: refuse to write to the interactive default path.
        try:
            if metrics_path.resolve() == _INTERACTIVE_DEFAULT.resolve():
                raise ValueError(
                    f"AutoMetricsStream would collide with the interactive "
                    f"metrics file at {_INTERACTIVE_DEFAULT.resolve()}. "
                    f"Pass a proper .agent/ directory, not the project root."
                )
        except OSError as exc:
            # A path that cannot be resolved is skipped; a debug line keeps this from failing silently (BUGFIX).
            logger.debug(
                "AutoMetricsStream: could not resolve %s for the "
                "interactive-path collision check: %s", metrics_path, exc,
            )

        # No lock here: MetricsCollector.record() serialises its own writes.
        self._collector = MetricsCollector(metrics_path=metrics_path)
        self._warn_if_contaminated()

    # Properties

    @property
    def collector(self) -> MetricsCollector:
        """The underlying MetricsCollector, e.g. to pass to AutoTuner."""
        return self._collector

    @property
    def metrics_path(self) -> Path:
        """Absolute path of the auto-run metrics file."""
        return self._collector.metrics_path

    # Write API

    def record_gate2(
        self,
        task_id: str,
        *,
        approved: bool,
        feedback: str,
        attempts_used: int | None = None,
        attempts: int = 0,   # backward-compatible alias — prefer attempts_used
        prompt_store=None,  # Optional[PromptStore]
        unavailable: bool = False,   # RUN-7/RUN-8: a provider outage, not a verdict
    ) -> None:
        """
        Record a Gate-2 validation outcome to the auto metrics stream.

        Thread-safe: delegates to ``MetricsCollector.record()``, which
        serialises the read-modify-write cycle internally (FL-1, round 84).

        ``improvement_json_ok`` is always ``None`` so these records are
        excluded from the interactive optimizer's json_parse_failure_rate
        computation even if the paths were accidentally shared.

        RUN-7: ``unavailable=True`` records ``validator_status="unavailable"``
        instead of "approved"/"rejected" — the Gate-2 validator never reached
        a verdict (a transport/parse error on every retry), so this round is
        not evidence about the validator prompt's quality. RUN-8 reuses the
        same value for the coder half of that outage: the coder call died
        before the model could answer, so no verdict was reached there either
        (``InnerLoopResult.unavailable`` with ``unavailable_stage="coder"``).
        ``approved`` is
        ignored when ``unavailable`` is set (the caller always has it False
        for an unavailable round anyway — see ``InnerLoopResult.unavailable``).

        Never raises — errors are logged and swallowed.

        .. note::
            Use ``attempts_used=N``.  The old ``attempts=N`` keyword is kept
            for backward compatibility but will be removed in a future release.
        """
        # attempts_used=0 is a real value; only a missing attempts_used falls back to the legacy attempts argument.
        effective_attempts = attempts_used if attempts_used is not None else attempts
        try:
            # No lock here: MetricsCollector.record() serialises itself (FL-1).
            _record_gate2_locked(
                self._collector,
                task_id,
                approved=approved,
                feedback=feedback,
                attempts_used=effective_attempts,
                prompt_store=prompt_store,
                unavailable=unavailable,
            )
        except Exception as exc:
            logger.error("AutoMetricsStream.record_gate2: failed to record metric — %s", exc)

    def flush(self) -> None:
        """
        Explicit sync / clean-shutdown hook.

        MetricsCollector's fsync is bounded-cadence, not per-call (FL-1,
        round 84), so this is no longer always a no-op: it forces the
        current on-disk metrics file durable right now, regardless of when
        the last cadence-triggered fsync happened.

        Never raises.
        """
        self._collector.flush()

    # Factory

    @classmethod
    def from_agent_dir(cls, agent_dir: Path) -> "AutoMetricsStream":
        """Canonical factory — directory creation is now also in __init__."""
        return cls(agent_dir)

    # Internal helpers

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
                # Unexpected format (an old or unrelated file): logged and left intact.
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
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # A corrupt or unreadable file reads as empty; decode errors are handled too (BUGFIX).
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
    unavailable: bool = False,   # RUN-7: a provider outage, not a verdict
) -> None:
    """
    Inner write primitive — no locking.  Callers MUST hold any relevant lock.
    Separated so AutoMetricsStream can lock once around this call.
    """
    prompt_version = "auto"
    if prompt_store is not None:
        try:
            v = prompt_store.get_version_label("validator")
            # Values are coerced to str, so mock prompt versions do not break the JSON.
            prompt_version = str(v) if v is not None else "auto"
        except Exception as exc:
            # A failed version lookup is logged, so an operator can tell it from a real auto run (BUGFIX).
            logger.warning(
                "auto_metrics: get_version_label('validator') failed — "
                "recording prompt_version='auto': %s", exc,
            )

    # unavailable beats approved and rejected: a validator with no verdict is neither (RUN-7).
    if unavailable:
        _status = "unavailable"
    else:
        _status = "approved" if approved else "rejected"

    record = RunRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        intent=task_id,
        prompt_version=prompt_version,
        iterations_used=attempts_used,
        validator_status=_status,
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
    unavailable: bool = False,   # RUN-7: a provider outage, not a verdict
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
            unavailable=unavailable,
        )
    except Exception as exc:
        logger.error("record_gate2_result: failed to record metric — %s", exc)
