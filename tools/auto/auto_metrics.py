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
* All methods are fail-closed: errors are logged and swallowed so a metrics
  write failure never aborts a coding task.

Public API
----------
  stream = AutoMetricsStream(agent_dir)
  stream.record_gate2(task_id, approved=True, feedback="...", attempts=2)
  stream.record_gate2(task_id, approved=False, feedback="...", attempts=5,
                      prompt_store=ps)   # optional: records prompt version
  stream.collector     → the underlying MetricsCollector (e.g. for AutoTuner)

  # Module-level convenience wrapper (keeps back-compat with auto_tuner usage):
  record_gate2_result(mc, task_id, approved=..., feedback=..., attempts_used=...,
                      prompt_store=...)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.metrics_collector import MetricsCollector, RunRecord

logger = logging.getLogger(__name__)

# Interactive default — used only to assert isolation in tests / guards.
_INTERACTIVE_DEFAULT = Path("metrics.json")


class AutoMetricsStream:
    """
    Owns the `.agent/metrics.json` write path for an autonomous run.

    Parameters
    ----------
    agent_dir:
        The `.agent/` directory for the current run.  The stream file is
        always ``agent_dir / "metrics.json"``, never the interactive default.

    Raises
    ------
    ValueError
        If ``agent_dir / "metrics.json"`` resolves to the interactive default
        path (``metrics.json`` in the CWD).  This is a programming error —
        auto metrics must never share a file with the interactive stream.
    """

    def __init__(self, agent_dir: Path) -> None:
        metrics_path = agent_dir / "metrics.json"
        # Guard: refuse to write to the interactive default path.
        try:
            resolved = metrics_path.resolve()
            interactive = _INTERACTIVE_DEFAULT.resolve()
            if resolved == interactive:
                raise ValueError(
                    f"AutoMetricsStream would collide with the interactive "
                    f"metrics file at {interactive}. "
                    f"Pass a proper .agent/ directory, not the project root."
                )
        except OSError:
            # resolve() can fail on non-existent paths on some OSes — safe to ignore.
            pass

        self._collector = MetricsCollector(metrics_path=metrics_path)

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

        ``improvement_json_ok`` is always ``None`` so these records are
        excluded from the interactive optimizer's json_parse_failure_rate
        computation even if the paths were accidentally shared.

        Never raises — errors are logged and swallowed.
        """
        record_gate2_result(
            self._collector,
            task_id,
            approved=approved,
            feedback=feedback,
            attempts_used=attempts,
            prompt_store=prompt_store,
        )

    # ------------------------------------------------------------------ #
    # Factory                                                              #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_agent_dir(cls, agent_dir: Path) -> "AutoMetricsStream":
        """Canonical factory — creates (if needed) and returns the stream."""
        agent_dir.mkdir(parents=True, exist_ok=True)
        return cls(agent_dir)


# ── Module-level helper (backward-compat; used by auto_tuner.py) ─────────────

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

    This is the low-level primitive used by AutoMetricsStream.record_gate2()
    and exported for backward compatibility with code that already holds a
    MetricsCollector reference.

    ``improvement_json_ok`` is always written as ``None`` for auto-mode records
    so they do not pollute the interactive ``json_parse_failure_rate`` signal
    (AUTO-E2 isolation).

    Never raises — errors are logged and swallowed.
    """
    try:
        prompt_version = "auto"
        if prompt_store is not None:
            try:
                prompt_version = prompt_store.get_version_label("validator")
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
    except Exception as exc:
        logger.error("record_gate2_result: failed to record metric — %s", exc)
