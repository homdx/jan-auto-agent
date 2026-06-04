"""tools/auto/run_trace.py — AUTO-F2: Trace + run log for autonomous runs.

Every architect / coder / validator / executor exchange is logged via
``agent_trace`` using a *per-run id* so all events from one autonomous run
group together and can be replayed later.  High-level lifecycle events (run
start, task start/done/blocked, cap fired, run finished) are written to
``run.log`` via the StateStore as well.

Public surface
--------------
    from tools.auto.run_trace import RunTrace, setup_run_trace

    # ── at run start ──────────────────────────────────────────────────
    rt = setup_run_trace(state, config)
    # agent_trace singleton is now configured; all tracer.event() calls
    # from architect / coder / validator / executor land in
    # .agent/trace_<run_id>.jsonl

    # ── high-level lifecycle events ───────────────────────────────────
    rt.log_run_start(goal, base_dir)
    rt.log_task_start(task_id, title)
    rt.log_task_done(task_id, commit_hash)
    rt.log_task_blocked(task_id, reason)
    rt.log_run_finished(stop_reason=None)   # None = clean finish
    rt.log_run_capped(stop_reason)

    # ── introspection ─────────────────────────────────────────────────
    rt.run_id          # str, the hex run id (also in the trace filename)
    rt.trace_path      # Path to .agent/trace_<run_id>.jsonl  (or None)

Agents must NOT call setup_run_trace themselves; the controller calls it once
and the module-level ``tracer`` singleton is shared automatically.

agents.ini [trace] keys (all optional)
---------------------------------------
enabled             — "yes"/"no"  (default "yes" in auto mode)
max_field_chars     — int (default 4000)
console_echo        — "yes"/"no"  (default "no")

The trace file is named ``trace_<run_id>.jsonl`` and lives under ``.agent/``
alongside ``run.log`` and ``progress.json`` so the entire run is
reconstructable from that directory.
"""

from __future__ import annotations

import configparser
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer

logger = logging.getLogger(__name__)

_SOURCE = "controller"   # source label for lifecycle trace events


class RunTrace:
    """Thin wrapper around the global ``tracer`` singleton.

    Holds per-run metadata and emits high-level run.log / trace events.
    Constructed once per autonomous run by :func:`setup_run_trace`.
    """

    def __init__(
        self,
        state,                       # StateStore — loosely typed to avoid circular import
        run_id: str,
        trace_path: Optional[Path],
    ) -> None:
        self._state      = state
        self.run_id      = run_id
        self.trace_path  = trace_path

    # ── High-level lifecycle events ───────────────────────────────────────

    def log_run_start(self, goal: str, base_dir: str | Path) -> None:
        """Record that the autonomous run has started (trace + run.log)."""
        tracer.event(
            source=_SOURCE,
            target="auto_run",
            kind="run_start",
            params={"goal": goal, "base_dir": str(base_dir), "run_id": self.run_id},
        )
        self._state.log(
            f"[AUTO-F2] run started  run_id={self.run_id}  goal={goal!r}"
        )

    def log_task_start(self, task_id: str, title: str) -> None:
        """Record that a task has entered the coder loop."""
        tracer.event(
            source=_SOURCE,
            target="outer_loop",
            kind="call",
            params={"task_id": task_id, "title": title},
        )
        self._state.log(f"[AUTO-F2] task start   {task_id}: {title}")

    def log_task_done(self, task_id: str, commit_hash: Optional[str] = None) -> None:
        """Record that a task passed Gate 2 and was committed."""
        params: dict = {"task_id": task_id}
        if commit_hash:
            params["commit"] = commit_hash
        tracer.event(
            source="outer_loop",
            target=_SOURCE,
            kind="result",
            params=params,
            content="DONE",
        )
        commit_info = f"  commit={commit_hash}" if commit_hash else ""
        self._state.log(f"[AUTO-F2] task done    {task_id}{commit_info}")

    def log_task_blocked(self, task_id: str, reason: str) -> None:
        """Record that a task exhausted all attempts and is BLOCKED."""
        tracer.event(
            source="outer_loop",
            target=_SOURCE,
            kind="decision",
            params={"task_id": task_id, "reason": reason},
            content="BLOCKED",
        )
        self._state.log(f"[AUTO-F2] task blocked {task_id}: {reason}")

    def log_run_finished(self, stop_reason: Optional[str] = None) -> None:
        """Record a clean (or capped) end of the run."""
        kind    = "run_capped" if stop_reason else "run_finished"
        params  = {"run_id": self.run_id}
        if stop_reason:
            params["stop_reason"] = stop_reason
        tracer.event(
            source=_SOURCE,
            target="auto_run",
            kind=kind,
            params=params,
        )
        if stop_reason:
            self._state.log(
                f"[AUTO-F2] run capped   run_id={self.run_id}  reason={stop_reason}"
            )
        else:
            self._state.log(
                f"[AUTO-F2] run finished run_id={self.run_id}"
            )

    def log_run_capped(self, stop_reason: str) -> None:
        """Convenience alias for :meth:`log_run_finished` with a stop reason."""
        self.log_run_finished(stop_reason=stop_reason)


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def setup_run_trace(
    state,
    config: configparser.ConfigParser,
) -> RunTrace:
    """Configure the global ``agent_trace`` singleton for this run and return a
    :class:`RunTrace` bound to *state*.

    Steps
    -----
    1. Generate a fresh ``run_id`` (12-char hex UUID fragment).
    2. Derive the trace file path: ``.agent/trace_<run_id>.jsonl``.
    3. Call ``tracer.configure(enabled=True, path=…)`` so every subsequent
       ``tracer.event()`` call writes into that file.
    4. Set the tracer's internal ``_run_id`` so all events are grouped.
    5. Return a :class:`RunTrace` instance ready for lifecycle logging.

    The function is idempotent within a process — calling it a second time
    reconfigures the tracer with a *new* run_id (used when the controller
    is exercised multiple times in the same test process).
    """
    # ── 1. Generate run_id ────────────────────────────────────────────────
    run_id = uuid.uuid4().hex[:12]

    # ── 2. Derive trace path ──────────────────────────────────────────────
    agent_dir  = Path(state.agent_dir)
    trace_path = agent_dir / f"trace_{run_id}.jsonl"

    # ── 3–4. Configure the tracer singleton ───────────────────────────────
    enabled         = config.getboolean("trace", "enabled",         fallback=True)
    max_field_chars = config.getint    ("trace", "max_field_chars",  fallback=4000)
    console_echo    = config.getboolean("trace", "console_echo",    fallback=False)

    tracer.configure(
        enabled         = enabled,
        path            = str(trace_path),
        max_field_chars = max_field_chars,
        console_echo    = console_echo,
    )
    # Inject the run_id so tracer.event() records it on every event
    tracer._run_id = run_id  # noqa: SLF001  (private attribute; intentional)

    state.log(
        f"[AUTO-F2] trace configured  "
        f"enabled={enabled}  "
        f"run_id={run_id}  "
        f"path={trace_path}"
    )

    return RunTrace(state=state, run_id=run_id, trace_path=trace_path if enabled else None)
