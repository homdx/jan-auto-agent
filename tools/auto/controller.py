"""tools/auto/controller.py — AUTO-A1 / AUTO-A2 / AUTO-A4: Autonomous mode controller.

Entry point for the autonomous improvement mode.  Provides the public surface
that main.py imports:

    from tools.auto.controller import AutoController, run_auto

AUTO-A1 (entry point):
  * Validates inputs (non-empty goal, existing base_dir).
  * Prints start banner echoing goal and base_dir.
  * Routes --auto / /auto to this module; interactive/one-shot paths untouched.

AUTO-A2 (state store + resume):
  * Delegates all .agent/ I/O to StateStore (tools/auto/state.py).
  * On start: loads existing state and resumes (skips DONE tasks,
    continues IN_PROGRESS tasks) — kill mid-run, restart → no repeated work.
  * plan.json schema enforced via make_task() / _validate_task_schema().
  * progress.json updated after every logical step.

AUTO-A4 (run limits & safety):
  * Reads ``[auto] max_runtime_min`` and ``[auto] max_tasks_per_run`` from
    agents.ini (both default to 0 = no cap).
  * Wall-clock and task caps are checked before every task iteration.
  * When a cap fires the run stops gracefully:
      - progress.json status → "capped"
      - progress.json stop_reason → "runtime_cap" | "task_cap"
      - run.log records which cap fired and how many tasks completed
      - exit code 0 (graceful stop, not an error)
  * Resumable: a subsequent run reloads state and continues from the last
    unfinished task, respecting caps anew.

agents.ini [auto] keys (AUTO-A4)
---------------------------------
max_runtime_min   — wall-clock cap in minutes (float; 0 = disabled)
max_tasks_per_run — maximum tasks to execute this session (int; 0 = disabled)
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from tools.auto.state import StateStore, STATUS_DONE, STATUS_IN_PROGRESS, STATUS_BLOCKED
from tools.auto.git_manager import make_git_manager, GitError

logger = logging.getLogger(__name__)

# ── Stop-reason constants (written to progress.json) ────────────────────────
STOP_RUNTIME_CAP = "runtime_cap"
STOP_TASK_CAP    = "task_cap"


# ─────────────────────────────────────────────────────────────────────────────
# RunLimits — thin value object carrying the two caps
# ─────────────────────────────────────────────────────────────────────────────

class RunLimits:
    """Carries the wall-clock and task caps for one autonomous session.

    Parameters
    ----------
    max_runtime_sec:
        Maximum wall-clock seconds for this run.  ``0`` (or negative) means
        no cap.
    max_tasks_per_run:
        Maximum number of tasks to execute in this session.  ``0`` (or
        negative) means no cap.
    exec_timeout_sec:
        Per-execution wall-clock timeout (seconds) handed to the executor
        (AUTO-C1) when it runs generated code / acceptance checks.  This is an
        *execution constraint* (how long a single ``python``/test run may take),
        distinct from the whole-session ``max_runtime_sec`` cap.  ``0`` (or
        negative) means no per-execution timeout.  Default is 120s.
    """

    def __init__(
        self,
        max_runtime_sec: float = 0,
        max_tasks_per_run: int = 0,
        exec_timeout_sec: float = 120,
    ) -> None:
        self.max_runtime_sec   = max(0.0, float(max_runtime_sec))
        self.max_tasks_per_run = max(0, int(max_tasks_per_run))
        self.exec_timeout_sec  = max(0.0, float(exec_timeout_sec))

    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> "RunLimits":
        """Read limits from a ``ConfigParser`` instance ([auto] section)."""
        max_min   = config.getfloat("auto", "max_runtime_min",   fallback=0)
        max_tasks = config.getint  ("auto", "max_tasks_per_run", fallback=0)
        exec_to   = config.getfloat("auto", "exec_timeout_sec",  fallback=120)
        return cls(
            max_runtime_sec   = max_min * 60,
            max_tasks_per_run = max_tasks,
            exec_timeout_sec  = exec_to,
        )

    @property
    def runtime_capped(self) -> bool:
        """``True`` if a wall-clock cap is active (non-zero)."""
        return self.max_runtime_sec > 0

    @property
    def task_capped(self) -> bool:
        """``True`` if a task cap is active (non-zero)."""
        return self.max_tasks_per_run > 0

    @property
    def exec_timeout_active(self) -> bool:
        """``True`` if a per-execution timeout is active (non-zero)."""
        return self.exec_timeout_sec > 0

    def __repr__(self) -> str:
        return (
            f"RunLimits(max_runtime_sec={self.max_runtime_sec}, "
            f"max_tasks_per_run={self.max_tasks_per_run}, "
            f"exec_timeout_sec={self.exec_timeout_sec})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AutoController
# ─────────────────────────────────────────────────────────────────────────────

class AutoController:
    """Orchestrates an autonomous improvement run.

    Parameters
    ----------
    goal:
        A non-empty string describing what the agent should achieve,
        e.g. ``"improve current code"``.
    base_dir:
        Absolute (or relative) path to the project root that will be
        reviewed and modified.  Must exist.
    config_path:
        Path to ``agents.ini`` (default ``"agents.ini"``).
    _time_fn:
        Callable returning the current monotonic time in seconds.
        Defaults to ``time.monotonic``.  Intended for testing only — allows
        tests to fake elapsed time without sleeping.

    Raises
    ------
    ValueError
        If *goal* is empty.
    FileNotFoundError
        If *base_dir* does not exist.
    """

    def __init__(
        self,
        goal: str,
        base_dir: str | os.PathLike = ".",
        config_path: str = "agents.ini",
        _time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        goal = goal.strip() if goal else ""
        if not goal:
            raise ValueError("AutoController requires a non-empty goal string.")

        base_path = Path(base_dir).resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_dir does not exist: {base_path}")

        self.goal        = goal
        self.base_dir    = base_path
        self.config_path = config_path
        self.agent_dir   = base_path / ".agent"
        # AUTO-A4: execution working dir (executor/AUTO-C1 runs code here)
        self.workspace_dir = self.agent_dir / "workspace"

        # AUTO-A4: monotonic clock — injectable for unit tests
        self._time_fn: Callable[[], float] = _time_fn or time.monotonic

        # AUTO-A4: load run limits from agents.ini
        self.limits = self._load_limits(config_path)

        # AUTO-A2: StateStore owns all .agent/ I/O
        self.state = StateStore(self.agent_dir)

        # AUTO-A3: git manager wired at run start (None until run())
        self.git = None

        # Set at the start of run(); used by is_runtime_exceeded()
        self._start_time: float = 0.0

    # ── Run limits API (AUTO-A4) ─────────────────────────────────────────────

    def is_runtime_exceeded(self) -> bool:
        """Return ``True`` if the wall-clock cap has been reached.

        Always returns ``False`` when no cap is configured
        (``limits.max_runtime_sec == 0``).
        """
        if not self.limits.runtime_capped:
            return False
        elapsed = self._time_fn() - self._start_time
        return elapsed >= self.limits.max_runtime_sec

    def is_task_cap_reached(self, tasks_done: int) -> bool:
        """Return ``True`` if *tasks_done* has reached the per-run task cap.

        Always returns ``False`` when no cap is configured
        (``limits.max_tasks_per_run == 0``).
        """
        if not self.limits.task_capped:
            return False
        return tasks_done >= self.limits.max_tasks_per_run

    def check_caps(self, tasks_done: int) -> Optional[str]:
        """Return the stop-reason string if any cap is exceeded, else ``None``.

        Evaluates runtime cap first (lowest-cost check).

        Parameters
        ----------
        tasks_done:
            Number of tasks fully executed so far in this session.

        Returns
        -------
        str or None
            ``"runtime_cap"``, ``"task_cap"``, or ``None``.
        """
        if self.is_runtime_exceeded():
            return STOP_RUNTIME_CAP
        if self.is_task_cap_reached(tasks_done):
            return STOP_TASK_CAP
        return None

    def elapsed_seconds(self) -> float:
        """Return seconds elapsed since :meth:`run` was called."""
        return self._time_fn() - self._start_time

    # ------------------------------------------------------------------
    # Public run API
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Execute the autonomous run and return an exit code (0 = success).

        Lifecycle
        ---------
        1. Print start banner.
        2. Initialise (fresh) or resume (existing) state.
        3. Enter task loop — check caps before every task.
        4. On cap: persist stop_reason, log, exit 0 (graceful stop).
        5. On normal completion: set status "idle", exit 0.
            # ── Dependency Guard (Bug #5) ──────────────────────────────
            failed_deps = []
            for dep_id in task.get("dependencies", []):
                dep = self.state.get_task(dep_id)
                # If dependency doesn't exist or isn't DONE (e.g. BLOCKED), we cannot proceed.
                if not dep or dep["status"] != STATUS_DONE:
                    failed_deps.append(dep_id)

            if failed_deps:
                logger.info("Task %s blocked by incomplete dependencies: %s", task["id"], failed_deps)
                self.state.set_task_status(task["id"], STATUS_BLOCKED)
                self.state.log(f"task {task['id']} blocked (dependency not done: {', '.join(failed_deps)})")
                continue
        """
        self._start_time = self._time_fn()
        self._print_banner()

        # AUTO-A2: initialise (fresh) or resume (existing state)
        is_fresh = self.state.initialise(self.goal, self.base_dir)

        # AUTO-A3: ensure the target folder is a git repo with agent identity.
        self._setup_git()

        resume_info = self.state.resume_info()
        if not is_fresh:
            self._print_resume_summary(resume_info)

        # AUTO-A4: log active limits at run start
        self._log_limits()

        # Update progress to "running"
        self.state.update_progress(status="running")

        # ── Task execution loop ────────────────────────────────────────────
        # Iterates pending tasks; checks both caps before each execution.
        # Real task execution (AUTO-B Architect, AUTO-C Coder) hooks in here.
        stop_reason, session_tasks_done = self._run_task_loop()

        # ── Finalise ──────────────────────────────────────────────────────
        if stop_reason:
            self._handle_cap(stop_reason, session_tasks_done)
        else:
            self.state.update_progress(status="idle")
            self.state.log("run finished cleanly")

        return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_task_loop(self) -> tuple[Optional[str], int]:
        """Iterate pending tasks, check caps, execute (stub), return stop reason.

        Returns
        -------
        (stop_reason, tasks_done)
            stop_reason is ``"runtime_cap"`` / ``"task_cap"`` if a cap fired,
            or ``None`` if all pending tasks were processed.
            tasks_done is the count of tasks completed in *this session only*.
        """
        pending = self.state.resume_info()["pending"]
        tasks_done = 0

        for task in pending:
            # AUTO-A4: check caps BEFORE executing each task
            reason = self.check_caps(tasks_done)
            if reason:
                logger.info(
                    "_run_task_loop: cap fired (%s) after %d task(s) — stopping",
                    reason, tasks_done,
                )
                return reason, tasks_done

            # ── Future: real task execution (AUTO-B/C) hooks here ──────
            # e.g. self._execute_task(task)
            # For now: mark in_progress then done as a skeleton pass-through.
            self.state.set_task_status(task["id"], STATUS_IN_PROGRESS)
            # (Real execution would happen here)
            self.state.set_task_status(task["id"], STATUS_DONE)
            tasks_done += 1
            self.state.log(f"task {task['id']} completed (skeleton)")

        return None, tasks_done  # all tasks done / no tasks

    def _handle_cap(self, stop_reason: str, tasks_done: int) -> None:
        """Persist cap state, print user-facing notice, write to log."""
        elapsed = self.elapsed_seconds()

        self.state.update_progress(status="capped", stop_reason=stop_reason)
        self.state.log(
            f"run capped: reason={stop_reason} "
            f"elapsed={elapsed:.1f}s tasks_done={tasks_done}"
        )

        ts = _ts()
        if stop_reason == STOP_RUNTIME_CAP:
            mins = self.limits.max_runtime_sec / 60
            print(
                f"[{ts}] ⏱  Runtime cap reached "
                f"({mins:.2g} min / {elapsed:.1f}s elapsed) — "
                f"state saved, run is resumable."
            )
        else:  # STOP_TASK_CAP
            print(
                f"[{ts}] 🔢 Task cap reached "
                f"({self.limits.max_tasks_per_run} tasks/session, "
                f"{tasks_done} completed) — "
                f"state saved, run is resumable."
            )

    def _setup_git(self) -> None:
        """AUTO-A3: ensure the base dir is a git repo and apply agent identity.

        Guarded: a git failure (e.g. git not installed) is logged but does NOT
        abort the run — Epic A stays usable without git, and commits (AUTO-C5)
        simply won't happen until git is available.
        """
        try:
            cfg = configparser.ConfigParser()
            if Path(self.config_path).exists():
                cfg.read(self.config_path)
            self.git = make_git_manager(self.base_dir, cfg)
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            self.state.log(
                f"git ready (user={self.git.git_user} <{self.git.git_email}>)"
            )
        except (GitError, OSError) as exc:
            self.git = None
            logger.warning("git setup failed (continuing without git): %s", exc)
            self.state.log(f"git setup failed: {exc}")

    def _print_banner(self) -> None:
        ts = _ts()
        print(f"[{ts}] 🤖 Autonomous mode starting")
        print(f"[{ts}]    goal     : {self.goal}")
        print(f"[{ts}]    base_dir : {self.base_dir}")
        print(f"[{ts}]    config   : {self.config_path}")

    def _print_resume_summary(self, info: dict) -> None:
        done    = len(info["done_ids"])
        pending = len(info["pending"])
        ts = _ts()
        print(f"[{ts}] ♻️  Resuming existing run — "
              f"{done} already done (skipping), {pending} pending")
        if info["done_ids"]:
            print(f"[{ts}]    skipping: {', '.join(sorted(info['done_ids']))}")

    def _log_limits(self) -> None:
        parts = []
        if self.limits.runtime_capped:
            parts.append(f"max_runtime={self.limits.max_runtime_sec:.1f}s")
        if self.limits.task_capped:
            parts.append(f"max_tasks={self.limits.max_tasks_per_run}")
        if parts:
            self.state.log(f"run limits active: {', '.join(parts)}")
        else:
            self.state.log("run limits: none configured")

    @staticmethod
    def _load_limits(config_path: str) -> RunLimits:
        cfg = configparser.ConfigParser()
        if Path(config_path).exists():
            cfg.read(config_path)
        return RunLimits.from_config(cfg)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper used by main.py
# ─────────────────────────────────────────────────────────────────────────────

def run_auto(
    goal: str,
    base_dir: str | os.PathLike = ".",
    config_path: str = "agents.ini",
) -> int:
    """Create an :class:`AutoController` and run it.

    Returns the integer exit code (0 on success, non-zero on error).
    """
    try:
        controller = AutoController(goal=goal, base_dir=base_dir, config_path=config_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        return controller.run()
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error in autonomous run: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1