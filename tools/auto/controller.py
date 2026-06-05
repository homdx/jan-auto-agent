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

agents.ini [auto] keys (AUTO-DM-1)
-----------------------------------
task_mode — domain mode: code (default) | docs | creative
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

from tools.auto.state import StateStore, STATUS_DONE, STATUS_BLOCKED
from tools.auto.git_manager import make_git_manager, GitError
from tools.auto.outer_loop import make_outer_loop  # noqa: F401 — re-exported as a patch target for tests

# Epic G Integrations
from tools.auto.run_trace import setup_run_trace
from tools.auto.progress_display import ProgressDisplay, make_progress_display
from tools.auto.auto_metrics import AutoMetricsStream
from tools.auto.auto_tuner import AutoTuner, make_auto_tuner

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
        dry_run: bool = False,
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
        self.dry_run     = dry_run
        self.agent_dir   = base_path / ".agent"

        # AUTO-DM-1: read task_mode once at startup; forwarded to pipeline and
        # all downstream factory calls.  Defaults to "code" so existing configs
        # and call sites are completely unaffected.
        _cfg_dm = configparser.ConfigParser()
        if Path(config_path).exists():
            _cfg_dm.read(config_path)
        self.task_mode: str = _cfg_dm.get("auto", "task_mode", fallback="code")
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
        
        # Epic G sub-systems: Initialized as None to prevent test crashes
        self.run_trace = None
        self.progress_display: Optional[ProgressDisplay] = None
        self.metrics_stream: Optional[AutoMetricsStream] = None
        self.auto_tuner: Optional[AutoTuner] = None

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
        """
        self._start_time = self._time_fn()
        self._print_banner()

        # AUTO-A2: MUST initialise state first so .agent/ exists
        is_fresh = self.state.initialise(self.goal, self.base_dir)

        cfg = configparser.ConfigParser()
        if Path(self.config_path).exists():
            cfg.read(self.config_path)

        # ── Epic G Initialization ─────────────────────────────────────────

        # AUTO-F2: Configure the run tracer singleton
        self.run_trace = setup_run_trace(self.state, cfg)
        self.run_trace.log_run_start(self.goal, self.base_dir)

        # AUTO-F1: Setup progress display
        self.progress_display = make_progress_display(self.state, cfg)
        self.progress_display.code_total = len(self.state.all_tasks())

        # AUTO-E1/E2: Setup metrics stream and auto tuner
        self.metrics_stream = AutoMetricsStream(self.agent_dir)
        self.auto_tuner = make_auto_tuner(cfg, self.agent_dir)

        # ──────────────────────────────────────────────────────────────────

        # AUTO-A3: ensure the target folder is a git repo with agent identity.
        self._setup_git()

        resume_info = self.state.resume_info()
        if not is_fresh:
            self._print_resume_summary(resume_info)

        # AUTO-A4: log active limits at run start
        self._log_limits()

        # Update progress to "running"
        self.state.update_progress(status="running")

        # ── AUTO-G0: Delegate to pipeline ─────────────────────────────────
        # pipeline.run_pipeline() handles:
        #   G1 — PLAN phase (ingest → architect → gate1 → prioritise → emit)
        #   G2+ — EXECUTE phase (outer_loop per task, commit, exhaustion)
        # controller.run() stays thin; all orchestration is unit-testable via
        # tools/auto/pipeline.py in isolation.
        from tools.auto.pipeline import run_pipeline
        stop_reason, session_tasks_done = run_pipeline(self)

        # ── Finalise ──────────────────────────────────────────────────────
        if stop_reason:
            self._handle_cap(stop_reason, session_tasks_done)
        else:
            self.state.update_progress(status="idle")
            self.state.log("run finished cleanly")
            if self.run_trace:
                self.run_trace.log_run_finished()

        return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_task_loop(
        self,
        *,
        task_mode: str = "code",
    ) -> tuple[Optional[str], int]:
        """Iterate pending tasks, check caps, execute via outer_loop, return stop reason.

        AUTO-G2: replaces the inner_loop skeleton with the real pipeline:
            outer_loop.run_task(task) → passed   → commit_on_success  (G3)
                                      → exhausted → exhaustion_handler (G4)
        A4 caps, dependency guard, progress display, run_trace, and auto_tuner
        wiring are all preserved unchanged.

        AUTO-DM-1: ``task_mode`` is forwarded to ``make_outer_loop`` so all
        downstream agents (coder, inner_loop, validator) receive the mode.

        Returns
        -------
        (stop_reason, tasks_done)
            stop_reason is ``"runtime_cap"`` / ``"task_cap"`` if a cap fired,
            or ``None`` if all pending tasks were processed.
            tasks_done is the count of tasks completed in *this session only*.
        """
        pending = self.state.resume_info()["pending"]
        tasks_done = 0

        # ── Build execution helpers once per loop ──────────────────────────
        cfg = configparser.ConfigParser()
        if Path(self.config_path).exists():
            cfg.read(self.config_path)

        from tools.auto.outer_loop import make_outer_loop
        from tools.auto.commit_on_success import CommitOnSuccess
        from tools.auto.exhaustion_handler import make_exhaustion_handler
        from tools.auto.executor import make_executor
        from tools.auto.bug_fix_loop import make_bug_fix_loop

        outer_loop = make_outer_loop(cfg, self.base_dir, self.state,
                                       task_mode=task_mode)
        commit_helper = (
            CommitOnSuccess(self.git, self.state)
            if self.git is not None else None
        )
        exhaustion_handler = make_exhaustion_handler(self.state)

        # AUTO-G5: executor + bug_fix_loop for post-commit regression checks
        executor = make_executor(
            self.base_dir,
            timeout_sec=self.limits.exec_timeout_sec,
        )
        bug_fix_loop = make_bug_fix_loop(
            cfg, self.base_dir, self.state,
            outer_loop=outer_loop,
            commit_on_success=commit_helper,
        )

        for task in pending:
            # AUTO-A4: check caps BEFORE executing each task
            reason = self.check_caps(tasks_done)
            if reason:
                logger.info(
                    "_run_task_loop: cap fired (%s) after %d task(s) — stopping",
                    reason, tasks_done,
                )
                return reason, tasks_done

            failed_deps = []
            for dep_id in task.get("dependencies", []):
                dep = self.state.get_task(dep_id)
                if not dep or dep["status"] != STATUS_DONE:
                    failed_deps.append(dep_id)

            if failed_deps:
                reason_str = f"dependency not done: {', '.join(failed_deps)}"
                logger.info("Task %s blocked by incomplete dependencies: %s",
                            task["id"], failed_deps)
                self.state.set_task_status(task["id"], STATUS_BLOCKED)
                self.state.log(f"task {task['id']} blocked ({reason_str})")
                if self.run_trace:
                    self.run_trace.log_task_blocked(task["id"], reason_str)
                continue

            if self.run_trace:
                self.run_trace.log_task_start(task["id"], task.get("title", ""))

            # AUTO-F1: Update display for task start
            if self.progress_display:
                self.progress_display.set_task(
                    task_num=tasks_done + 1,
                    attempt=task.get("attempt", 1) or 1,
                    round_num=task.get("round", 1) or 1,
                )

            # ── AUTO-G2: outer_loop execution ──────────────────────────────
            result = outer_loop.run_task(task, self.base_dir)

            if result.passed:
                # ── AUTO-G3: commit on success ─────────────────────────────
                commit_hash: Optional[str] = None
                if commit_helper is not None:
                    commit_hash = commit_helper.commit(task, result)
                else:
                    # No git: mark DONE manually
                    self.state.set_task_status(task["id"], STATUS_DONE)

                tasks_done += 1
                self.state.log(
                    f"task {task['id']} completed — "
                    f"rounds={result.rounds_used} "
                    f"commit={commit_hash[:12] if commit_hash else 'none'}"
                )
                if self.run_trace:
                    self.run_trace.log_task_done(task["id"], commit_hash)

                # ── AUTO-G5: post-commit regression check ──────────────────
                self._check_regressions(task["id"], executor, bug_fix_loop)

            else:
                # ── AUTO-G4: exhaustion → knowledge note + ticket ──────────
                ex_outcome = exhaustion_handler.handle(task, result)
                self.state.log(
                    f"task {task['id']} exhausted — "
                    f"rounds={result.rounds_used} "
                    f"ticket={ex_outcome.ticket_id}"
                )
                if self.run_trace:
                    feedback_snippet = result.knowledge()[:200] if result.feedback_files else ""
                    self.run_trace.log_task_blocked(task["id"], feedback_snippet)

            # AUTO-F1: Tick progress upon task completion / exhaustion
            if self.progress_display:
                self.progress_display.tick_code()

            # AUTO-E1/E2: Record metric and maybe tune
            if self.metrics_stream and self.auto_tuner:
                total_attempts = sum(
                    getattr(r, "attempts_used", 0) for r in result.inner_results
                )
                last_feedback = ""
                if result.inner_results:
                    last_feedback = getattr(result.inner_results[-1], "last_feedback", "")
                self.metrics_stream.record_gate2(
                    task["id"],
                    approved=result.passed,
                    feedback=last_feedback,
                    attempts=total_attempts,
                    prompt_store=self.auto_tuner.prompt_store,
                )
                tune_outcome = self.auto_tuner.maybe_tune()
                if tune_outcome.promoted:
                    self.state.log(
                        f"[AUTO-E1] auto_tuner promoted validator prompt: "
                        f"score={tune_outcome.new_prompt_score:.2f}"
                    )

        return None, tasks_done  # all tasks done / no tasks

    def _check_regressions(
        self,
        just_committed_id: str,
        executor,
        bug_fix_loop,
    ) -> None:
        """AUTO-G5: re-run acceptance checks for all previously-DONE tasks.

        Called immediately after a task is committed.  Any task whose check
        now fails is treated as a regression and routed through BugFixLoop.

        Parameters
        ----------
        just_committed_id:
            The task that was just committed — excluded from re-checking
            because its check was validated moments ago by outer_loop.
        executor:
            Ready :class:`~tools.auto.executor.Executor` instance.
        bug_fix_loop:
            Ready :class:`~tools.auto.bug_fix_loop.BugFixLoop` instance.
        """
        done_tasks = [
            t for t in self.state.all_tasks()
            if t["status"] == STATUS_DONE
            and t["id"] != just_committed_id
            and t.get("acceptance_check", "").strip()
        ]
        for done_task in done_tasks:
            exec_result = executor.run(done_task)
            if not exec_result.passed:
                logger.warning(
                    "_check_regressions: task %s regressed (rc=%s) — "
                    "running bug fix loop",
                    done_task["id"], exec_result.exit_code,
                )
                self.state.log(
                    f"regression detected in task {done_task['id']} "
                    f"after commit of {just_committed_id} "
                    f"(rc={exec_result.exit_code})"
                )
                bfl_result = bug_fix_loop.handle_regression(
                    done_task, exec_result, self.base_dir
                )
                self.state.log(
                    f"bug fix loop: {bfl_result.summary()}"
                )

    def _handle_cap(self, stop_reason: str, tasks_done: int) -> None:
        """Persist cap state, print user-facing notice, write to log."""
        elapsed = self.elapsed_seconds()

        if self.run_trace:
            self.run_trace.log_run_capped(stop_reason)
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
            # BUG 12 FIXED: Reports the accurate session tasks_done
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
        print(f"[{ts}]    goal      : {self.goal}")
        print(f"[{ts}]    base_dir  : {self.base_dir}")
        print(f"[{ts}]    config    : {self.config_path}")
        print(f"[{ts}]    task_mode : {getattr(self, 'task_mode', 'code')}")

    def _print_resume_summary(self, info: dict) -> None:
        done    = len(info["done_ids"])
        pending = len(info["pending"])
        ts = _ts()
        # BUG 13 FIXED: Removed redundant "{len(done_ids)} skipped" mislabel.
        print(f"[{ts}] ♻️  Resuming existing run — "
              f"{done} already done, {pending} pending")
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
    dry_run: bool = False,
) -> int:
    """Create an :class:`AutoController` and run it.

    Returns the integer exit code (0 on success, non-zero on error).
    """
    try:
        controller = AutoController(
            goal=goal,
            base_dir=base_dir,
            config_path=config_path,
            dry_run=dry_run,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        return controller.run()
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error in autonomous run: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1