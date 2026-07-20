"""tools/auto/controller.py — AUTO-A1 / AUTO-A2 / AUTO-A4: Autonomous mode controller.

Entry point for the autonomous improvement mode.  Provides the public surface
that main.py imports:

    from tools.auto.controller import AutoController, run_auto
  * Resumable: a subsequent run reloads state and continues from the last
    unfinished task, respecting caps anew.

agents.ini [auto] keys (AUTO-A4)
---------------------------------
max_runtime_min   — wall-clock cap in minutes (float; 0 = disabled)
max_tasks_per_run — maximum tasks to execute this session (int; 0 = disabled)

-----------------------------------
task_mode — domain mode: code (default) | docs | creative
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
import time
from tools.auto.utils import _ts
from pathlib import Path
from typing import Callable, Optional

from tools.auto.state import StateStore, STATUS_DONE, STATUS_BLOCKED, STATUS_TODO
from tools.auto.git_manager import make_git_manager, GitError
from tools.auto.utils import highest_completed_round
# NOTE: make_outer_loop is deliberately NOT imported at module level here.
# _run_task_loop() imports it locally (see below) precisely so that tests
# can mock it via patch("tools.auto.outer_loop.make_outer_loop", ...) — the
# established convention used throughout tests/test_auto_g*.py. A module-
# level import used to sit here as an alternate "patch target", but it was
# never actually live (the local import always shadowed it); see CR notes.

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
    workspace_retain_count:
        Max number of per-task ``.agent/workspace/<task_id>/`` mirrors (each
        a full repo copy, see executor.py AUTO-FIX-1) kept on disk at once.
        Without this bound, a long auto-mode run (AUTO-T1, AUTO-T2, ... plus
        AUTO-G5 regression re-checks) fills the disk with one full repo copy
        per task.  ``0`` disables pruning.  Default is 5.
    """

    def __init__(
        self,
        max_runtime_sec: float = 0,
        max_tasks_per_run: int = 0,
        exec_timeout_sec: float = 120,
        workspace_retain_count: int = 5,
    ) -> None:
        self.max_runtime_sec   = max(0.0, float(max_runtime_sec))
        self.max_tasks_per_run = max(0, int(max_tasks_per_run))
        self.exec_timeout_sec  = max(0.0, float(exec_timeout_sec))
        self.workspace_retain_count = max(0, int(workspace_retain_count))

    @classmethod
    def from_config(cls, config: configparser.ConfigParser) -> "RunLimits":
        """Read limits from a ``ConfigParser`` instance ([auto] section)."""
        max_min   = config.getfloat("auto", "max_runtime_min",   fallback=0)
        max_tasks = config.getint  ("auto", "max_tasks_per_run", fallback=0)
        exec_to   = config.getfloat("auto", "exec_timeout_sec",  fallback=120)
        ws_retain = config.getint  ("auto", "workspace_retain_count", fallback=5)
        return cls(
            max_runtime_sec   = max_min * 60,
            max_tasks_per_run = max_tasks,
            exec_timeout_sec  = exec_to,
            workspace_retain_count = ws_retain,
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
# AUTO-CR-19-4 — startup config lint (guards AUTO-CR-19-1 from regressing)
# ─────────────────────────────────────────────────────────────────────────────

# (section, legacy bare key) pairs that are code-specific by convention; each
# has a mode-specific override form f"{key}_{task_mode}" (e.g. "system_creative").
# Linted here so a future edit can't reintroduce a generic override without a
# matching mode variant, per AUTO-CR-19-1's "mode key > legacy key > builtin" rule.
_CR19_LEGACY_SYSTEM_KEYS = (
    ("validator_agent", "system"),
    ("coder", "system"),
    ("architect", "system"),
)

# Below this base num_ctx, the 4k-era prompt budgets (see agents_4k.ini) are
# known to be too tight for creative mode's larger architect/coder prompts —
# unless an explicit num_ctx_creative override compensates.
_CR19_SMALL_NUM_CTX_THRESHOLD = 8192


def _lint_mode_config(config: "configparser.ConfigParser | None", task_mode: str) -> list[str]:
    """AUTO-CR-19-4: warn at startup about config traps for non-code modes.

    Specifically guards against the exact regression AUTO-CR-19-1 fixed: a
    code-specific legacy ``system`` override (``[validator_agent]``,
    ``[coder]``, ``[architect]``) silently winning over the builtin prompt in
    a non-code ``task_mode`` because no matching ``system_{task_mode}``
    override was added alongside it. Also flags an ``agents_4k.ini``-style
    small ``num_ctx`` left in place for a ``creative`` run with no
    ``[coder] num_ctx_creative`` override to compensate.

    Pure logging — returns the list of warning strings it logged (for
    testability) and never raises or changes behaviour. No-op for
    ``task_mode == "code"`` since the legacy keys are exactly the ones code
    mode is supposed to use.
    """
    warnings: list[str] = []
    if config is None or task_mode == "code":
        return warnings

    mode_key = f"system_{task_mode}"
    for section, key in _CR19_LEGACY_SYSTEM_KEYS:
        legacy_val = config.get(section, key, fallback="").strip()
        if not legacy_val:
            continue
        override_val = config.get(section, mode_key, fallback="").strip()
        if override_val:
            continue
        warnings.append(
            f"[{section}] {key} is code-specific but task_mode={task_mode}; "
            f"set {mode_key} or rely on the builtin — the bare key is ignored "
            f"in {task_mode} mode (AUTO-CR-19-1)."
        )

    if task_mode == "creative":
        active_profile = config.get("api", "active", fallback="local")
        try:
            base_num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
        except ValueError:
            base_num_ctx = 0
        num_ctx_creative = config.get("coder", "num_ctx_creative", fallback="").strip()
        if not num_ctx_creative and 0 < base_num_ctx < _CR19_SMALL_NUM_CTX_THRESHOLD:
            warnings.append(
                f"api_{active_profile} num_ctx={base_num_ctx} looks like an "
                "agents_4k.ini-style small context window for task_mode=creative, "
                "and no [coder] num_ctx_creative override is set; creative prompts "
                "(architect + coder + prefetched context) tend to need more room — "
                "consider agents_32k.ini or setting num_ctx_creative explicitly."
            )

    for w in warnings:
        logger.warning("controller: %s", w)
    return warnings


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

        # AUTO-DM-1: task_mode is read once here and forwarded everywhere
        # (defaults to "code", so existing configs are unaffected). self.config
        # is likewise parsed exactly once and reused everywhere, instead of
        # re-reading agents.ini from disk on each call.
        self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        if Path(config_path).exists():
            self.config.read(config_path, encoding="utf-8")
        # AUTO-CR-10: normalise task_mode (typo-tolerant) so a misspelling like
        # 'creativy' is corrected with a loud warning instead of silently
        # degrading to code mode.
        from tools.auto.utils import normalize_task_mode
        _raw_mode = self.config.get("auto", "task_mode", fallback="code")
        self.task_mode, _mode_warn = normalize_task_mode(_raw_mode)
        if _mode_warn:
            logger.warning("controller: %s", _mode_warn)
        # AUTO-CR-19-4: lint for the exact config trap AUTO-CR-19-1 fixed —
        # a code-specific legacy "system" override left in place for a
        # non-code task_mode with no matching system_{mode} override.
        # Pure logging; never raises, never changes self.task_mode.
        _lint_mode_config(self.config, self.task_mode)
        # AUTO-A4: execution working dir (executor/AUTO-C1 runs code here)
        self.workspace_dir = self.agent_dir / "workspace"

        # AUTO-A4: monotonic clock — injectable for unit tests
        self._time_fn: Callable[[], float] = _time_fn or time.monotonic

        # AUTO-A4: load run limits from agents.ini (reuse the already-parsed config)
        self.limits = RunLimits.from_config(self.config)

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

        cfg = self._cfg()

        # ── Epic G Initialization ─────────────────────────────────────────

        # AUTO-F2: Configure the run tracer singleton
        self.run_trace = setup_run_trace(self.state, cfg)
        self.run_trace.log_run_start(self.goal, self.base_dir)

        # AUTO-F1: Setup progress display
        self.progress_display = make_progress_display(self.state, cfg, task_mode=self.task_mode)
        self.progress_display.code_total = len(self.state.all_tasks())

        # AUTO-E1/E2: Setup metrics stream and auto tuner
        self.metrics_stream = AutoMetricsStream(self.agent_dir)
        self.auto_tuner = make_auto_tuner(cfg, self.agent_dir)

        # ──────────────────────────────────────────────────────────────────

        # AUTO-A3: ensure the target folder is a git repo with agent identity.
        self._setup_git()

        # Reset any tasks that were blocked in a prior session back to TODO so
        # their dependencies are re-evaluated this session (see docstring on
        # _reset_resettable_blocked_tasks for why this isn't simply "reset
        # every BLOCKED task").
        self._reset_resettable_blocked_tasks(cfg)

        resume_info = self.state.resume_info()
        if not is_fresh:
            self._print_resume_summary(resume_info)

        # AUTO-A4: log active limits at run start
        self._log_limits()

        # Update progress to "running"
        self.state.update_progress(status="running")

        # AUTO-G0: delegates to pipeline.run_pipeline(), which handles PLAN
        # (ingest → architect → gate1 → prioritise → emit) and EXECUTE
        # (outer_loop per task, commit, exhaustion). controller.run() stays
        # thin — all orchestration is unit-testable via pipeline.py in isolation.
        from tools.auto.pipeline import run_pipeline
        stop_reason, session_tasks_done = run_pipeline(self)

        # ── Finalise ──────────────────────────────────────────────────────
        self._log_auto_prompts()
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
        cfg: Optional[configparser.ConfigParser] = None,
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

        # AUTO-FIX (podrugi sim): a repeat --auto invocation against a
        # directory whose plan.json is already fully done used to exit with
        # a bare "run_finished" and no explanation — from the outside it
        # looked like the run silently did nothing. Say why, and say what to
        # do about it, before the (correct) early exit below.
        # NOTE: resume_info()["pending"] is a LIST of tasks, not a count —
        # the first version of this fix compared it to 0 and never fired.
        if not pending:
            _total = len(self.state.all_tasks())
            if _total > 0:
                print(f"[controller] plan.json in {self.base_dir} has no "
                      f"pending tasks — all {_total} task(s) in the existing "
                      f"plan are already done. Nothing to execute this run. "
                      f"To plan NEW work toward the goal, remove the .agent "
                      f"state directory (or start with a fresh --base) and "
                      f"re-run.")
                logger.info("execute_phase: resumed plan has 0 pending of %d "
                            "task(s) — exiting without work; stale plan.json "
                            "is the likely cause on a repeat --auto "
                            "invocation.", _total)
            # _total == 0 (architect produced nothing) is already reported by
            # the plan-phase logs — no extra message needed here.

        # ── Build execution helpers once per loop ──────────────────────────
        if cfg is None:
            cfg = self._cfg()

        # NOTE: make_outer_loop must be imported here (locally, per-call), not
        # hoisted to module scope. tests/test_auto_g*.py mock it via
        # patch("tools.auto.outer_loop.make_outer_loop", ...); a fresh local
        # import is what makes that patch visible here. Hoisting this would
        # silently break ~15 test files that rely on the local re-resolve.
        from tools.auto.outer_loop import make_outer_loop
        from tools.auto.commit_on_success import CommitOnSuccess
        from tools.auto.exhaustion_handler import make_exhaustion_handler
        from tools.auto.executor import make_executor
        from tools.auto.bug_fix_loop import make_bug_fix_loop

        outer_loop = make_outer_loop(cfg, self.base_dir, self.state,
                                       task_mode=task_mode, run_goal=self.goal)
        # AUTO-CR-14: the bare CommitOnSuccess left summary_memory=None, so the
        # creative synopsis hook (CR-5) never fired — starving continuity (each
        # chapter only saw the previous one) and disabling the canon gate. Wire
        # SummaryMemory here, reusing self.git, so it actually runs.
        _summary_memory = None
        if task_mode == "creative" and self.git is not None:
            try:
                from tools.auto.summary_memory import make_summary_memory
                _summary_memory = make_summary_memory(
                    cfg, base_dir=self.base_dir, task_mode=task_mode,
                )
            except Exception as exc:  # noqa: BLE001 — never block commits on setup
                logger.warning(
                    "controller: could not build SummaryMemory — synopsis "
                    "updates will be skipped: %s", exc,
                )
        # AUTO-CR-23-1: build the StoryBible here too, the same way as
        # SummaryMemory, so durable facts are actually maintained in
        # production — make_story_bible was previously never called anywhere.
        # Without it, the bible's always-on injection and the continuity
        # gate's anchor never fire.
        _story_bible = None
        if task_mode == "creative":
            try:
                from tools.auto.story_bible import make_story_bible
                _active  = cfg.get("api", "active", fallback="local")
                _api_sec = f"api_{_active}"
                _story_bible = make_story_bible(
                    cfg,
                    base_url=cfg.get(_api_sec, "base_url", fallback="http://localhost:11434"),
                    api_key=cfg.get(_api_sec, "api_key", fallback="ollama"),
                    model=cfg.get(_api_sec, "model", fallback="llama3.1:8b"),
                    api_format=cfg.get(_api_sec, "api_format", fallback="ollama"),
                    base_dir=self.base_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never block commits on setup
                logger.warning(
                    "controller: could not build StoryBible — durable-fact "
                    "updates will be skipped: %s", exc,
                )

        commit_helper = (
            CommitOnSuccess(
                self.git, self.state,
                summary_memory=_summary_memory,
                task_mode=task_mode,
                base_dir=self.base_dir,
                story_bible=_story_bible,
            )
            if self.git is not None else None
        )
        exhaustion_handler = make_exhaustion_handler(self.state)

        # AUTO-G5: executor + bug_fix_loop for post-commit regression checks
        executor = make_executor(
            self.base_dir,
            timeout_sec=self.limits.exec_timeout_sec,
            max_retained_workspaces=self.limits.workspace_retain_count,
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
                    task_id=task.get("id", ""),
                    title=task.get("title", ""),
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
                # Bug 2: the coder writes its candidate into base_dir before
                # validation, so an exhausted task leaves that edit dirty — and
                # commit() stages everything (git add -u/.), which would sweep
                # it into the next successful task's commit. Discard the
                # uncommitted residue now (no-op when git is disabled).
                if self.git is not None:
                    self.git.discard_working_changes()
                    self.state.log(
                        f"task {task['id']} uncommitted edits discarded "
                        f"(exhausted, not committed)"
                    )
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
                self.progress_display.record_result(passed=result.passed)
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
                    # Full record (score/agent/reason) also lands in
                    # agent_trace.jsonl via auto_tuner's
                    # tracer.event(kind="prompt_promoted") call, so
                    # analyze_logs.py can report promoted rewrites alongside
                    # denied ones.
                    self.state.log(
                        f"[AUTO-E1] auto_tuner promoted validator prompt: "
                        f"score={tune_outcome.new_prompt_score:.2f}"
                    )
                elif getattr(tune_outcome, "triggered", False):
                    # Candidate was evaluated but denied. Print only — run.log
                    # must stay silent for non-promoted outcomes (AC4); the
                    # full record already lands in agent_trace.jsonl via
                    # auto_tuner's tracer.event(kind="prompt_denied") call.
                    print(
                        f"[AUTO-E1] auto_tuner denied candidate prompt: "
                        f"score={tune_outcome.new_prompt_score:.4f} — "
                        f"{getattr(tune_outcome, 'reason', '')}"
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
            # Bug 6: respect the run's time budget here too.  _run_task_loop
            # only checks caps between tasks; a regression that breaks several
            # earlier tasks could otherwise drive a long BugFixLoop cascade
            # (rounds × attempts of LLM calls each) with no cap check until
            # control returns to the main loop.
            if self.is_runtime_exceeded():
                logger.info(
                    "_check_regressions: runtime cap reached — stopping "
                    "regression checks after commit of %s", just_committed_id,
                )
                self.state.log(
                    f"regression checks halted by runtime cap "
                    f"(after commit of {just_committed_id})"
                )
                break
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
            print(
                f"[{ts}] 🔢 Task cap reached "
                f"({self.limits.max_tasks_per_run} tasks/session, "
                f"{tasks_done} completed) — "
                f"state saved, run is resumable."
            )

    def _cfg(self) -> configparser.ConfigParser:
        """Return the parsed agents.ini, loading it once and caching on self.

        Real runs set ``self.config`` in ``__init__`` so this returns it with no
        disk read.  Test harnesses that build the controller via ``__new__``
        (bypassing ``__init__``) hit the lazy path, which reads ``config_path``
        once and caches it — so the single-parse guarantee holds either way.
        """
        cfg = getattr(self, "config", None)
        if cfg is None:
            cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
            if Path(self.config_path).exists():
                cfg.read(self.config_path, encoding="utf-8")
            self.config = cfg
        return cfg

    def _reset_resettable_blocked_tasks(self, cfg: configparser.ConfigParser) -> None:
        """Reset BLOCKED tasks back to TODO — but only when that can help.

        A task can be BLOCKED for two very different reasons:

        1. An unmet dependency (set in ``_run_task_loop``). Resetting this to
           TODO is exactly right: the dependency check runs again immediately,
           so the task either runs now (dependency done) or is re-blocked
           straight away (dependency still pending) — harmless either way.
        2. OuterLoop used up every one of its ``max_rounds_per_task``
           attempts (round-exhaustion; see ``OuterLoop.run_task``'s
           "already exhausted" and "all rounds exhausted" paths).

        Bugfix: this used to reset BOTH cases unconditionally, on the theory
        (per the original comment, which only ever described case 1) that a
        fresh session should give every BLOCKED task another look. For case 2
        that reset was a no-op in disguise: OuterLoop decides where to resume
        from ``feedback_round_*.md`` files on disk, which a bare status reset
        never touches, so the task immediately re-exhausted and flipped
        straight back to BLOCKED without a single new attempt — silently
        defeating the exact restart an operator would reach for to give a
        stuck task another chance.

        This detects case 2 directly — via the same file-based round count
        OuterLoop itself uses (``highest_completed_round``) compared against
        the configured cap — and leaves those tasks BLOCKED so the status
        honestly reflects reality. Only genuinely-resettable tasks (case 1,
        or anything that hasn't used up its rounds) are reset.
        """
        max_rounds_cfg = cfg.getint("auto", "max_rounds_per_task", fallback=10)
        for task in self.state.all_tasks():
            if task["status"] != STATUS_BLOCKED:
                continue
            if highest_completed_round(self.state.task_dir(task["id"])) >= max_rounds_cfg:
                continue  # round-exhausted — resetting would not help
            self.state.set_task_status(task["id"], STATUS_TODO)

    def _setup_git(self) -> None:
        """AUTO-A3: ensure the base dir is a git repo and apply agent identity.

        Guarded: a git failure (e.g. git not installed) is logged but does NOT
        abort the run — Epic A stays usable without git, and commits (AUTO-C5)
        simply won't happen until git is available.
        """
        try:
            cfg = self._cfg()
            self.git = make_git_manager(self.base_dir, cfg)
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            self.state.log(
                f"git ready (user={self.git.git_user} <{self.git.git_email}>)"
            )
        except (GitError, OSError) as exc:
            self.git = None
            logger.warning("git setup failed (continuing without git): %s", exc)
            self.state.log(f"git setup failed: {exc}")


    def _log_auto_prompts(self) -> None:
        """If auto_prompts.json exists, print its contents to console and log it."""
        prompts_path = self.agent_dir / "auto_prompts.json"
        if not prompts_path.exists():
            return
        import json as _json
        try:
            data = _json.loads(prompts_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[{_ts()}] ⚠️  Could not read auto_prompts.json: {exc}")
            return
        ts = _ts()
        text = _json.dumps(data, indent=2, ensure_ascii=False)
        print(f"[{ts}] ✨ Validator prompts were tuned this run — auto_prompts.json:")
        print(text)
        self.state.log(f"auto_prompts.json contents:\n{text}")

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

    # ── COLLECT-23: opt-in collect-context injection ────────────────────

    def _collect_use_flag(self) -> bool:
        """`[collect] use_in_doc` for `task_mode == "docs"`, else
        `[collect] use_in_auto` — both default `false`, matching
        `agents.ini`'s own documented default (turning collect on never
        changes behaviour until this flag is also explicitly set)."""
        key = "use_in_doc" if self.task_mode == "docs" else "use_in_auto"
        return self.config.getboolean("collect", key, fallback=False)

    def collect_context_for(self, target_file: str) -> str:
        """The opt-in COLLECT-23 context block for `target_file`: its
        `collect` module record, contracts, and config reads — or `""`
        when the feature is off, the artifact is unavailable, or there is
        nothing to say about this file.

        Nothing calls this unless `[collect] use_in_auto`/`use_in_doc` is
        `true` *and* a caller actually invokes it — with the flag left at
        its default `false`, every call short-circuits before ever
        touching `tools.collect`, so a disabled run's context is
        byte-for-byte what it was before COLLECT-23 (this method's own
        AC).
        """
        if not self._collect_use_flag():
            return ""
        try:
            from tools.collect.loader import load as load_collect_model
            from tools.auto.context_assembler import build_collect_context_block
        except Exception as exc:  # noqa: BLE001 — opt-in feature, never fatal
            logger.warning("controller: collect context injection unavailable: %s", exc)
            return ""
        try:
            model = load_collect_model(self.base_dir, config=self.config, config_path=self.config_path)
            return build_collect_context_block(model, target_file, task_mode=self.task_mode)
        except Exception as exc:  # noqa: BLE001 — same fail-open stance as SummaryMemory/StoryBible above
            logger.warning("controller: collect context injection failed for %s: %s", target_file, exc)
            return ""


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
    except RuntimeError as exc:
        # BUGFIX: StateStore.initialise()'s "plan already holds a different
        # goal — refusing to silently resume it" guard raises RuntimeError
        # with a deliberately clear, multi-line, actionable message (existing
        # goal / new goal / exactly what to do about it). That guard runs
        # inside controller.run() (state.initialise() is called there, not
        # in __init__), so it fell into the branch below meant for genuinely
        # unexpected crashes: logger.exception() dumped a full Python
        # traceback to the console, then the same message was printed again
        # under an alarming "Fatal error:" banner. A deliberate, well-
        # designed safety check ended up looking exactly like an internal
        # bug in the tool instead of the guard it actually is. Give it the
        # same clean, traceback-free "Error: ..." treatment as the
        # constructor's own known-error cases just above.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error in autonomous run: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
